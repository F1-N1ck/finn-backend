"""Local server for UI testing ONLY. The model and live prices are faked; nothing leaves this machine.
Run:  python tests/ui_server.py   (serves http://127.0.0.1:8765)"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["FINN_LLM_API_KEY"] = "fake-key-for-ui-test"
os.environ["FINN_LLM_BASE_URL"] = "https://api.example.test/v1"
os.environ["FINN_LLM_MODEL"] = "fake-model"

import uvicorn

from finn import digest, llm_adapter, prices
import app.main as main


def fake_chat(config, messages):
    user_text = messages[-1]["content"]
    after = user_text.split("[S1]", 1)[1]
    body = after.split("\n", 1)[1]
    quote = body.strip().split("\n")[0]
    reply = {"answer": "FAKE MODEL ANSWER for UI testing.", "not_stated": False,
             "claims": [{"text": "First line of the first passage.", "source": "S1", "quote": quote}]}
    return {"text": json.dumps(reply), "usage": {}, "latency_s": 0.01, "attempts": 1,
            "rate_limit_headers": {}, "cost_usd_estimate": None}


ADDABLE = {  # tickers the fake Yahoo knows (valid NSE equities) and their names
    "BAJFINANCE": "Bajaj Finance Limited", "ASIANPAINT": "Asian Paints Limited", "HAVELLS": "Havells India Limited",
    "ETERNAL": "Eternal Limited", "FAILCO": "Fail Co Limited (test: NSE filings check fails)",
    "NOPRICE": "No Price Co Limited (test: price source fails)", "TATAMOTORS": "Tata Motors Limited", "TATASTEEL": "Tata Steel Limited",
}


def fake_fetch_symbol(symbol, now=None):
    raw = digest._load_json("raw_prices.json", "data/snapshot")
    if symbol in raw:
        stats = digest.price_stats(raw[symbol]["bars"])
        stats["close"] = round(stats["close"] * 1.01, 2)
        stats["change_pct"] = 1.5
    elif symbol == "NOPRICE":
        return {"symbol": symbol, "ok": False, "error": "ReadTimeout"}
    elif symbol in ADDABLE:
        stats = {"last_date": "2026-10-30", "close": 1234.5, "prev_close": 1200.0, "change_pct": 2.88, "volume": 500000,
                 "avg_volume_20d": 400000, "volume_ratio": 1.25, "name": ADDABLE[symbol]}
    else:
        return {"symbol": symbol, "ok": False, "error": "not_found"}
    stats["partial"] = False
    stats["symbol"] = symbol
    stats["ok"] = True
    return stats


def fake_check_symbol(symbol):
    if symbol in ("SLOWCHK",):
        return {"symbol": symbol, "state": "check_failed", "error": "ReadTimeout"}
    if symbol in ADDABLE or symbol in digest.PORTFOLIO:
        name = ADDABLE.get(symbol) or (digest._load_json("raw_prices.json", "data/snapshot").get(symbol) or {}).get("name") or symbol
        return {"symbol": symbol, "state": "valid", "name": name, "last_close": 100.0}
    return {"symbol": symbol, "state": "not_found"}


def fake_search_companies(query):
    if query.lower().startswith("down"):
        raise RuntimeError("HTTP 429")
    q = query.lower()
    found = [{"symbol": s, "name": n.split(" (")[0]} for s, n in ADDABLE.items() if q in s.lower() or q in n.lower()]
    for symbol in digest.PORTFOLIO:
        name = (digest._load_json("raw_prices.json", "data/snapshot").get(symbol) or {}).get("name") or symbol
        if (q in symbol.lower() or q in name.lower()) and symbol not in ADDABLE:
            found.append({"symbol": symbol, "name": name})
    return found[:8]


llm_adapter.chat = fake_chat
prices.fetch_symbol = fake_fetch_symbol
prices.check_symbol = fake_check_symbol
prices.search_companies = fake_search_companies


def install_fake_research():
    """Generic research with a fake network and fake model. KPITTECH works; every other ticker is 'unknown'."""
    import time
    from finn import snapshot
    from finn.research import jobs, pacer, pipeline
    from tests import research_fakes as fk

    html = open("tests/fixtures/screener_kpit_reconstructed.html").read()
    ar_pages = snapshot.load_pages("KPIT_AR_2026")
    keep = {}
    for number in range(1, 153):
        keep[number] = {"page": number, "text": "Filler page " + str(number) + " about nothing in particular."}
    for number in (4, 56, 107, 110, 149, 152):
        keep[number] = [p for p in ar_pages if p["page"] == number][0]
    ar_pdf = fk.make_pdf_from_pages([keep[n] for n in sorted(keep)])
    call_pdf = fk.make_pdf_from_pages(snapshot.load_pages("KPIT_CONCALL_AUG2026"))
    ar_url = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/4d2467e3-e28a-472b-8492-7fd9e46e63c4.pdf"
    call_url = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d07f9c52-5c5a-443b-b239-6acd5e94b97a.pdf"

    def slow_page():
        time.sleep(2)
        return fk.FakeResponse(200, html.encode())

    routes = {"https://www.screener.in/company/KPITTECH/": slow_page,
              "https://www.bseindia.com/": fk.FakeResponse(200, b"<html></html>"),
              ar_url: fk.FakeResponse(200, ar_pdf), call_url: fk.FakeResponse(200, call_pdf)}

    def runner(symbol, config, callback=None):
        return pipeline.analyze(symbol, config, callback=callback, session=fk.FakeSession(routes),
                                resolver=fk.public_resolver, chat_fn=fk.make_fake_chat("good"),
                                pacer=pacer.TokenPacer(10 ** 9, 0))

    os.environ["FINN_CACHE_DIR"] = "/tmp/finn_ui_cache"
    import shutil
    shutil.rmtree("/tmp/finn_ui_cache", ignore_errors=True)
    main.RESEARCH = jobs.ResearchService(main.get_config, fallback_fn=main.snapshot_fallback, runner=runner,
                                         limiter=jobs.SlidingLimit(1000, 1000))


def install_fake_filings():
    """Fake NSE: slow enough to show progress. ITC has a new HIGH filing, TCS is a valid empty window, ICICIBANK is
    unverifiable-empty, SBIN fails. TCS/ITC/ICICIBANK/SBIN cover every state the UI must tell apart."""
    import time
    from datetime import datetime, timedelta, timezone
    from finn import announcements
    ist = timezone(timedelta(hours=5, minutes=30))
    stamp = (datetime.now(ist) - timedelta(hours=1)).strftime("%d-%b-%Y %H:%M:%S")

    def fake_fetch(symbol):
        time.sleep(0.4)
        if symbol in ("SBIN", "FAILCO"):
            return {"symbol": symbol, "verdict": "fetch_error", "detail": "ReadTimeout", "status": None}
        if symbol == "TCS":
            return {"symbol": symbol, "verdict": "valid_empty_window", "newest_filing_in_wide_window": "19-Oct-2026",
                    "widened_count": 45}
        if symbol == "ICICIBANK":
            return {"symbol": symbol, "verdict": "ambiguous_empty", "detail": "empty even over 90 days"}
        items = [{"an_dt": stamp, "desc": "Updates" if symbol != "ITC" else "Outcome of Board Meeting",
                  "attchmntText": "FAKE live filing for " + symbol + " used by the UI test.",
                  "attchmntFile": "https://nsearchives.nseindia.com/corporate/fake_" + symbol + ".pdf"}]
        return {"symbol": symbol, "verdict": "ok_nonempty", "items": items}

    os.makedirs("/tmp/finn_ui_cache", exist_ok=True)
    state_file = "/tmp/finn_ui_cache/announcements_ui.json"
    if os.path.exists(state_file):
        os.remove(state_file)
    main.FILINGS = announcements.FilingsRefresher(fetch_fn=fake_fetch, state_file=state_file, cooldown_s=0)


install_fake_research()
install_fake_filings()


@main.app.post("/__test/reset")
def reset_filings_for_test():
    """UI-test server only (this file is never imported by the real app)."""
    install_fake_filings()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=8765, log_level="warning")
