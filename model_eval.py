# Steps 3 and 4: local model check (Ollama / llama.cpp) and a small evaluation.
#
# Model access is configurable with environment variables:
#   MODEL       default qwen3:4b
#   OLLAMA_URL  default http://localhost:11434
#   NUM_CTX     default 4096
#
# This script NEVER downloads a model. If the model is missing it says what to run.
# Outputs: results/03_model_check.json and results/04_eval.json

import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta

import psutil
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("MODEL", "qwen3:4b")
NUM_CTX = int(os.environ.get("NUM_CTX", "4096"))
RESULTS_DIR = "results"
TEXT_DIR = "data/text"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Categories copied from the reference document (17 rows).
CATEGORIES = {
    "results": "Quarterly/annual results, board meeting approving them, results decks, shareholder letters, ads confirming results were published.",
    "M&A": "Acquisition, merger, demerger, joint venture, investing in or selling a stake in a subsidiary.",
    "fund_raise": "Rights issues, QIPs, preferential allotments, buybacks, debt like commercial paper.",
    "management_change": "A CEO, CFO, MD or director joining, resigning, or retiring.",
    "litigation": "Lawsuits, regulatory orders, fines, warnings, insolvency/restructuring proceedings.",
    "insider_trading": "A promoter or insider buying/selling their own personal shareholding.",
    "earnings_call": "Anything about the investor conference call: notice, transcript, or recording link.",
    "investor_meetings": "One-on-one or small-group meetings with specific investors/funds, or attending an investor conference.",
    "credit_rating": "A rating agency assigning, upgrading, downgrading, or reaffirming a credit rating.",
    "dividend": "A cash dividend being declared, with its record date.",
    "bonus_split": "A bonus issue or stock split.",
    "capex": "Approving spend on a new plant, facility, or capacity expansion.",
    "press_release": "General news: order wins, new products, partnerships, rumour clarifications not covered elsewhere.",
    "postal_ballot": "Shareholders voting by postal ballot: notice and result.",
    "agm_egm": "Annual or Extraordinary General Meeting: notice and result.",
    "pledging": "A promoter pledging, releasing, or changing pledge status of shares.",
    "unclassified": "Routine low-relevance paperwork: ESOP allotments, compliance certificates, ESG reports, auditor appointments.",
}

# Expected labels are MY judgement from the reference table, not exchange-provided truth.
EXPECTED_BY_NSE_DESC = {
    "Cessation": "management_change",
    "Analysts/Institutional Investor Meet/Con. Call Updates": "earnings_call",
}

QUESTIONS = [
    {
        "id": "Q1_concall_guidance",
        "doc": "KPIT_CONCALL_AUG2026",
        "question": "What explicit forward guidance did management give on revenue growth or margins? "
                    "If none is stated in the excerpts, say so.",
        "keywords": ["guidance", "expect", "outlook", "growth", "margin", "target",
                     "next quarter", "full year", "going forward"],
    },
    {
        "id": "Q2_ar_headline_numbers",
        "doc": "KPIT_AR_2026",
        "question": "What were revenue from operations and profit for the year in the consolidated "
                    "statements, and for which year? Use only numbers that appear in the excerpts.",
        "keywords": ["revenue from operations", "profit for the year", "total income",
                     "consolidated", "net profit"],
    },
]


# ---------------------------------------------------------------- runtime check

def check_runtime():
    info = {"model_wanted": MODEL, "ollama_url": OLLAMA_URL}

    ollama_path = shutil.which("ollama")
    if ollama_path is None:
        app_path = "/Applications/Ollama.app/Contents/Resources/ollama"
        if os.path.exists(app_path):
            ollama_path = app_path
    info["ollama_binary"] = ollama_path
    info["llama_cli"] = shutil.which("llama-cli")
    info["llama_server"] = shutil.which("llama-server")

    try:
        response = requests.get(OLLAMA_URL + "/api/tags", timeout=4)
        info["server"] = "running"
        tags = response.json().get("models", [])
        names = []
        for tag in tags:
            names.append(tag.get("name"))
        info["installed_models"] = names
    except Exception as error:
        info["server"] = "not_reachable"
        info["server_error"] = str(error)
        info["installed_models"] = []

    info["model_installed"] = MODEL in info["installed_models"]

    if info["model_installed"]:
        try:
            show = requests.post(OLLAMA_URL + "/api/show", json={"model": MODEL}, timeout=20)
            info["model_details"] = show.json().get("details")
        except Exception as error:
            info["show_error"] = str(error)

    mem = psutil.virtual_memory()
    info["system_total_gb"] = round(mem.total / 1024 ** 3, 1)
    info["system_available_gb_at_start"] = round(mem.available / 1024 ** 3, 1)
    return info


