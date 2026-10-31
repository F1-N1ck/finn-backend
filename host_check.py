"""Run this ON THE INTENDED DEPLOYMENT HOST (the machine/service that will serve FINN).

It tests, from that host:
  1. Yahoo EOD price/volume (3 symbols, one outside the demo set)
  2. NSE announcements (2 symbols) and one NSE attachment PDF
  3. Screener documents section
  4. One BSE primary-source PDF (download + text extraction)   [+ BSE announcements API, recorded only]
  5. Two hosted-model requests using extracted passages with source IDs (needs FINN_LLM_* env)
Output: results/05_host_check.json (no secrets inside) and a printed summary.
The manual claim check (step 3 of your list) is done by a human afterwards, from this JSON.
"""

import json
import os
import platform
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract_test
from finn import llm_adapter
from finn import grounded_qa

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
RESULTS_DIR = "results"


def timed(function):
    started = time.time()
    try:
        outcome = function()
        outcome["ok"] = True
    except Exception as error:
        outcome = {"ok": False, "error": type(error).__name__ + ": " + str(error)[:200]}
    outcome["seconds"] = round(time.time() - started, 2)
    return outcome


def check_yahoo(symbol):
    def run():
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
        response = requests.get(url, params={"range": "3mo", "interval": "1d"}, headers=HEADERS, timeout=20)
        info = {"status": response.status_code}
        if response.status_code != 200:
            info["ok_status"] = False
            raise RuntimeError("HTTP " + str(response.status_code))
        result = response.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        last = len(timestamps) - 1
        info["rows"] = len(timestamps)
        info["last_date"] = datetime.fromtimestamp(timestamps[last], timezone.utc).strftime("%Y-%m-%d")
        info["last_close"] = quote["close"][last]
        info["last_volume"] = quote["volume"][last]
        return info
    return timed(run)


def check_nse(symbols):
    def run():
        session = requests.Session()
        session.headers.update(HEADERS)
        page = session.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements", timeout=30)
        info = {"page_status": page.status_code, "symbols": {}}
        today = datetime.now()
        from_date = (today - timedelta(days=10)).strftime("%d-%m-%Y")
        to_date = today.strftime("%d-%m-%Y")
        first_link = None
        for symbol in symbols:
            params = {"index": "equities", "symbol": symbol, "from_date": from_date, "to_date": to_date}
            response = session.get("https://www.nseindia.com/api/corporate-announcements", params=params, timeout=30)
            entry = {"status": response.status_code}
            if response.status_code == 200:
                items = response.json()
                entry["count"] = len(items)
                if len(items) > 0 and first_link is None and items[0].get("attchmntFile"):
                    first_link = items[0]["attchmntFile"]
                    entry["first_item_desc"] = items[0].get("desc")
            info["symbols"][symbol] = entry
        info["attachment_link_tested"] = first_link
        if first_link:
            pdf = session.get(first_link, timeout=60)
            info["attachment_status"] = pdf.status_code
            info["attachment_magic"] = pdf.content[:5].decode("latin-1")
            info["attachment_bytes"] = len(pdf.content)
        return info
    return timed(run)


def check_screener(symbol):
    def run():
        url = "https://www.screener.in/company/" + symbol + "/consolidated/"
        response = requests.get(url, headers=HEADERS, timeout=30)
        info = {"status": response.status_code}
        if response.status_code != 200:
            raise RuntimeError("HTTP " + str(response.status_code))
        html = response.text
        info["has_documents_section"] = 'id="documents"' in html
        info["annual_report_links_regex_count"] = len(re.findall(r"Annual Report 20\d\d", html))
        info["pdf_links_regex_count"] = len(re.findall(r"AnnPdfOpen|AttachHis", html))
        return info
    return timed(run)


def check_bse_api(code):
    def run():
        session = requests.Session()
        session.headers.update(HEADERS)
        session.headers.update({"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"})
        today = datetime.now()
        params = {"pageno": 1, "strCat": -1, "strPrevDate": (today - timedelta(days=7)).strftime("%Y%m%d"),
                  "strScrip": code, "strSearch": "P", "strToDate": today.strftime("%Y%m%d"),
                  "strType": "C", "subcategory": -1}
        response = session.get("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
                               params=params, timeout=30)
        info = {"status": response.status_code}
        if response.status_code == 200:
            info["rows"] = len(response.json().get("Table", []))
        else:
            raise RuntimeError("HTTP " + str(response.status_code))
        return info
    return timed(run)


def check_pdfs():
    results = {}
    for doc in extract_test.DOCS:
        def run(doc=doc):
            download_info = extract_test.download(doc)
            processed = extract_test.process(doc, download_info)
            keep = {}
            for name in ("extraction", "n_pages", "total_chars", "flag_counts", "text_file"):
                if name in processed:
                    keep[name] = processed[name]
            keep["download"] = download_info.get("download")
            keep["bytes"] = download_info.get("bytes")
            keep["http_status"] = download_info.get("status")
            if processed.get("extraction") != "ok":
                raise RuntimeError("download or extraction failed: " + str(download_info.get("download")))
            return keep
        results[doc["id"]] = timed(run)
    return results


def load_pages(doc_id):
    path = os.path.join("data", "text", doc_id + ".pages.jsonl")
    if not os.path.exists(path):
        return None
    pages = []
    file_handle = open(path)
    for line in file_handle:
        pages.append(json.loads(line))
    file_handle.close()
    return pages


def check_model():
    llm_adapter.load_dotenv_if_present(".env")
    config = llm_adapter.LLMConfig.from_env()
    info = {"config": config.public_dict()}
    missing = config.missing()
    if len(missing) > 0:
        info["skipped"] = "missing env: " + ", ".join(missing)
        return info
    info["requests"] = []
    cache_path = os.environ.get("FINN_CACHE_PATH", "data/cache/demo_answers.json")
    for question in grounded_qa.QUESTIONS:
        pages = load_pages(question["doc"])
        if pages is None:
            info["requests"].append({"id": question["id"], "skipped": "extracted text missing"})
            continue
        info["requests"].append(grounded_qa.run_question(config, question, pages, cache_path))
    return info


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    report = {
        "host": {"node": socket.gethostname(), "platform": platform.platform(),
                 "python": sys.version.split()[0], "utc": datetime.now(timezone.utc).isoformat()},
        "note": "Results describe ONLY the machine this ran on.",
    }
    print("host:", report["host"])
    report["yahoo"] = {}
    for symbol in ("RELIANCE.NS", "TCS.NS", "KPITTECH.NS"):
        report["yahoo"][symbol] = check_yahoo(symbol)
        print("yahoo", symbol, report["yahoo"][symbol])
    report["nse"] = check_nse(["RELIANCE", "KPITTECH"])
    print("nse", report["nse"])
    report["screener"] = check_screener("KPITTECH")
    print("screener", report["screener"])
    report["bse_pdfs"] = check_pdfs()
    print("pdfs", report["bse_pdfs"])
    report["bse_announcements_api"] = check_bse_api("542651")
    print("bse api", report["bse_announcements_api"])
    report["hosted_model"] = check_model()
    print("model config:", report["hosted_model"].get("config"), report["hosted_model"].get("skipped", ""))

    out_path = os.path.join(RESULTS_DIR, "05_host_check.json")
    file_handle = open(out_path, "w")
    json.dump(report, file_handle, indent=2)
    file_handle.close()
    print("wrote " + out_path)


if __name__ == "__main__":
    main()
