"""App tests. The model is mocked: NO network call, NO real key. Run:  python -m pytest tests -q"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from finn import llm_adapter, grounded_qa
import app.main as main

SECRET = "sk-test-SECRET-1234567890"
ADMIN = "admin-token-ABCDEF"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ["FINN_LLM_API_KEY", "FINN_LLM_BASE_URL", "FINN_LLM_MODEL", "FINN_ADMIN_TOKEN",
                 "FINN_ENABLE_HOST_CHECK", "FINN_LLM_EXTRA_BODY"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FINN_SNAPSHOT_DIR", "data/snapshot")
    main.LIVE_BUDGET = main.budget.LiveBudget(2, 40)
    del main.ADMIN_FAILURES[:]
    main.JOBS.clear()


def configure_live(monkeypatch):
    monkeypatch.setenv("FINN_LLM_API_KEY", SECRET)
    monkeypatch.setenv("FINN_LLM_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("FINN_LLM_MODEL", "test-model")


def fake_chat_factory(reply_builder):
    def fake_chat(config, messages):
        user_text = messages[-1]["content"]
        return {"text": json.dumps(reply_builder(user_text)), "usage": {}, "latency_s": 0.1,
                "attempts": 1, "rate_limit_headers": {}, "cost_usd_estimate": None}
    return fake_chat


def honest_reply(user_text):
    # quote the first line of S1 verbatim from the prompt
    after = user_text.split("[S1]", 1)[1]
    body = after.split("\n", 1)[1]
    quote = body.strip().split("\n")[0]
    return {"answer": "see source", "not_stated": False,
            "claims": [{"text": "a claim", "source": "S1", "quote": quote}]}


def fabricated_reply(user_text):
    return {"answer": "Revenue was 99,999.99", "not_stated": False,
            "claims": [{"text": "Revenue 99,999.99", "source": "S1", "quote": "this quote is invented"}]}


client = TestClient(main.app)


def test_healthz_has_no_secrets(monkeypatch):
    configure_live(monkeypatch)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["live_model_configured"] is True
    assert body["snapshot_ok"] is True
    assert SECRET not in response.text


def test_docs_disabled():
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_questions_list_labels_cached():
    body = client.get("/api/questions").json()
    ids = [q["id"] for q in body["questions"]]
    assert "Q1_concall_guidance" in ids
    for q in body["questions"]:
        assert q["cached"]["label"].startswith("CACHED demo result")
        assert "NOT model output" in q["cached"]["label"]


def test_unknown_question_404():
    response = client.post("/api/research", json={"question_id": "nope"})
    assert response.status_code == 404


def test_cached_mode_never_calls_model(monkeypatch):
    configure_live(monkeypatch)

    def boom(config, messages):
        raise AssertionError("model must not be called in cached mode")
    monkeypatch.setattr(llm_adapter, "chat", boom)
    response = client.post("/api/research", json={"question_id": "Q2_ar_headline_numbers", "mode": "cached"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "cached"
    assert body["label"].startswith("CACHED")
    assert body["generated_at"]


def test_auto_without_key_falls_back_to_labelled_cache():
    response = client.post("/api/research", json={"question_id": "Q1_concall_guidance"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "cached"
    assert body["fallback_reason"]["code"] == "config_missing"


def test_live_without_key_is_an_error_not_silent_cache():
    response = client.post("/api/research", json={"question_id": "Q1_concall_guidance", "mode": "live"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "config_missing"


def test_live_success_is_labelled_live(monkeypatch):
    configure_live(monkeypatch)
    monkeypatch.setattr(llm_adapter, "chat", fake_chat_factory(honest_reply))
    response = client.post("/api/research", json={"question_id": "Q1_concall_guidance", "mode": "live"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "live"
    assert body["label"].startswith("LIVE analysis")
    assert body["auto_check"]["auto_pass_count"] == 1
    assert SECRET not in response.text


def test_fabricated_answer_is_flagged_not_hidden(monkeypatch):
    configure_live(monkeypatch)
    monkeypatch.setattr(llm_adapter, "chat", fake_chat_factory(fabricated_reply))
    response = client.post("/api/research", json={"question_id": "Q2_ar_headline_numbers", "mode": "live"})
    assert response.status_code == 200
    body = response.json()
    assert body["auto_check"]["auto_pass_count"] == 0
    assert "99999.99" in body["auto_check"]["answer_numbers_not_in_passages"] or \
        "99,999.99".replace(",", "") in body["auto_check"]["answer_numbers_not_in_passages"]
    assert "0/1 claims passed" in body["label"]


def test_provider_error_in_auto_falls_back(monkeypatch):
    configure_live(monkeypatch)

    def failing(config, messages):
        raise llm_adapter.LLMRateLimited("provider said slow down")
    monkeypatch.setattr(llm_adapter, "chat", failing)
    response = client.post("/api/research", json={"question_id": "Q2_ar_headline_numbers"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "cached"
    assert body["fallback_reason"]["code"] == "rate_limited"


def test_budget_exhaustion_falls_back(monkeypatch):
    configure_live(monkeypatch)
    monkeypatch.setattr(llm_adapter, "chat", fake_chat_factory(honest_reply))
    main.LIVE_BUDGET = main.budget.LiveBudget(1, 40)
    first = client.post("/api/research", json={"question_id": "Q1_concall_guidance"}).json()
    second = client.post("/api/research", json={"question_id": "Q1_concall_guidance"}).json()
    assert first["mode"] == "live"
    assert second["mode"] == "cached"
    assert second["fallback_reason"]["code"] == "live_budget_exhausted_per_minute"


def test_bad_extra_body_does_not_crash(monkeypatch):
    monkeypatch.setenv("FINN_LLM_EXTRA_BODY", "{not json")
    assert client.get("/healthz").status_code == 200


# ------------------------------------------------------------ admin

def test_admin_disabled_by_default_is_404():
    assert client.post("/admin/host-check").status_code == 404
    assert client.get("/admin/host-check/abc").status_code == 404


def test_admin_enabled_but_no_token_set_is_404(monkeypatch):
    monkeypatch.setenv("FINN_ENABLE_HOST_CHECK", "1")
    assert client.post("/admin/host-check").status_code == 404


def test_admin_wrong_token_401_then_lockout(monkeypatch):
    monkeypatch.setenv("FINN_ENABLE_HOST_CHECK", "1")
    monkeypatch.setenv("FINN_ADMIN_TOKEN", ADMIN)
    for _ in range(5):
        assert client.post("/admin/host-check", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # even the right token is locked out now
    locked = client.post("/admin/host-check", headers={"Authorization": "Bearer " + ADMIN})
    assert locked.status_code == 429


def test_admin_token_in_query_string_is_not_accepted(monkeypatch):
    monkeypatch.setenv("FINN_ENABLE_HOST_CHECK", "1")
    monkeypatch.setenv("FINN_ADMIN_TOKEN", ADMIN)
    assert client.post("/admin/host-check?token=" + ADMIN).status_code == 401


def test_admin_starts_job_and_second_start_is_throttled(monkeypatch):
    monkeypatch.setenv("FINN_ENABLE_HOST_CHECK", "1")
    monkeypatch.setenv("FINN_ADMIN_TOKEN", ADMIN)
    configure_live(monkeypatch)
    monkeypatch.setattr(main.hostcheck, "run_all",
                        lambda include_model, config, directory, cache_path: {"fake": True, "model": include_model})
    headers = {"Authorization": "Bearer " + ADMIN}
    start = client.post("/admin/host-check?model=0", headers=headers)
    assert start.status_code == 202
    job_id = start.json()["job_id"]
    import time
    for _ in range(50):
        poll = client.get("/admin/host-check/" + job_id, headers=headers)
        if poll.json()["status"] != "running":
            break
        time.sleep(0.05)
    body = poll.json()
    assert body["status"] == "done"
    assert body["report"] == {"fake": True, "model": False}
    assert SECRET not in poll.text and ADMIN not in poll.text
    again = client.post("/admin/host-check", headers=headers)
    assert again.status_code == 429


# ------------------------------------------------------------ retrieval regression

def test_frequent_speaker_label_is_not_stripped_as_header():
    from finn import retrieval
    pages = []
    for number in range(1, 7):
        text = ("KPIT Technologies Limited\nJuly 29, 2026\nPage " + str(number) + " of 6\n"
                "Kishor Patil:\nWe will return to growth in H2 on page " + str(number) + ".\n"
                "Analyst One:\nQuestion on page " + str(number) + "?\n")
        pages.append({"page": number, "text": text})
    turns = retrieval.split_turns(pages)
    speakers = [t["speaker"] for t in turns]
    assert speakers.count("Kishor Patil") == 6
    assert speakers.count("Analyst One") == 6
    for turn in turns:
        assert "KPIT Technologies Limited" not in turn["text"]


# ------------------------------------------------------------------ poll timing evidence (round 7)

def test_poll_carries_timing_headers_and_server_timing(monkeypatch):
    from finn.research import jobs as research_jobs
    configure_live(monkeypatch)
    with TestClient(main.app) as client:
        job = main.RESEARCH._new_job("KPITTECH", status="done", brief={"sections": {}}, running=False)
        response = client.get("/api/analyze/" + job["job_id"])
        assert response.status_code == 200
        received = float(response.headers["x-received-at"])
        responded = float(response.headers["x-responded-at"])
        assert 0 <= responded - received < 5 and response.headers["server-timing"].startswith("app;dur=")
        body = response.json()
        timing = body["server_timing"]
        assert set(timing) >= {"server_time", "cpu_s", "rss_mb", "stage", "probe_events", "slow_handlers"}
        assert SECRET not in response.text and "http" not in json.dumps(timing)
        other = client.get("/healthz")
        assert "x-received-at" not in other.headers


def test_probe_records_a_stall(monkeypatch):
    from finn.research import timing
    before = len(timing.snapshot()["probe_events"])
    timing.record_event("thread", 3.2, 0.1)
    events = timing.snapshot()["probe_events"]
    assert len(events) == min(before + 1, 20) and events[-1]["lag_s"] == 3.2 and events[-1]["cpu_during_s"] == 0.1


def test_slow_handler_is_logged_and_kept():
    from finn.research import timing
    timing.note_request("analyze", 100.0, 102.5)
    assert timing.snapshot()["slow_handlers"][-1]["handler_s"] == 2.5
    timing.note_request("analyze", 100.0, 100.2)
    assert timing.snapshot()["slow_handlers"][-1]["handler_s"] == 2.5  # a fast request is not recorded


# ------------------------------------------------------------------ CPU evidence in the timing data (round 8)

def test_snapshot_reports_host_cpu_sources_without_failing_when_they_are_missing():
    from finn.research import timing
    snap = timing.snapshot()
    assert "host_cpu" in snap and isinstance(snap["host_cpu"], dict)
    assert timing._read_kv("/definitely/not/a/file") is None


def test_stage_records_cpu_seconds_next_to_wall_seconds():
    from finn.research import pipeline
    tracker = pipeline.Tracker(None)
    stage = tracker.start("extract:x")
    sum(i * i for i in range(200000))
    tracker.finish(stage, "ok")
    record = tracker.public()[0]
    assert record["seconds"] is not None and record["cpu_s"] >= 0
