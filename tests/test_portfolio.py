"""Per-user portfolios: the server is stateless about selections. Network (Yahoo, NSE) is faked."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from finn import announcements, digest, prices
import app.main as main

SNAP = "data/snapshot"


def live(symbol, stamp="03-Nov-2026 10:00:00"):
    return {"symbol": symbol, "verdict": "ok_nonempty",
            "items": [{"an_dt": stamp, "desc": "Updates", "attchmntText": "t " + symbol,
                       "attchmntFile": "https://nsearchives.nseindia.com/corporate/x_" + symbol + ".pdf"}]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    prices.clear_cache()
    refresher = announcements.FilingsRefresher(fetch_fn=lambda s: live(s), state_file=str(tmp_path / "a.json"), cooldown_s=0)
    monkeypatch.setattr(main, "FILINGS", refresher)
    monkeypatch.setattr(main, "refresher_for_test", refresher, raising=False)
    yield TestClient(main.app)
    prices.clear_cache()


def wait_idle(refresher):
    for _ in range(200):
        if not refresher.status()["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("refresher did not finish")


def test_digest_for_a_selection_contains_only_those_companies(client):
    body = client.post("/api/digest", json={"symbols": ["tcs", "RELIANCE", "TCS"]}).json()
    assert [p["symbol"] for p in body["portfolio"]] == ["TCS", "RELIANCE"]  # upper-cased, de-duplicated, ordered
    assert {f["symbol"] for f in body["filings"]} <= {"TCS", "RELIANCE"}
    assert body["filings_status"]["mode"] == "snapshot"
    assert all(p["in_snapshot"] and p["price_source"] == "snapshot" for p in body["portfolio"])


def test_unknown_company_is_unchecked_not_no_filings(client):
    body = client.post("/api/digest", json={"symbols": ["TCS", "PIDILITIND", "ZOMATO"]}).json()
    zomato = [p for p in body["portfolio"] if p["symbol"] == "ZOMATO"][0]
    assert zomato["filings_state"] == "unchecked" and zomato["filings_count"] == 0
    assert "Not checked yet" in zomato["no_filings_note"] and "No filings" not in zomato["no_filings_note"]
    assert zomato["price_source"] == "none"
    assert "close" not in zomato  # no price is shown for a company that has not been checked
    assert body["filings_status"]["symbols"]["ZOMATO"]["state"] == "unchecked"


def test_only_unchecked_companies_means_mode_unchecked_and_no_as_of(client):
    body = client.post("/api/digest", json={"symbols": ["ZOMATO"]}).json()
    assert body["filings_status"]["mode"] == "unchecked" and body["as_of_utc"] is None


def test_empty_selection_is_an_empty_digest(client):
    body = client.post("/api/digest", json={"symbols": []}).json()
    assert body["portfolio"] == [] and body["filings"] == [] and body["filings_status"]["mode"] == "empty"


def test_refresh_for_selection_fetches_only_those_and_shows_them_live(client):
    refresher = main.FILINGS
    seen = []
    refresher.fetch_fn = lambda s: (seen.append(s), live(s))[1]
    response = client.post("/api/filings/refresh", json={"symbols": ["ZOMATO", "TCS"]})
    assert response.status_code == 202 and response.json()["started"] is True
    wait_idle(refresher)
    assert sorted(seen) == ["TCS", "ZOMATO"]
    body = client.post("/api/digest", json={"symbols": ["ZOMATO", "TCS", "ITC"]}).json()
    states = body["filings_status"]["symbols"]
    assert states["ZOMATO"]["state"] == "live" and states["TCS"]["state"] == "live" and states["ITC"]["state"] == "snapshot"
    assert body["filings_status"]["mode"] == "partial"
    assert any(f["symbol"] == "ZOMATO" and f["source"] == "live" for f in body["filings"])


def test_one_users_refresh_does_not_change_anothers_view(client):
    refresher = main.FILINGS
    client.post("/api/filings/refresh", json={"symbols": ["ZOMATO"]})
    wait_idle(refresher)
    other = client.post("/api/digest", json={"symbols": ["TCS", "INFY"]}).json()
    assert other["filings_status"]["mode"] == "snapshot"
    assert {p["symbol"] for p in other["portfolio"]} == {"TCS", "INFY"}
    shared = client.get("/api/digest").json()  # the shared demo digest keeps its own 16 companies
    assert len(shared["portfolio"]) == 16 and "ZOMATO" not in {p["symbol"] for p in shared["portfolio"]}
    assert shared["filings_status"]["mode"] == "snapshot"


def test_failed_source_for_an_added_company_is_failed_not_empty(client):
    refresher = main.FILINGS
    refresher.fetch_fn = lambda s: {"symbol": s, "verdict": "fetch_error", "detail": "ReadTimeout", "status": None}
    client.post("/api/filings/refresh", json={"symbols": ["ZOMATO", "TCS"]})
    wait_idle(refresher)
    body = client.post("/api/digest", json={"symbols": ["ZOMATO", "TCS"]}).json()
    states = body["filings_status"]["symbols"]
    assert states["ZOMATO"]["state"] == "failed" and states["TCS"]["state"] == "failed_snapshot"
    zomato = [p for p in body["portfolio"] if p["symbol"] == "ZOMATO"][0]
    assert "failed" in zomato["no_filings_note"] and "No filings" not in zomato["no_filings_note"]
    assert body["filings_status"]["mode"] == "refresh_failed"
    assert body["filings_status"]["last_run"]["failed_symbols"] == ["ZOMATO", "TCS"]


def test_symbols_already_queued_are_not_queued_twice_and_late_requests_join(client):
    refresher = main.FILINGS
    gate = threading.Event()
    seen = []

    def slow(symbol):
        gate.wait(5)
        seen.append(symbol)
        return live(symbol)
    refresher.fetch_fn = slow
    assert refresher.start(["A1", "A2"]) == (True, None)
    assert refresher.start(["A1"]) == (False, "already_running")
    assert refresher.start(["A2", "B1"]) == (True, None)  # B1 joins the running refresh, A2 is not repeated
    status = client.get("/api/filings/status?symbols=B1,A1").json()
    assert status["running"] is True and status["inflight"] == ["A1", "B1"]
    assert client.get("/api/filings/status?symbols=TCS").json()["running"] is False
    gate.set()
    wait_idle(refresher)
    assert sorted(seen) == ["A1", "A2", "B1"]


def test_input_validation_and_no_proxy_surface(client):
    assert client.post("/api/digest", json={"symbols": ["TCS", "https://evil.example"]}).status_code == 422
    assert client.post("/api/digest", json={"symbols": ["TCS"], "url": "https://evil.example"}).status_code == 422
    assert client.post("/api/digest", json={"symbols": ["T%d" % i for i in range(41)]}).status_code == 422
    assert client.post("/api/prices/refresh", json={"symbols": ["../etc"]}).status_code == 422
    assert client.post("/api/prices/refresh", json={"symbols": []}).status_code == 422
    assert client.get("/api/symbols/check?symbol=a/b").status_code == 422
    assert client.get("/api/symbols/search?q=<script>").status_code == 422
    assert client.get("/api/symbols/search?q=a").status_code == 422


def test_price_refresh_for_a_selection_and_per_symbol_cache(client, monkeypatch):
    calls = []

    def fake(symbol, now=None):
        calls.append(symbol)
        return {"symbol": symbol, "ok": True, "close": 10.0, "name": symbol + " Ltd", "last_date": "2026-11-03"}
    monkeypatch.setattr(prices, "fetch_symbol", fake)
    first = client.post("/api/prices/refresh", json={"symbols": ["ZOMATO", "TCS"]}).json()
    assert first["ok_count"] == 2 and sorted(calls) == ["TCS", "ZOMATO"]
    client.post("/api/prices/refresh", json={"symbols": ["ZOMATO", "INFY"]})
    assert sorted(calls) == ["INFY", "TCS", "ZOMATO"]  # ZOMATO came from the per-symbol cache
    body = client.post("/api/digest", json={"symbols": ["ZOMATO"]}).json()
    row = body["portfolio"][0]
    assert row["price_source"] == "live_cache" and row["close"] == 10.0 and row["name"] == "ZOMATO Ltd"
    assert row["price_fetched_utc"]


def test_price_refresh_failure_codes(client, monkeypatch):
    monkeypatch.setattr(prices, "fetch_symbol", lambda symbol, now=None: {"symbol": symbol, "ok": False, "error": "HTTP 500"})
    response = client.post("/api/prices/refresh", json={"symbols": ["ZOMATO"]})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "live_prices_unavailable"
    monkeypatch.setattr(prices, "allow_calls", lambda n: False)
    response = client.post("/api/prices/refresh", json={"symbols": ["ZOMATO"]})
    assert response.status_code == 429


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def chart(exchange="NSI", kind="EQUITY", name="Eternal Limited"):
    return {"chart": {"result": [{"meta": {"exchangeName": exchange, "instrumentType": kind, "longName": name},
                                  "indicators": {"quote": [{"close": [100.0, 101.5]}]}}]}}


def test_check_symbol_states(monkeypatch):
    prices.clear_cache()
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(200, chart()))
    ok = prices.check_symbol("ETERNAL")
    assert ok["state"] == "valid" and ok["name"] == "Eternal Limited" and ok["last_close"] == 101.5
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(404, {}))
    assert prices.check_symbol("NOPE")["state"] == "not_found"
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(200, chart(exchange="NMS")))
    assert prices.check_symbol("AAPL")["state"] == "not_found"  # found, but not NSE
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(200, chart(kind="INDEX")))
    assert prices.check_symbol("NIFTY")["state"] == "not_found"
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(503, {}))
    assert prices.check_symbol("TCS")["state"] == "check_failed"  # could not tell: must not be added

    def boom(*a, **k):
        raise prices.requests.exceptions.ConnectTimeout()
    monkeypatch.setattr(prices.requests, "get", boom)
    assert prices.check_symbol("TCS") == {"symbol": "TCS", "state": "check_failed", "error": "ConnectTimeout"}
    prices.clear_cache()


def test_check_endpoint_and_search(client, monkeypatch):
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(200, chart()))
    assert client.get("/api/symbols/check?symbol=eternal").json()["state"] == "valid"
    quotes = {"quotes": [{"symbol": "ETERNAL.NS", "quoteType": "EQUITY", "longname": "Eternal Limited"},
                         {"symbol": "ETERNAL.BO", "quoteType": "EQUITY", "longname": "Eternal BSE"},
                         {"symbol": "ETE", "quoteType": "EQUITY", "shortname": "US thing"},
                         {"symbol": "NIFTY.NS", "quoteType": "INDEX"}]}
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(200, quotes))
    body = client.get("/api/symbols/search?q=eternal").json()
    assert body["results"] == [{"symbol": "ETERNAL", "name": "Eternal Limited"}]
    monkeypatch.setattr(prices.requests, "get", lambda *a, **k: Resp(429, {}))
    response = client.get("/api/symbols/search?q=eternal")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "search_unavailable"


def test_global_fetch_limit_is_enforced(monkeypatch):
    prices.clear_cache()
    assert prices.allow_calls(prices.FETCH_LIMIT_PER_MIN) is True
    assert prices.allow_calls(1) is False
    prices.clear_cache()
    assert prices.allow_calls(1) is True
    prices.clear_cache()


def test_demo_portfolio_endpoint_lists_the_shared_starting_point():
    from fastapi.testclient import TestClient
    import app.main as m
    r = TestClient(m.app).get("/api/portfolio/demo")
    assert r.status_code == 200
    body = r.json()
    assert [s["symbol"] for s in body["symbols"]] == list(digest.PORTFOLIO)
    assert len(body["symbols"]) == 16
    assert r.headers["cache-control"] == "no-store"
