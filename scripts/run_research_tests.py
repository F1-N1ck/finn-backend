"""Run the generic research flow against a deployed FINN and record what happened.

Tip: run it as  caffeinate -i python3 scripts/run_research_tests.py ...  so the Mac does not sleep mid-run
(a sleep shows up as a long poll gap and inflates the wall time; the script now flags it as client_pause_s).

Usage (Terminal, from the project folder):
    python3 scripts/run_research_tests.py --base https://finn-backend-b4j5.onrender.com --tickers KPITTECH,PIDILITIND,HINDUNILVR,COFORGE

What is different from the first version:
  * results/research_tests.json is rewritten after EVERY step, so a crash never loses earlier results.
  * every poll is logged (time, HTTP status, job status, stage count, server elapsed, instance id).
  * a poll that returns something unexpected (no "status", a non-JSON body, 404, 5xx) is NOT a crash:
    the HTTP status and a redacted copy of the body are saved, and the script retries a few times.
  * hard time limit per company (--max-wait), then the run is recorded as "client_timeout".
Writes:
    results/research_tests.json              summary, polls, failures
    results/research_briefs/<TICKER>.json    the full brief when there is one
No API key or token is used or printed. Uses only the public /api/analyze endpoints.
"""

import argparse
import json
import os
import re
import sys
import threading
import time

import requests

SECRET_RE = re.compile(r"(gsk_[A-Za-z0-9]+|Bearer\s+\S+|api[_-]?key[\"':= ]+\S+|token[\"':= ]+\S+)", re.I)


def redact(text, limit=600):
    text = SECRET_RE.sub("[redacted]", text or "")
    return text[:limit]


def describe_response(response):
    """Small, safe description of any HTTP response."""
    body_text = ""
    try:
        body_text = response.text
    except Exception as error:
        body_text = "<unreadable: " + type(error).__name__ + ">"
    keys = None
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            keys = sorted(parsed.keys())
    except Exception:
        pass
    return {"http": response.status_code, "content_type": response.headers.get("content-type"),
            "json_keys": keys, "body_redacted": redact(body_text)}