def memory_snapshot():
    snap = {}
    try:
        response = requests.get(OLLAMA_URL + "/api/ps", timeout=5)
        models = response.json().get("models", [])
        loaded = []
        for model in models:
            loaded.append({
                "name": model.get("name"),
                "size_mb": round(model.get("size", 0) / 1024 ** 2),
                "size_vram_mb": round(model.get("size_vram", 0) / 1024 ** 2),
                "context_length": model.get("context_length"),
            })
        snap["ollama_ps"] = loaded
    except Exception as error:
        snap["ollama_ps_error"] = str(error)

    total_rss = 0
    for proc in psutil.process_iter(["name", "memory_info"]):
        name = (proc.info["name"] or "").lower()
        mem_info = proc.info["memory_info"]
        if "ollama" in name and mem_info is not None:
            total_rss = total_rss + mem_info.rss
    snap["ollama_processes_rss_mb"] = round(total_rss / 1024 ** 2)
    snap["system_available_gb"] = round(psutil.virtual_memory().available / 1024 ** 3, 1)
    return snap


# ---------------------------------------------------------------- model call

def strip_think(text):
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return cleaned.strip()


def parse_json(text):
    cleaned = strip_think(text)
    try:
        return json.loads(cleaned), True
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1]), True
        except Exception:
            pass
    return None, False


def call_model(system_text, user_text):
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text + "\n/no_think"},
    ]
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": 700},
    }

    start = time.time()
    response = requests.post(OLLAMA_URL + "/api/chat", json=payload, timeout=900)
    if response.status_code == 400 and "think" in response.text:
        payload.pop("think")
        start = time.time()
        response = requests.post(OLLAMA_URL + "/api/chat", json=payload, timeout=900)
    wall = time.time() - start

    record = {"http_status": response.status_code, "wall_seconds": round(wall, 2)}
    if response.status_code != 200:
        record["error"] = response.text[:300]
        return record

    data = response.json()
    raw = data.get("message", {}).get("content", "")
    parsed, ok = parse_json(raw)
    record["raw_output"] = raw
    record["parsed"] = parsed
    record["json_parse_ok"] = ok

    record["load_seconds"] = round(data.get("load_duration", 0) / 1e9, 2)
    record["prompt_tokens"] = data.get("prompt_eval_count")
    record["prompt_eval_seconds"] = round(data.get("prompt_eval_duration", 0) / 1e9, 2)
    record["output_tokens"] = data.get("eval_count")
    eval_seconds = data.get("eval_duration", 0) / 1e9
    record["generation_seconds"] = round(eval_seconds, 2)
    if eval_seconds > 0 and data.get("eval_count"):
        record["output_tokens_per_second"] = round(data["eval_count"] / eval_seconds, 1)
    record["memory_after_call"] = memory_snapshot()
    return record


# ---------------------------------------------------------------- announcements

