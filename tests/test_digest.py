"""Digest, tiers, price stats, live-price parsing and the new endpoints. No network."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from finn import digest, grounded_qa, prices
import app.main as main

client = TestClient(main.app)


def test_tier_rules_and_reasons():
    assert digest.tier_for("Outcome of Board Meeting", "")[0] == "high"
    assert digest.tier_for("Copy of Newspaper Publication", "")[0] == "low"
    assert digest.tier_for("Press Release", "")[0] == "medium"
    tier, reason = digest.tier_for("General Updates", "disclosure ... Change in Managing Director")
    assert tier == "high" and "managing director" in reason
    assert digest.tier_for("Something New", "")[0] == "medium"


def test_price_stats_uses_prior_20_bars_not_the_last():
    bars = []
    for day in range(21):
        bars.append(["2026-10-%02d" % (day + 1), 100.0, 1000])
    bars.append(["2026-10-30", 103.0, 5000])
    stats = digest.price_stats(bars)
    assert stats["change_pct"] == 3.0
    assert stats["avg_volume_20d"] == 1000
    assert stats["volume_ratio"] == 5.0


def test_price_stats_needs_enough_bars_for_ratio():
    bars = [["2026-10-01", 100.0, 10], ["2026-10-02", 101.0, 10]]
    stats = digest.price_stats(bars)
    assert stats["volume_ratio"] is None
    assert digest.price_stats([["2026-10-01", 1, 1]]) is None


def test_duplicate_filings_collapse_with_count():
    raw = {"RELIANCE": {"items": [
        {"an_dt": "30-Oct-2026 19:40:11", "desc": "Outcome of Board Meeting", "text": "same", "file": "a.pdf"},
        {"an_dt": "30-Oct-2026 19:40:11", "desc": "Outcome of Board Meeting", "text": "same", "file": "b.pdf"}]}}
    filings = digest.build_filings(raw)
    assert len(filings) == 1
    assert filings[0]["dup_count"] == 2
    assert filings[0]["at"].endswith("+05:30")


def test_real_snapshot_digest_facts():
    result = digest.build_digest(questions=grounded_qa.QUESTIONS)
    assert result["source_type"] == "snapshot"
    assert len(result["portfolio"]) == 16
    by_symbol = {}
    for entry in result["portfolio"]:
        by_symbol[entry["symbol"]] = entry
    assert by_symbol["KPITTECH"]["has_research"] is True
    assert by_symbol["TCS"]["has_research"] is False
    assert "45 filings" in by_symbol["TCS"]["no_filings_note"]
    assert "14 filings" in by_symbol["ITC"]["no_filings_note"]
    assert by_symbol["KPITTECH"]["close"] == 530.25
    assert by_symbol["SUNPHARMA"]["volume_ratio"] > 3
    assert len(result["filings"]) == 80  # 81 raw items, 1 exact duplicate collapsed


def test_digest_endpoint_and_root_page():
    response = client.get("/api/digest")
    assert response.status_code == 200
    assert response.json()["source_type"] == "snapshot"
    page = client.get("/")
    assert page.status_code == 200
    assert "FINN" in page.text
    assert client.get("/static/app.js").status_code == 200


class FakeYahooResponse:
    status_code = 200

    def json(self):
        stamps = []
        closes = []
        volumes = []
        base = 1761782400  # 30 Oct 2026 00:00 UTC
        for index in range(25):
            stamps.append(base - (24 - index) * 86400)
            closes.append(100.0 + index)
            volumes.append(1000 + index)
        closes[10] = None
        return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{"close": closes, "volume": volumes}]}}]}}


def test_live_fetch_parses_and_skips_null_bars(monkeypatch):
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: FakeYahooResponse())
    stats = prices.fetch_symbol("TCS")
    assert stats["ok"] is True
    assert stats["symbol"] == "TCS"
    assert stats["close"] == 124.0


def test_live_fetch_rejects_symbols_outside_portfolio(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call Yahoo for an unknown symbol")
    monkeypatch.setattr(prices.requests, "get", boom)
    assert prices.fetch_symbol("EVIL/../x")["ok"] is False
    assert prices.fetch_symbol("NOTINPORTFOLIO")["ok"] is False


def test_live_fetch_reports_http_errors_without_raising(monkeypatch):
    class Blocked:
        status_code = 429

        def json(self):
            return {}
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Blocked())
    result = prices.fetch_symbol("TCS")
    assert result == {"symbol": "TCS", "ok": False, "error": "HTTP 429"}


def test_refresh_endpoint_503_when_all_symbols_fail(monkeypatch):
    prices._cache["data"] = None
    prices._cache["at"] = 0.0
    main.LAST_PRICE_REFRESH["at"] = 0.0
    monkeypatch.setattr(prices, "fetch_symbol", lambda symbol, now=None: {"symbol": symbol, "ok": False, "error": "x"})
    response = client.post("/api/prices/refresh")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "live_prices_unavailable"


def test_refresh_endpoint_ok_and_cached(monkeypatch):
    prices._cache["data"] = None
    prices._cache["at"] = 0.0
    main.LAST_PRICE_REFRESH["at"] = 0.0
    calls = {"n": 0}

    def fake(symbol, now=None):
        calls["n"] = calls["n"] + 1
        return {"symbol": symbol, "ok": True, "close": 1.0}
    monkeypatch.setattr(prices, "fetch_symbol", fake)
    first = client.post("/api/prices/refresh")
    second = client.post("/api/prices/refresh")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["ok_count"] == 16
    assert calls["n"] == 16  # second call served from the 120 s cache
    prices._cache["data"] = None
