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

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=8765, log_level="warning")
