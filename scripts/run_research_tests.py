"""Run the generic research flow against a deployed FINN and record what happened.

Usage (Terminal, from the project folder):
    python3 scripts/run_research_tests.py --base https://finn-backend-b4j5.onrender.com --unfamiliar ABCD

Tickers tested in order: KPITTECH, PIDILITIND, HINDUNILVR and the one you choose with --unfamiliar
(choose it at test time; do not pick one you have already looked at). Also runs small negative probes
and repeats the first ticker to measure the cached path. Writes:
    results/research_tests.json         summary, latency, failures
    results/research_briefs/<TICKER>.json   the full brief, for manual claim and citation checks
No API key or token is used or printed. Uses only the public /api/analyze endpoints.
"""

import argparse
import json
import os
import sys
import time

import requests


def post_analyze(base, body):
    started = time.time()
    response = requests.post(base + "/api/analyze", json=body, timeout=60)
    return response, round(time.time() - started, 2)


def run_one(base, ticker, max_wait_s=840):
    record = {"ticker": ticker, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "attempts": []}
    started = time.time()
    job = None
    for attempt in range(4):
        response, submit_s = post_analyze(base, {"ticker": ticker})
        record["attempts"].append({"http": response.status_code, "submit_s": submit_s,
                                   "detail": (response.json().get("detail") if response.status_code >= 400 else None)})
        if response.status_code == 202:
            job = response.json()
            break
        if response.status_code == 429 and (response.json().get("detail") or {}).get("code") == "busy":
            time.sleep(30)
            continue
        break
    if job is None:
        record["outcome"] = "not_started"
        record["wall_s"] = round(time.time() - started, 1)
        return record, None
    job_id = job["job_id"]
    while job["status"] == "running" and time.time() - started < max_wait_s:
        time.sleep(4)
        try:
            job = requests.get(base + "/api/analyze/" + job_id, timeout=60).json()
        except Exception as error:
            record.setdefault("poll_errors", []).append(type(error).__name__)
    record["wall_s"] = round(time.time() - started, 1)
    record["status"] = job["status"]
    record["server_elapsed_s"] = job.get("elapsed_s")
    record["stages"] = job.get("stages")
    if job.get("error"):
        record["error"] = job["error"]
    brief = job.get("brief")
    if brief:
        record["answer_source"] = brief.get("answer_source")
        record["fallback_reason"] = brief.get("fallback_reason")
        record["sections"] = {k: v["status"] for k, v in brief.get("sections", {}).items()}
        record["documents"] = {}
        for kind, doc in (brief.get("documents") or {}).items():
            if doc.get("status") == "ok":
                record["documents"][kind] = {"title": doc["title"], "url": doc["url"], "fetch": doc["fetch"],
                                             "extraction": doc["extraction"], "skipped": doc.get("skipped")}
            else:
                record["documents"][kind] = {"status": doc.get("status"), "reason": doc.get("reason"),
                                             "skipped": doc.get("skipped")}
        record["notes"] = brief.get("notes")
        record["limitations"] = brief.get("limitations")
    record["outcome"] = "done" if job["status"] == "done" else job["status"]
    return record, brief


def print_claims(brief):
    for name, section in (brief.get("sections") or {}).items():
        print("  [" + name + "] " + section["status"] + (": " + section.get("reason", "") if section["status"] != "ok" else ""))
        for claim in section.get("claims", []):
            cite = claim["citation"]
            print("    - " + claim["text"][:140].replace("\n", " "))
            print("      p." + str(cite["pdf_page"]) + " | " + cite["doc_date"] + " | " + cite["url"])
        for item in section.get("items", []):
            cite = item["citation"]
            print("    - " + item["metric"] + " = " + item["value"] + " " + item["unit"] + " | " + item["period"]
                  + " | " + item["scope"] + " | p." + str(cite["pdf_page"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--unfamiliar", default="")
    parser.add_argument("--out", default="results/research_tests.json")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    unfamiliar = args.unfamiliar.strip().upper()
    if unfamiliar == "":
        unfamiliar = input("Unfamiliar NSE ticker to test (chosen now): ").strip().upper()
    os.makedirs("results/research_briefs", exist_ok=True)

    report = {"base": base, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "unfamiliar_ticker": unfamiliar,
              "negative_probes": [], "runs": []}

    probes = [("invalid ticker with space", {"ticker": "A B"}),
              ("ticker that is a URL", {"ticker": "https://evil.example/x.pdf"}),
              ("extra url field", {"ticker": "TCS", "url": "https://evil.example/x.pdf"})]
    for label, body in probes:
        response, seconds = post_analyze(base, body)
        report["negative_probes"].append({"probe": label, "http": response.status_code, "seconds": seconds,
                                          "expected": 422, "pass": response.status_code == 422})
        print("probe:", label, "->", response.status_code)

    order = ["KPITTECH", "PIDILITIND", "HINDUNILVR", unfamiliar]
    seen = set()
    for ticker in order:
        if ticker in seen:
            continue
        seen.add(ticker)
        print("\n=== " + ticker + " ===")
        record, brief = run_one(base, ticker)
        record["kind"] = "unfamiliar" if ticker == unfamiliar and ticker not in ("KPITTECH", "PIDILITIND", "HINDUNILVR") else "named"
        report["runs"].append(record)
        print(" outcome:", record.get("outcome"), "| wall", record.get("wall_s"), "s | sections:", record.get("sections"),
              "| error:", record.get("error"))
        if brief:
            with open("results/research_briefs/" + ticker + ".json", "w") as handle:
                json.dump(brief, handle, indent=1)
            print_claims(brief)

    first = order[0]
    print("\n=== repeat " + first + " (cache path) ===")
    record, brief = run_one(base, first)
    record["kind"] = "repeat_for_cache"
    report["runs"].append(record)
    print(" outcome:", record.get("outcome"), "| wall", record.get("wall_s"), "s | source:", record.get("answer_source"))

    print("\n=== unknown ticker ===")
    record, _ = run_one(base, "ZZQXNOTREAL")
    record["kind"] = "unknown_ticker_probe"
    report["runs"].append(record)
    print(" outcome:", record.get("outcome"), "| error:", record.get("error"))

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(args.out, "w") as handle:
        json.dump(report, handle, indent=1)
    print("\nSaved " + args.out + " and results/research_briefs/*.json")


if __name__ == "__main__":
    sys.exit(main())
