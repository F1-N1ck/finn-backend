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


def fake_fetch_symbol(symbol, now=None):
    raw = digest._load_json("raw_prices.json", "data/snapshot")
    stats = digest.price_stats(raw[symbol]["bars"])
    stats["close"] = round(stats["close"] * 1.01, 2)
    stats["change_pct"] = 1.5
    stats["partial"] = False
    stats["symbol"] = symbol
    stats["ok"] = True
    return stats


llm_adapter.chat = fake_chat
prices.fetch_symbol = fake_fetch_symbol


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


install_fake_research()

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=8765, log_level="warning")