def save(report, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(report, handle, indent=1)
    os.replace(tmp, path)


def post_analyze(base, body):
    started = time.time()
    try:
        response = requests.post(base + "/api/analyze", json=body, timeout=60)
    except requests.RequestException as error:
        return None, round(time.time() - started, 2), type(error).__name__
    return response, round(time.time() - started, 2), None


def add_server_timing(info, response, sent_at):
    """Split a poll into: send -> app first saw it | inside the handler | handler done -> client got the body.
    The first and last parts mix network time with any clock difference between this computer and the server
    (normally under a second), so only large values mean anything."""
    try:
        received = float(response.headers.get("X-Received-At"))
        responded = float(response.headers.get("X-Responded-At"))
    except (TypeError, ValueError):
        return
    info["x_received_at"] = received
    info["x_responded_at"] = responded
    info["split_s"] = {"before_app": round(received - sent_at, 2), "handler": round(responded - received, 2),
                       "after_handler": round(info["t"] - responded, 2)}


def poll_once(base, job_id):
    started = time.time()
    try:
        response = requests.get(base + "/api/analyze/" + job_id, timeout=45)
    except requests.RequestException as error:
        return {"t": time.time(), "network_error": type(error).__name__, "seconds": round(time.time() - started, 2)}, None
    info = describe_response(response)
    info["t"] = time.time()
    info["sent_at"] = round(started, 3)
    info["seconds"] = round(time.time() - started, 2)
    info["headers_s"] = round(response.elapsed.total_seconds(), 2)  # request sent -> response headers parsed
    info["body_bytes"] = len(response.content)
    add_server_timing(info, response, started)
    job = None
    try:
        parsed = response.json()
        if isinstance(parsed, dict) and "status" in parsed:
            job = parsed
            info["status"] = parsed.get("status")
            info["stages"] = len(parsed.get("stages") or [])
            info["server_elapsed_s"] = parsed.get("elapsed_s")
            info["instance"] = parsed.get("instance")
            if isinstance(parsed.get("server_timing"), dict):
                st = parsed["server_timing"]
                info["server_timing"] = {k: st.get(k) for k in ("server_time", "cpu_s", "rss_mb", "stage", "probe_events", "slow_handlers", "host_cpu")}
        else:
            info["unexpected"] = "response has no 'status' field"
    except Exception:
        info["unexpected"] = "body is not JSON"
    return info, job


def start_pinger(base, pings, stop, every_s=3.0):
    """While a job runs, ask the cheap /healthz endpoint every few seconds from a second thread. If a poll hangs
    but these answer at once, the stall is specific to that request (or its connection); if they hang too, the
    whole instance (or the path to it) was unresponsive."""
    def loop():
        while not stop.is_set():
            started = time.time()
            entry = {"sent_at": round(started, 3)}
            try:
                response = requests.get(base + "/healthz", timeout=60)
                entry["http"] = response.status_code
                entry["headers_s"] = round(response.elapsed.total_seconds(), 2)
            except requests.RequestException as error:
                entry["network_error"] = type(error).__name__
            entry["seconds"] = round(time.time() - started, 2)
            pings.append(entry)
            stop.wait(every_s)
    thread = threading.Thread(target=loop, daemon=True, name="healthz-pinger")
    thread.start()
    return thread


def run_one(base, ticker, max_wait_s, poll_s, report_hook):
    record = {"ticker": ticker, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "attempts": [], "polls": []}
    started = time.time()
    job = None
    for attempt in range(4):
        response, submit_s, error = post_analyze(base, {"ticker": ticker})
        if response is None:
            record["attempts"].append({"network_error": error, "submit_s": submit_s})
            time.sleep(10)
            continue
        info = describe_response(response)
        info["submit_s"] = submit_s
        record["attempts"].append(info)
        if response.status_code == 202:
            try:
                job = response.json()
            except Exception:
                job = None
            break
        detail = {}
        try:
            detail = response.json().get("detail") or {}
        except Exception:
            pass
        if response.status_code == 429 and isinstance(detail, dict) and detail.get("code") == "busy":
            time.sleep(30)
            continue
        break
    if job is None or "job_id" not in job:
        record["outcome"] = "not_started"
        record["wall_s"] = round(time.time() - started, 1)
        return record, None
    record["job_id"] = job["job_id"]
    record["first_instance"] = job.get("instance")
    record["pings"] = []
    stop_pings = threading.Event()
    start_pinger(base, record["pings"], stop_pings)
    bad_streak = 0
    final = job if job.get("status") != "running" else None
    while final is None and time.time() - started < max_wait_s:
        before = time.time()
        time.sleep(poll_s)
        info, polled = poll_once(base, job["job_id"])
        pause = time.time() - before - info.get("seconds", 0)
        if pause > poll_s + 30:
            # the loop itself stalled (for example the computer slept); this is NOT server time
            info["client_pause_s"] = round(pause, 1)
            record["client_paused_s"] = round(record.get("client_paused_s", 0) + pause, 1)
        record["polls"].append(info)
        report_hook()
        if polled is None:
            bad_streak += 1
            if info.get("http") == 404:
                record["outcome"] = "job_lost"
                record["job_lost_note"] = "poll returned 404; the server probably restarted or the job was evicted"
                break
            if bad_streak >= 8:
                record["outcome"] = "poll_failed"
                break
            continue
        bad_streak = 0
        if polled["status"] != "running":
            final = polled
    stop_pings.set()
    record["wall_s"] = round(time.time() - started, 1)
    record["active_wall_s"] = round(record["wall_s"] - record.get("client_paused_s", 0), 1)
    if final is None:
        record.setdefault("outcome", "client_timeout" if "outcome" not in record else record["outcome"])
        return record, None
    record["status"] = final["status"]
    record["server_elapsed_s"] = final.get("elapsed_s")
    record["server_outcome"] = final.get("outcome")
    record["stages"] = final.get("stages")
    if final.get("error"):
        record["error"] = final["error"]
    brief = final.get("brief")
    if brief:
        record["answer_source"] = brief.get("answer_source")
        record["fallback_reason"] = brief.get("fallback_reason")
        record["sections"] = {k: v["status"] for k, v in (brief.get("sections") or {}).items()}
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
    record["outcome"] = final.get("outcome") or final["status"]
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
    parser.add_argument("--tickers", default="KPITTECH,PIDILITIND,HINDUNILVR")
    parser.add_argument("--unfamiliar", default="", help="extra ticker chosen at test time")
    parser.add_argument("--max-wait", type=int, default=480, help="seconds to wait per company")
    parser.add_argument("--poll", type=int, default=5)
    parser.add_argument("--skip-probes", action="store_true")
    parser.add_argument("--out", default="results/research_tests.json")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    order = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.unfamiliar.strip():
        order.append(args.unfamiliar.strip().upper())
    os.makedirs("results/research_briefs", exist_ok=True)

    report = {"base": base, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tickers": order,
              "max_wait_s": args.max_wait, "negative_probes": [], "runs": []}
    hook = lambda: save(report, args.out)

    if not args.skip_probes:
        probes = [("invalid ticker with space", {"ticker": "A B"}),
                  ("ticker that is a URL", {"ticker": "https://evil.example/x.pdf"}),
                  ("extra url field", {"ticker": "TCS", "url": "https://evil.example/x.pdf"})]
        for label, body in probes:
            response, seconds, error = post_analyze(base, body)
            code = response.status_code if response is not None else None
            report["negative_probes"].append({"probe": label, "http": code, "network_error": error,
                                              "seconds": seconds, "expected": 422, "pass": code == 422})
            print("probe:", label, "->", code)
            hook()

    seen = set()
    for ticker in order:
        if ticker in seen:
            continue
        seen.add(ticker)
        print("\n=== " + ticker + " ===", flush=True)
        record = {"ticker": ticker, "outcome": "running"}
        report["runs"].append(record)
        try:
            result, brief = run_one(base, ticker, args.max_wait, args.poll, hook)
        except Exception as error:  # a bug in this script must not lose the other results
            result, brief = {"ticker": ticker, "outcome": "script_error", "error": type(error).__name__ + ": " + redact(str(error))}, None
        record.clear()
        record.update(result)
        hook()
        print(" outcome:", record.get("outcome"), "| wall", record.get("wall_s"), "s (active", record.get("active_wall_s"), "s) | server", record.get("server_elapsed_s"),
              "s | sections:", record.get("sections"), "| error:", record.get("error"), flush=True)
        if brief:
            with open("results/research_briefs/" + ticker + ".json", "w") as handle:
                json.dump(brief, handle, indent=1)
            print_claims(brief)

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    hook()
    print("\nSaved " + args.out)


if __name__ == "__main__":
    sys.exit(main())
