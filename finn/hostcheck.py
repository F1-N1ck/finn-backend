"""Fixed-target host check. No URL or host is accepted from callers. Output contains no secrets.

Targets: Yahoo (3 symbols), NSE announcements (RELIANCE, KPITTECH, TCS with empty-result
classification), Screener (KPITTECH), BSE PDF (concall: download + extract + compare with the
committed snapshot; annual report: first bytes only), BSE announcements API (recorded only),
and two grounded Groq requests on snapshot excerpts (optional).
"""

import hashlib
import json
import platform
import re
import socket
import sys
import time
from datetime import datetime, timezone

import requests

from finn import grounded_qa, llm_adapter, nse, snapshot

HEADERS = nse.HEADERS
CONCALL_URL = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d07f9c52-5c5a-443b-b239-6acd5e94b97a.pdf"
AR_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/4d2467e3-e28a-472b-8492-7fd9e46e63c4.pdf"


def timed(function):
    started = time.time()
    try:
        outcome = function()
        outcome.setdefault("ok", True)
    except Exception as error:
        outcome = {"ok": False, "error": type(error).__name__ + ": " + str(error)[:160]}
    outcome["seconds"] = round(time.time() - started, 2)
    return outcome


def check_yahoo(symbol):
    def run():
        response = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + symbol,
                                params={"range": "3mo", "interval": "1d"}, headers=HEADERS, timeout=20)
        info = {"status": response.status_code}
        if response.status_code != 200:
            info["ok"] = False
            return info
        result = response.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        last = len(stamps) - 1
        info.update({"rows": len(stamps),
                     "last_date": datetime.fromtimestamp(stamps[last], timezone.utc).strftime("%Y-%m-%d"),
                     "last_close": quote["close"][last], "last_volume": quote["volume"][last]})
        return info
    return timed(run)


def _stage(function):
    """Run one small step; never raise. Records seconds and the error TYPE only."""
    started = time.time()
    try:
        outcome = function()
    except Exception as error:
        outcome = {"ok": False, "error": type(error).__name__ + ": " + str(error)[:120]}
    outcome["seconds"] = round(time.time() - started, 2)
    return outcome


def check_nse(budget_s=110):
    """Stage by stage, so a timeout says WHERE it happened (page warm-up vs API vs attachment).
    NSE answers slowly from this host (about 20 s per API call), so the whole step has its own
    time budget and runs LAST; later stages are skipped (and say so) when the budget is spent."""
    started_at = time.time()

    def over_budget():
        return time.time() - started_at > budget_s

    info = {"stages": {}, "symbols": {}}
    session = requests.Session()
    session.headers.update(HEADERS)

    def warm_up():
        response = session.get(nse.PAGE_URL, timeout=20)
        return {"ok": response.status_code == 200, "status": response.status_code,
                "cookies_set": len(session.cookies)}

    info["stages"]["page_warmup"] = _stage(warm_up)

    def api_probe():
        result = nse.fetch(session, "RELIANCE", datetime.now(), datetime.now())
        return {"ok": result["kind"] == "list", "kind": result["kind"], "status": result["status"],
                "error": result["error"]}

    info["stages"]["api_probe"] = _stage(api_probe)
    if not info["stages"]["api_probe"]["ok"]:
        info["ok"] = False
        info["note"] = "NSE API unreachable from this host; remaining NSE steps skipped"
        return info

    attachment = None
    for symbol in ("TCS", "KPITTECH", "RELIANCE"):
        if over_budget():
            info["symbols"][symbol] = {"ok": False, "skipped": "nse time budget spent"}
            continue

        def classify(symbol=symbol):
            verdict = nse.classify_window(session, symbol, window_days=7)
            verdict["ok"] = verdict.get("verdict") in ("ok_nonempty", "valid_empty_window")
            return verdict
        verdict = _stage(classify)
        items = verdict.pop("items", None)
        if items and attachment is None and items[0].get("attchmntFile"):
            attachment = items[0]["attchmntFile"]
            verdict["sample_desc"] = items[0].get("desc")
        info["symbols"][symbol] = verdict

    def controls():
        now = datetime.now()
        month_ago = datetime.fromtimestamp(time.time() - 30 * 86400)
        lower = nse.fetch(session, "tcs", month_ago, now)
        upper = nse.fetch(session, "TCS", month_ago, now)
        return {"lowercase_tcs": {"kind": lower["kind"], "error": lower["error"],
                                  "count": len(lower["items"]) if lower["items"] is not None else None},
                "uppercase_TCS": {"kind": upper["kind"], "error": upper["error"],
                                  "count": len(upper["items"]) if upper["items"] is not None else None}}

    if over_budget():
        info["controls"] = {"ok": False, "skipped": "nse time budget spent"}
    else:
        info["controls"] = _stage(controls)
    if over_budget():
        info["tcs_market_wide"] = {"ok": False, "skipped": "nse time budget spent"}
    else:
        info["tcs_market_wide"] = _stage(lambda: nse.symbol_in_market_window(session, "TCS", window_days=7))

    if attachment and over_budget():
        info["attachment"] = {"ok": False, "skipped": "nse time budget spent"}
    elif attachment:
        def download():
            pdf = session.get(attachment, timeout=45)
            return {"status": pdf.status_code, "magic": pdf.content[:5].decode("latin-1"), "bytes": len(pdf.content)}
        info["attachment"] = _stage(download)
    info["ok"] = all(info["symbols"][name].get("ok") for name in info["symbols"])
    info["total_seconds"] = round(time.time() - started_at, 1)
    return info


