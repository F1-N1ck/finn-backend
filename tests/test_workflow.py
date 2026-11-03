"""Round 10: filings refresh states, digest merge, endpoints, guidance gate. No network; NSE is a fake function."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from finn import announcements, digest, guidance
import app.main as main

SNAP = "data/snapshot"


def verdict_items(symbol, stamp="03-Nov-2026 10:00:00", desc="Updates"):
    return {"symbol": symbol, "verdict": "ok_nonempty",
            "items": [{"an_dt": stamp, "desc": desc, "attchmntText": "text for " + symbol,
                       "attchmntFile": "https://nsearchives.nseindia.com/corporate/x_" + symbol + ".pdf"}]}


def make_refresher(tmp_path, fetch, **kw):
    return announcements.FilingsRefresher(fetch_fn=fetch, state_file=str(tmp_path / "ann.json"),
                                          cooldown_s=kw.pop("cooldown_s", 0), **kw)


def test_normalise_item_maps_fields_and_drops_foreign_links():
    item = announcements.normalise_item({"an_dt": "x", "desc": " D ", "attchmntText": "t" * 900,
                                         "attchmntFile": "https://evil.example/a.pdf"})
    assert item["desc"] == "D" and len(item["text"]) == 400 and item["file"] is None
    ok = announcements.normalise_item({"an_dt": "x", "desc": "d", "attchmntText": "t",
                                       "attchmntFile": "https://nsearchives.nseindia.com/corporate/a.pdf"})
    assert ok["file"].startswith("https://nsearchives.nseindia.com/")


def test_every_state_is_kept_apart(tmp_path):
    def fetch(symbol):
        if symbol == "TCS":
            return {"symbol": symbol, "verdict": "valid_empty_window", "newest_filing_in_wide_window": "19-Oct-2026",
                    "widened_count": 45}
        if symbol == "ITC":
            return {"symbol": symbol, "verdict": "ambiguous_empty", "detail": "empty even over 90 days"}
        if symbol == "SBIN":
            return {"symbol": symbol, "verdict": "fetch_error", "detail": "ReadTimeout", "status": None}
        if symbol == "LT":
            raise RuntimeError("boom")
        return verdict_items(symbol)
    refresher = make_refresher(tmp_path, fetch)
    run = refresher.run()
    assert run["total"] == 16 and run["failed"] == 2 and set(run["failed_symbols"]) == {"SBIN", "LT"}
    result = digest.build_digest(SNAP, live_state=refresher.live_state())
    states = result["filings_status"]["symbols"]
    assert states["TCS"]["state"] == "valid_empty" and states["ITC"]["state"] == "ambiguous_empty"
    assert states["SBIN"]["state"] == "failed_snapshot" and states["RELIANCE"]["state"] == "live"
    assert result["filings_status"]["mode"] == "partial"
    by_symbol = {p["symbol"]: p for p in result["portfolio"]}
    assert "No filings in the last 7 days" in by_symbol["TCS"]["no_filings_note"]
    assert "cannot be read as 'no filings'" in by_symbol["ITC"]["no_filings_note"]
    assert "fetch_error" in states["SBIN"]["detail"] and states["LT"]["detail"] == "RuntimeError"
    assert by_symbol["SBIN"]["filings_state"] == "failed_snapshot"


def test_live_items_replace_snapshot_inside_window_and_keep_older_snapshot(tmp_path):
    refresher = make_refresher(tmp_path, lambda s: verdict_items(s))
    refresher.run()
    result = digest.build_digest(SNAP, live_state=refresher.live_state())
    rel = [f for f in result["filings"] if f["symbol"] == "RELIANCE"]
    assert any(f["source"] == "live" for f in rel)
    # the live window is 27 Oct to 3 Nov: snapshot items from 25 and 26 Oct are older and stay, labelled snapshot
    snap = [f for f in rel if f["source"] == "snapshot"]
    assert all(f["at"] < "2026-10-27" for f in snap)
    live_row = [f for f in rel if f["source"] == "live"][0]
    assert live_row["fetched_utc"] and live_row["url"].startswith("https://nsearchives.nseindia.com/")


def test_failed_refresh_keeps_last_good_data_dated(tmp_path):
    calls = {"n": 0}

    def fetch(symbol):
        calls["n"] += 1
        if calls["n"] > 16:
            return {"symbol": symbol, "verdict": "fetch_error", "detail": "ConnectTimeout", "status": None}
        return verdict_items(symbol)
    refresher = make_refresher(tmp_path, fetch)
    refresher.run()
    first_success = refresher.live_state()["last_success_utc"]
    second = refresher.run()
    assert second["ok"] == 0 and second["failed"] == 16
    state = refresher.live_state()
    assert state["last_success_utc"] == first_success  # a total failure never moves the success time
    result = digest.build_digest(SNAP, live_state=state)
    summary = result["filings_status"]
    assert summary["mode"] == "refresh_failed" and summary["last_success_utc"] == first_success
    assert summary["symbols"]["RELIANCE"]["state"] == "stale_live"
    assert any(f["source"] == "live" for f in result["filings"])  # the older live data is still shown


def test_state_survives_restart_and_corrupt_file_falls_back(tmp_path):
    refresher = make_refresher(tmp_path, lambda s: verdict_items(s))
    refresher.run()
    again = make_refresher(tmp_path, lambda s: verdict_items(s))
    assert again.live_state()["last_success_utc"] == refresher.live_state()["last_success_utc"]
    (tmp_path / "ann.json").write_text("{not json")
    broken = make_refresher(tmp_path, lambda s: verdict_items(s))
    assert broken.live_state()["last_success_utc"] is None
    assert digest.build_digest(SNAP, live_state=broken.live_state())["filings_status"]["mode"] == "snapshot"


def test_snapshot_digest_is_unchanged_without_live_state():
    result = digest.build_digest(SNAP)
    assert result["source_type"] == "snapshot" and result["as_of_utc"] == "2026-10-31T18:17:41Z"
    assert all(f["source"] == "snapshot" for f in result["filings"])
    assert result["filings_status"]["mode"] == "snapshot"


def test_deadline_marks_remaining_symbols_failed(tmp_path):
    t = {"now": 0.0}

    def clock():
        return t["now"]

    def fetch(symbol):
        t["now"] += 100.0
        return verdict_items(symbol)
    refresher = make_refresher(tmp_path, fetch, workers=1, deadline_s=250, clock=clock)
    run = refresher.run()
    assert run["ok"] == 3 and run["failed"] == 13


def test_start_refuses_while_running_and_inside_cooldown(tmp_path):
    import threading
    gate = threading.Event()

    def fetch(symbol):
        gate.wait(5)
        return verdict_items(symbol)
    refresher = make_refresher(tmp_path, fetch, cooldown_s=30)
    started, _ = refresher.start()
    assert started
    assert refresher.start() == (False, "already_running")
    assert refresher.status()["running"] is True
    gate.set()
    for _ in range(100):
        if not refresher.status()["running"]:
            break
        import time
        time.sleep(0.05)
    assert refresher.start() == (False, "too_soon")


def test_endpoints_and_no_proxy_surface(tmp_path):
    refresher = make_refresher(tmp_path, lambda s: verdict_items(s))
    old = main.FILINGS
    main.FILINGS = refresher
    try:
        client = TestClient(main.app)
        body = client.get("/api/digest").json()
        assert body["filings_status"]["mode"] == "snapshot"
        refresher.run()
        body = client.get("/api/digest").json()
        assert body["filings_status"]["mode"] == "live" and body["filings_status"]["last_success_utc"]
        assert client.get("/api/filings/status").json()["running"] is False
        # the refresh endpoint accepts only a list of valid tickers: a url or any other field is rejected, not ignored
        refresher.cooldown_s = 0
        response = client.post("/api/filings/refresh?symbol=EVIL&url=https://evil.example", json={"url": "x"})
        assert response.status_code == 422
        response = client.post("/api/filings/refresh", json={"symbols": ["TCS"], "url": "https://evil.example"})
        assert response.status_code == 422
        response = client.post("/api/filings/refresh", json={"symbols": ["TCS", "http://evil.example/x"]})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_symbol"
        response = client.post("/api/filings/refresh")
        assert response.status_code == 202
        assert set(refresher.symbols) == set(digest.PORTFOLIO)
    finally:
        main.FILINGS = old


def test_default_fetch_uses_no_cookie_warmup(monkeypatch):
    seen = {}

    def fake_make_session(warm=True):
        seen["warm"] = warm
        raise RuntimeError("stop here")
    monkeypatch.setattr(announcements.nse, "make_session", fake_make_session)
    with pytest.raises(RuntimeError):
        announcements.default_fetch("TCS")
    assert seen["warm"] is False


# ---------------------------------------------------------------- guidance gate

def side(**over):
    base = {"metric": {"key": "ebitda_margin", "label": "EBITDA margin", "basis": "stated"},
            "period": {"key": "q1_fy27", "label": "Q1 FY27"}, "unit": {"key": "percent", "label": "%"},
            "scope": {"key": "consolidated", "label": "consolidated"}}
    base.update(over)
    return base


def test_gate_comparable_only_when_all_four_match_and_are_stated():
    g = side(low=22.5, high=23.5)
    o = side(value=23.0)
    assert guidance.compare(g, o) == {"verdict": "comparable", "reasons": [], "caveats": [], "position": "within"}
    assert guidance.compare(g, side(value=24.0))["position"] == "above"
    assert guidance.compare(g, side(value=22.0))["position"] == "below"


@pytest.mark.parametrize("field,other", [
    ("metric", {"key": "ebit_margin", "label": "EBIT margin"}),
    ("period", {"key": "fy27", "label": "FY27"}),
    ("unit", {"key": "inr_crore", "label": "INR crore"}),
    ("scope", {"key": "standalone", "label": "standalone"}),
    ("scope", {"key": "not_stated", "label": "not stated"}),
])
def test_gate_refuses_any_single_mismatch_and_does_no_arithmetic(field, other):
    g = side(low=22.5, high=23.5)
    o = side(value=23.0, **{field: other})
    verdict = guidance.compare(g, o)
    assert verdict["verdict"] == "not_directly_comparable" and verdict["position"] is None
    assert [r["field"] for r in verdict["reasons"]] == [field]


def test_inferred_metric_is_a_caveat_not_a_silent_match():
    g = side(low=22.5, high=23.5, metric={"key": "ebitda_margin", "label": "m", "basis": "inferred", "basis_note": "why"})
    verdict = guidance.compare(g, side(value=23.0))
    assert verdict["verdict"] == "comparable" and "inference" in verdict["caveats"][0]


def test_hul_example_is_withheld_for_period_and_scope_and_quotes_are_verbatim():
    result = guidance.evaluate_all()
    assert len(result["examples"]) == 1
    ex = result["examples"][0]
    assert ex["gate"]["verdict"] == "not_directly_comparable" and ex["gate"]["position"] is None
    assert {r["field"] for r in ex["gate"]["reasons"]} == {"period", "scope"}
    assert ex["guidance"]["quote_verbatim_in_stored_page"] and ex["outcome"]["quote_verbatim_in_stored_page"]
    assert all(e["verbatim"] for e in ex["guidance"]["extra_quotes"])
    assert ex["guidance"]["pdf_page"] == 14 and ex["outcome"]["pdf_page"] == 6
    assert ex["guidance"]["url"].startswith("https://www.bseindia.com/") and ex["outcome"]["url"].startswith("https://www.bseindia.com/")
    assert ex["guidance"]["call_date"] == "2026-04-30" and ex["outcome"]["call_date"] == "2026-07-28"


def test_altered_quote_withholds_the_comparison(tmp_path, monkeypatch):
    data = json.load(open(os.path.join(SNAP, "guidance_outcome.json")))
    data["examples"][0]["outcome"]["quote"] = "delivering EBITDA Margin of 24%"
    os.makedirs(tmp_path / "s")
    json.dump(data, open(tmp_path / "s" / "guidance_outcome.json", "w"))
    (tmp_path / "s" / "guidance_sources.json").write_text(open(os.path.join(SNAP, "guidance_sources.json")).read())
    monkeypatch.setenv("FINN_SNAPSHOT_DIR", str(tmp_path / "s"))
    ex = guidance.evaluate_all()["examples"][0]
    assert not ex["outcome"]["quote_verbatim_in_stored_page"]
    assert [r["field"] for r in ex["gate"]["reasons"]] == ["quote"]


def test_guidance_endpoint():
    body = TestClient(main.app).get("/api/guidance").json()
    assert body["examples"][0]["symbol"] == "HINDUNILVR" and body["not_verified"]


def test_questions_and_cached_research_carry_source_links():
    """The redesigned UI links every saved answer to its original PDF; the link comes from the snapshot manifest."""
    from fastapi.testclient import TestClient
    import app.main as main
    client = TestClient(main.app)
    questions = client.get("/api/questions").json()["questions"]
    assert questions
    for question in questions:
        assert question["source_url"].startswith("https://www.bseindia.com/")
        record = client.post("/api/research", json={"question_id": question["id"], "mode": "cached"}).json()
        assert record["source_url"] == question["source_url"]
        assert record["doc"] == question["doc"] and record["short"] == question["short"]
        assert record["source_type"] == "operator_extract"  # never presented as model output