def fetch_nse_announcements(symbol):
    today = datetime.now()
    from_date = (today - timedelta(days=10)).strftime("%d-%m-%Y")
    to_date = today.strftime("%d-%m-%Y")
    session = requests.Session()
    session.headers.update(HEADERS)
    page = session.get(
        "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        timeout=30)
    params = {"index": "equities", "symbol": symbol, "from_date": from_date, "to_date": to_date}
    response = session.get("https://www.nseindia.com/api/corporate-announcements",
                           params=params, timeout=30)
    info = {"page_status": page.status_code, "api_status": response.status_code,
            "from": from_date, "to": to_date}
    if response.status_code != 200:
        return [], info
    items = response.json()
    info["count"] = len(items)
    return items, info


def pick_two(items):
    chosen = []
    for wanted_desc in EXPECTED_BY_NSE_DESC:
        for item in items:
            if item.get("desc") == wanted_desc and item.get("attchmntText"):
                chosen.append(item)
                break
    return chosen


def classify(item):
    category_lines = []
    for name in CATEGORIES:
        category_lines.append("- " + name + ": " + CATEGORIES[name])
    system_text = (
        "Classify a stock-exchange announcement into exactly one category.\n"
        "Categories:\n" + "\n".join(category_lines) + "\n"
        'Return JSON only: {"category": "<one category name>", "reason": "<max 20 words>"}'
    )
    user_text = "Announcement text:\n" + item.get("attchmntText", "")
    record = call_model(system_text, user_text)
    expected = EXPECTED_BY_NSE_DESC.get(item.get("desc"))
    predicted = None
    if record.get("parsed"):
        predicted = record["parsed"].get("category")
    record["input_text"] = item.get("attchmntText")
    record["nse_desc"] = item.get("desc")
    record["source_link"] = item.get("attchmntFile")
    record["announcement_time"] = item.get("an_dt")
    record["expected_by_me"] = expected
    record["predicted"] = predicted
    record["valid_category"] = predicted in CATEGORIES
    record["matches_expected"] = predicted == expected
    return record


# ---------------------------------------------------------------- retrieval + QA

def load_pages(doc_id):
    path = os.path.join(TEXT_DIR, doc_id + ".pages.jsonl")
    if not os.path.exists(path):
        return None
    pages = []
    file_handle = open(path)
    for line in file_handle:
        pages.append(json.loads(line))
    file_handle.close()
    return pages


def best_window(text, keywords, size=1800, step=600):
    lowered = text.lower()
    best_score = 0
    best_start = 0
    start = 0
    while start < max(1, len(text) - size + step):
        chunk = lowered[start:start + size]
        score = 0
        for keyword in keywords:
            score = score + chunk.count(keyword)
        if score > best_score:
            best_score = score
            best_start = start
        start = start + step
    return best_score, best_start, text[best_start:best_start + size]


def build_excerpts(pages, keywords, top_k=3):
    scored = []
    for page in pages:
        if "no_text_has_image_likely_scanned" in page["flags"]:
            continue
        if "no_text_no_image_blank_or_vector" in page["flags"]:
            continue
        score, start, chunk = best_window(page["text"], keywords)
        if score > 0:
            scored.append((score, page["page"], start, chunk))
    scored.sort(reverse=True)
    excerpts = []
    for rank, item in enumerate(scored[:top_k]):
        excerpts.append({
            "source_id": "S" + str(rank + 1),
            "page": item[1],
            "char_start": item[2],
            "keyword_score": item[0],
            "text": item[3],
        })
    return excerpts


def normalise(text):
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def numbers_in(text):
    found = re.findall(r"\d[\d,]*\.?\d*", text)
    cleaned = []
    for token in found:
        token = token.replace(",", "").rstrip(".")
        if token != "":
            cleaned.append(token)
    return cleaned


def check_claims(parsed, excerpts):
    by_id = {}
    for excerpt in excerpts:
        by_id[excerpt["source_id"]] = excerpt["text"]

    all_numbers = set()
    for excerpt in excerpts:
        for number in numbers_in(excerpt["text"]):
            all_numbers.add(number)

    report = {"claims": [], "answer_numbers_not_in_excerpts": []}
    if not parsed:
        report["error"] = "no parsed JSON"
        return report

    claims = parsed.get("claims") or []
    for claim in claims:
        source = claim.get("source")
        quote = claim.get("quote") or ""
        text = claim.get("text") or ""
        row = {"text": text, "source": source, "quote": quote}
        row["source_exists"] = source in by_id
        if row["source_exists"]:
            row["quote_verbatim_in_source"] = normalise(quote) in normalise(by_id[source])
            source_numbers = set(numbers_in(by_id[source]))
        else:
            row["quote_verbatim_in_source"] = False
            source_numbers = set()
        missing = []
        for number in numbers_in(text):
            if number not in source_numbers:
                missing.append(number)
        row["numbers_missing_from_cited_source"] = missing
        row["auto_pass"] = (row["source_exists"] and row["quote_verbatim_in_source"]
                            and len(missing) == 0)
        report["claims"].append(row)

    answer_text = parsed.get("answer") or ""
    for number in numbers_in(answer_text):
        if number not in all_numbers:
            report["answer_numbers_not_in_excerpts"].append(number)

    passed = 0
    for row in report["claims"]:
        if row["auto_pass"]:
            passed = passed + 1
    report["auto_pass_count"] = passed
    report["claim_count"] = len(report["claims"])
    report["note"] = ("Automatic check covers: source id exists, quote is verbatim in that excerpt, "
                      "numbers in the claim appear in that excerpt. It does NOT check meaning "
                      "(e.g. wrong year or wrong line item). A human read is still needed.")
    return report


def answer_question(question):
    pages = load_pages(question["doc"])
    result = {"id": question["id"], "doc": question["doc"], "question": question["question"]}
    if pages is None:
        result["skipped"] = "extracted text not found (step 2 failed or was not run)"
        return result

    excerpts = build_excerpts(pages, question["keywords"])
    result["excerpts"] = excerpts
    if len(excerpts) == 0:
        result["skipped"] = "no excerpt matched the keywords"
        return result

    excerpt_blocks = []
    for excerpt in excerpts:
        block = "[" + excerpt["source_id"] + "] (page " + str(excerpt["page"]) + ")\n" + excerpt["text"]
        excerpt_blocks.append(block)

    system_text = (
        "You answer strictly from the numbered excerpts. Never use outside knowledge.\n"
        'Return JSON only: {"answer": string, "not_stated": boolean, '
        '"claims": [{"text": string, "source": "S1", "quote": string}]}.\n'
        "Every quote must be copied verbatim from the cited excerpt. "
        "If the excerpts do not contain the answer, set not_stated to true and claims to []."
    )
    user_text = "\n\n".join(excerpt_blocks) + "\n\nQuestion: " + question["question"]
    record = call_model(system_text, user_text)
    result["model_call"] = record
    result["check"] = check_claims(record.get("parsed"), excerpts)
    return result


# ---------------------------------------------------------------- main

def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    runtime = check_runtime()
    check_path = os.path.join(RESULTS_DIR, "03_model_check.json")
    file_handle = open(check_path, "w")
    json.dump(runtime, file_handle, indent=2)
    file_handle.close()
    print("runtime:", json.dumps(runtime, indent=2))

    if runtime["server"] != "running":
        print("\nOLLAMA SERVER NOT REACHABLE. If Ollama is installed, open the Ollama app, "
              "then re-run. If not installed, install it from ollama.com (ask first, then I stop here).")
        return
    if not runtime["model_installed"]:
        print("\nMODEL NOT INSTALLED: " + MODEL)
        print("Installed models: " + str(runtime["installed_models"]))
        print("To test it, run:  ollama pull " + MODEL)
        print("Check the download size on the Ollama library page before pulling. "
              "This script does not download models.")
        print("To run the eval on an already-installed model instead, e.g.  MODEL=qwen2.5:3b bash run_all.sh")
        print("(that would NOT be a Qwen3-4B test; results must be labelled with the model used).")
        return

    output = {"model": MODEL, "num_ctx": NUM_CTX, "runtime": runtime}

    # classification
    output["classification"] = []
    try:
        items, fetch_info = fetch_nse_announcements("KPITTECH")
        output["nse_fetch"] = fetch_info
        chosen = pick_two(items)
        output["classification_items_found"] = len(chosen)
        for item in chosen:
            output["classification"].append(classify(item))
    except Exception as error:
        output["nse_fetch_error"] = str(error)

    # research questions
    output["qa"] = []
    for question in QUESTIONS:
        output["qa"].append(answer_question(question))

    out_path = os.path.join(RESULTS_DIR, "04_eval.json")
    file_handle = open(out_path, "w")
    json.dump(output, file_handle, indent=2)
    file_handle.close()
    print("wrote " + out_path)


if __name__ == "__main__":
    main()