def check_screener():
    def run():
        response = requests.get("https://www.screener.in/company/KPITTECH/consolidated/", headers=HEADERS, timeout=30)
        info = {"status": response.status_code}
        if response.status_code != 200:
            info["ok"] = False
            return info
        html = response.text
        info["has_documents_section"] = 'id="documents"' in html
        info["pdf_link_regex_count"] = len(re.findall(r"AnnPdfOpen|AttachHis", html))
        return info
    return timed(run)


def check_pdf(directory):
    def run():
        import pymupdf  # imported here so the API still starts if the wheel is missing
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get("https://www.bseindia.com/", timeout=30)
        response = session.get(CONCALL_URL, timeout=90, headers={"Referer": "https://www.bseindia.com/"})
        info = {"status": response.status_code, "bytes": len(response.content),
                "magic": response.content[:5].decode("latin-1")}
        if response.status_code != 200 or response.content[:5] != b"%PDF-":
            info["ok"] = False
            return info
        document = pymupdf.open(stream=response.content, filetype="pdf")
        snapshot_pages = snapshot.load_pages("KPIT_CONCALL_AUG2026", directory) or []
        by_page = {}
        for page in snapshot_pages:
            by_page[page["page"]] = hashlib.sha256(page["text"].encode()).hexdigest()
        same = 0
        for index in range(document.page_count):
            text = document[index].get_text("text")
            if by_page.get(index + 1) == hashlib.sha256(text.encode()).hexdigest():
                same = same + 1
        info["pages"] = document.page_count
        info["pages_text_identical_to_snapshot"] = same
        # annual report: only the first bytes (10 MB file; free instance is small)
        ar = session.get(AR_URL, stream=True, timeout=60, headers={"Referer": "https://www.bseindia.com/"})
        info["annual_report"] = {"status": ar.status_code, "content_length": ar.headers.get("content-length"),
                                 "magic": next(ar.iter_content(8), b"")[:5].decode("latin-1")}
        ar.close()
        return info
    return timed(run)


def check_bse_api():
    def run():
        session = requests.Session()
        session.headers.update(HEADERS)
        session.headers.update({"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"})
        today = datetime.now()
        params = {"pageno": 1, "strCat": -1, "strPrevDate": datetime.fromtimestamp(time.time() - 7 * 86400).strftime("%Y%m%d"),
                  "strScrip": "542651", "strSearch": "P", "strToDate": today.strftime("%Y%m%d"),
                  "strType": "C", "subcategory": -1}
        response = session.get("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w", params=params, timeout=30)
        info = {"status": response.status_code, "ok": response.status_code == 200}
        if response.status_code == 200:
            info["rows"] = len(response.json().get("Table", []))
        return info
    return timed(run)


def check_model(config, directory, cache_path):
    info = {"config": config.public_dict()}
    missing = config.missing()
    if len(missing) > 0:
        info["skipped"] = "missing env: " + ", ".join(missing)
        return info
    info["requests"] = []
    for question in grounded_qa.QUESTIONS:
        pages = snapshot.load_pages(question["doc"], directory)
        if pages is None:
            info["requests"].append({"id": question["id"], "skipped": "snapshot text missing"})
            continue
        # allow_live=True; if the provider fails, the record says outcome=cached with fallback_reason
        info["requests"].append(grounded_qa.run_question(config, question, pages, cache_path, allow_live=True))
    return info


def run_all(include_model, config, directory, cache_path, deadline_s=280):
    started = time.time()
    report = {
        "host": {"node": socket.gethostname(), "platform": platform.platform(),
                 "python": sys.version.split()[0], "utc": datetime.now(timezone.utc).isoformat()},
        "note": "Describes only the machine this ran on.",
        "snapshot": snapshot.verify(directory),
    }

    def remaining():
        return deadline_s - (time.time() - started)

    steps = [("yahoo", lambda: {s: check_yahoo(s) for s in ("RELIANCE.NS", "TCS.NS", "KPITTECH.NS")}),
             ("screener", check_screener), ("bse_pdf", lambda: check_pdf(directory)),
             ("bse_announcements_api", check_bse_api)]
    for name, function in steps:
        if remaining() <= 0:
            report[name] = {"ok": False, "error": "skipped: host check deadline reached"}
            continue
        report[name] = function()
    if include_model:
        report["hosted_model"] = check_model(config, directory, cache_path) if remaining() > 0 else {"skipped": "deadline"}
    # NSE last: it is the slow one (about 20 s per call from the free host)
    if remaining() > 30:
        report["nse"] = check_nse(budget_s=min(110, remaining() - 15))
    else:
        report["nse"] = {"ok": False, "error": "skipped: host check deadline reached"}
    report["total_seconds"] = round(time.time() - started, 1)
    return report
