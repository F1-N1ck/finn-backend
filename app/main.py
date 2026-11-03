"""FINN minimal backend (FastAPI).

Public routes:  GET /healthz, GET /api/questions, POST /api/research,
                POST /api/analyze (any NSE ticker, job based), GET /api/analyze/{job_id}
Admin routes:   POST /admin/host-check, GET /admin/host-check/{job_id}
                (bearer token required, fixed targets only, disabled unless enabled by env)

Secrets are read from the environment only and never returned or logged.
Questions are a fixed list (no free-text prompts), which also protects the model quota.
The snapshot (text + cached answers) is read-only and ships with the repo.
"""

import asyncio
import hmac
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from finn import announcements, budget, digest, grounded_qa, guidance, hostcheck, llm_adapter, prices, snapshot
from finn.research import jobs as research_jobs
from finn.research import timing as research_timing
from finn.research import validate as research_validate


@asynccontextmanager
async def lifespan(_app):
    research_timing.start_thread_probe()
    probe = asyncio.create_task(research_timing.loop_probe())
    try:
        yield
    finally:
        probe.cancel()


app = FastAPI(title="FINN backend", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class TimingMiddleware:
    """Adds X-Received-At / X-Responded-At / Server-Timing to analysis responses (see finn/research/timing.py)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/analyze"):
            await self.inner(scope, receive, send)
            return
        received = time.time()

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                now = time.time()
                headers = list(message.get("headers", []))
                headers.append((b"x-received-at", ("%.3f" % received).encode()))
                headers.append((b"x-responded-at", ("%.3f" % now).encode()))
                headers.append((b"server-timing", ("app;dur=%d" % int((now - received) * 1000)).encode()))
                message = dict(message, headers=headers)
                research_timing.note_request("analyze", received, now)
            await send(message)

        await self.inner(scope, receive, send_with_timing)


app.add_middleware(TimingMiddleware)

origins = [o.strip() for o in os.environ.get("FINN_CORS_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])

llm_adapter.load_dotenv_if_present(".env")  # local development only; Render uses real env vars


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


LIVE_BUDGET = budget.LiveBudget(_int_env("FINN_LIVE_MAX_PER_MIN", 2), _int_env("FINN_LIVE_MAX_PER_DAY", 40))
ADMIN_FAILURES = []
JOBS = {}
JOB_LOCK = threading.Lock()


def get_config():
    try:
        return llm_adapter.LLMConfig.from_env()
    except llm_adapter.LLMError:
        # bad FINN_LLM_EXTRA_BODY etc: behave as "live not configured", never crash the app
        return llm_adapter.LLMConfig("", "", "")


def questions_by_id():
    result = {}
    for question in grounded_qa.QUESTIONS:
        result[question["id"]] = question
    return result


# ---------------------------------------------------------------- public

class ResearchRequest(BaseModel):
    question_id: str
    mode: str = "auto"  # auto = live if possible else cached; live; cached


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


FILINGS = announcements.FilingsRefresher()


@app.get("/api/digest")
def get_digest():
    result = digest.build_digest(questions=grounded_qa.QUESTIONS, live_state=FILINGS.live_state(),
                                 running_status=FILINGS.status())
    if result is None:
        raise HTTPException(status_code=503, detail={"code": "digest_snapshot_missing"})
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.post("/api/filings/refresh", status_code=202)
def refresh_filings():
    """Start a background NSE announcements refresh for the demo portfolio (not prices). Poll /api/filings/status."""
    started, reason = FILINGS.start()
    return JSONResponse(dict(FILINGS.status(), started=started, reason=reason), status_code=202,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/filings/status")
def filings_status():
    return JSONResponse(FILINGS.status(), headers={"Cache-Control": "no-store"})


@app.get("/api/guidance")
def get_guidance():
    return JSONResponse(guidance.evaluate_all(), headers={"Cache-Control": "no-store"})


LAST_PRICE_REFRESH = {"at": 0.0}


@app.post("/api/prices/refresh")
def refresh_prices():
    """Live Yahoo prices for the fixed demo portfolio. Cached 120 s; at most one real refresh per 20 s."""
    now = time.time()
    if now - LAST_PRICE_REFRESH["at"] < 20 and prices._cache["data"] is None:
        raise HTTPException(status_code=429, detail={"code": "refresh_too_soon"})
    LAST_PRICE_REFRESH["at"] = now
    payload, _ = prices.refresh_all()
    if payload["ok_count"] == 0:
        raise HTTPException(status_code=503, detail={"code": "live_prices_unavailable"})
    return payload


@app.get("/healthz")
def healthz():
    config = get_config()
    check = snapshot.verify()
    return {
        "status": "ok",
        "live_model_configured": len(config.missing()) == 0,
        "model": config.model if config.model else None,
        "snapshot_ok": check["ok"],
        "snapshot_retrieved": check.get("retrieved"),
    }


@app.get("/api/questions")
def list_questions():
    cache = llm_adapter.load_cache(snapshot.cache_path())
    items = []
    for question in grounded_qa.QUESTIONS:
        key = question["doc"] + "|" + question["id"]
        entry = cache.get(key)
        cached = None
        if entry:
            cached = {"generated_at": entry.get("generated_at"), "source_type": entry.get("source_type", "model"),
                      "verification": entry.get("verification"),
                      "label": llm_adapter.make_label("cached", entry.get("model"), entry.get("generated_at"),
                                                      entry.get("verification", ""), entry.get("source_type", "model"))}
        items.append({"id": question["id"], "doc": question["doc"], "symbol": question.get("symbol"),
                      "short": question.get("short"), "question": question["question"], "cached": cached})
    return {"questions": items}


@app.post("/api/research")
def research(request: ResearchRequest):
    questions = questions_by_id()
    if request.question_id not in questions:
        raise HTTPException(status_code=404, detail={"code": "unknown_question"})
    if request.mode not in ("auto", "live", "cached"):
        raise HTTPException(status_code=422, detail={"code": "bad_mode"})
    question = questions[request.question_id]
    pages = snapshot.load_pages(question["doc"])
    if pages is None:
        raise HTTPException(status_code=503, detail={"code": "snapshot_missing"})

    config = get_config()
    cache_path = snapshot.cache_path()
    allow_live = False
    skip_reason = None
    if request.mode == "cached":
        skip_reason = "cached_requested"
    elif len(config.missing()) > 0:
        skip_reason = "config_missing"
    else:
        allowed, reason = LIVE_BUDGET.try_acquire()
        allow_live = allowed
        skip_reason = reason

    if request.mode == "live" and not allow_live:
        raise HTTPException(status_code=503, detail={"code": skip_reason, "message": "live analysis unavailable"})

    record = grounded_qa.run_question(config, question, pages, cache_path, allow_live=allow_live)
    if record.get("outcome") == "no_answer":
        raise HTTPException(status_code=503, detail={"code": record.get("error_code"),
                                                     "message": record.get("error_message")})
    if record.get("outcome") == "no_passages":
        raise HTTPException(status_code=422, detail={"code": "no_passages"})
    if request.mode == "live" and record.get("outcome") != "live":
        # explicit live request that fell back: refuse to pass cached off as live
        raise HTTPException(status_code=503, detail={"code": (record.get("fallback_reason") or {}).get("code", "live_failed"),
                                                     "message": "live analysis failed; use mode=cached or auto"})
    reason = record.get("fallback_reason")
    if record.get("outcome") == "cached" and reason and reason.get("code") == "live_not_attempted" and skip_reason:
        record["fallback_reason"] = {"code": skip_reason, "message": reason.get("message")}
    record["mode"] = record.pop("outcome")
    record["disclaimer"] = ("Passages are top-k retrieval, not the whole document; the answer may omit relevant "
                            "statements. Mechanical checks do not verify meaning.")
    return record


# ---------------------------------------------------------------- generic research (any NSE ticker)

def snapshot_fallback(symbol, reason):
    """The saved KPIT demo answers, clearly labelled. None for any other ticker."""
    matching = [q for q in grounded_qa.QUESTIONS if q.get("symbol") == symbol]
    if len(matching) == 0:
        return None
    answers = []
    for question in matching:
        pages = snapshot.load_pages(question["doc"])
        if pages is None:
            continue
        record = grounded_qa.run_question(get_config(), question, pages, snapshot.cache_path(), allow_live=False)
        if record.get("outcome") == "cached":
            record["mode"] = record.pop("outcome")
            answers.append(record)
    if len(answers) == 0:
        return None
    return {"ticker": symbol, "answer_source": "snapshot_fallback", "sections": {},
            "label": "SAVED SNAPSHOT | documents were retrieved earlier and committed with the app; nothing below was fetched now",
            "fallback_reason": reason, "snapshot_answers": answers,
            "disclaimer": "Saved demo answers for a fixed pair of questions, not a fresh analysis."}


RESEARCH = research_jobs.ResearchService(get_config, fallback_fn=snapshot_fallback)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a url field (or anything else) is rejected, not ignored
    ticker: str


@app.post("/api/analyze", status_code=202)
def analyze(request: AnalyzeRequest):
    symbol, code, message = research_validate.validate_ticker(request.ticker)
    if symbol is None:
        raise HTTPException(status_code=422, detail={"code": code, "message": message})
    try:
        return RESEARCH.submit(symbol)
    except research_jobs.ServiceError as error:
        raise HTTPException(status_code=error.status, detail={"code": error.code, "message": error.message})


@app.get("/api/analyze/{job_id}")
def analyze_status(job_id: str):
    if len(job_id) != 12 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(status_code=404, detail={"code": "unknown_job"})
    job = RESEARCH.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={
            "code": "unknown_job", "instance": research_jobs.INSTANCE,
            "message": "no such job on this server instance; the free server may have restarted, so submit again"})
    job = dict(job, server_timing=research_timing.snapshot())
    return JSONResponse(job, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- admin (protected)

def require_admin(authorization):
    token = os.environ.get("FINN_ADMIN_TOKEN", "")
    enabled = os.environ.get("FINN_ENABLE_HOST_CHECK", "0") == "1"
    if not enabled or token == "":
        raise HTTPException(status_code=404, detail="not found")  # do not advertise the route
    now = time.time()
    recent = [t for t in ADMIN_FAILURES if now - t < 600]
    del ADMIN_FAILURES[:]
    ADMIN_FAILURES.extend(recent)
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="too many failed attempts; wait 10 minutes")
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[len("Bearer "):]
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        ADMIN_FAILURES.append(now)
        raise HTTPException(status_code=401, detail="unauthorised")


def _run_job(job_id, include_model):
    try:
        report = hostcheck.run_all(include_model, get_config(), snapshot.snapshot_dir(), snapshot.cache_path())
        state = {"status": "done", "report": report}
    except Exception as error:  # never leak details beyond the type name
        state = {"status": "failed", "error": type(error).__name__}
    with JOB_LOCK:
        JOBS[job_id].update(state)


@app.post("/admin/host-check", status_code=202)
def start_host_check(model: int = 1, authorization: str = Header(default="")):
    require_admin(authorization)
    with JOB_LOCK:
        running = [j for j in JOBS.values() if j["status"] == "running"]
        recent = [j for j in JOBS.values() if time.time() - j["started"] < 60]
        if running or recent:
            raise HTTPException(status_code=429, detail="a host check ran or is running within the last 60 s")
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"status": "running", "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, model == 1), daemon=True).start()
    return {"job_id": job_id, "poll": "/admin/host-check/" + job_id}


@app.get("/admin/host-check/{job_id}")
def get_host_check(job_id: str, authorization: str = Header(default="")):
    require_admin(authorization)
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return dict(job)
