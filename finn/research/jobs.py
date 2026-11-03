"""Job runner for analyses. The free server is slow, so POST starts a job and the page polls.

Protections: one analysis at a time, a global hourly and daily cap on LIVE analyses, and a free
path for results that are already cached. Nothing here accepts a URL.
"""

import os
import threading
import time
import uuid

from finn import llm_adapter
from finn.research import pipeline, timing


INSTANCE = uuid.uuid4().hex[:8]  # changes on every process start, so a client can see a restart
BOOTED_AT = time.time()


class ServiceError(Exception):
    def __init__(self, status, code, message):
        Exception.__init__(self, code)
        self.status = status
        self.code = code
        self.message = message


class SlidingLimit:
    def __init__(self, per_hour, per_day):
        self.per_hour = per_hour
        self.per_day = per_day
        self.stamps = []
        self.lock = threading.Lock()

    def acquire(self, now=None):
        if now is None:
            now = time.time()
        with self.lock:
            self.stamps = [t for t in self.stamps if now - t < 86400]
            last_hour = [t for t in self.stamps if now - t < 3600]
            if len(last_hour) >= self.per_hour:
                return False, "rate_limited_hourly"
            if len(self.stamps) >= self.per_day:
                return False, "rate_limited_daily"
            self.stamps.append(now)
            return True, None


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class ResearchService:
    def __init__(self, config_fn, fallback_fn=None, runner=None, limiter=None, max_jobs=30, hard_limit_s=None):
        self.config_fn = config_fn
        self.fallback_fn = fallback_fn
        self.runner = runner or pipeline.analyze
        self.limiter = limiter or SlidingLimit(_int_env("FINN_ANALYZE_MAX_PER_HOUR", 8),
                                               _int_env("FINN_ANALYZE_MAX_PER_DAY", 20))
        self.max_jobs = max_jobs
        self.hard_limit_s = hard_limit_s if hard_limit_s is not None else _int_env("FINN_JOB_HARD_LIMIT_S", 420)
        self.jobs = {}
        self.order = []
        self.lock = threading.RLock()
        self.running_id = None

    # -------------------------------------------------------------- public

    def submit(self, symbol):
        config = self.config_fn()
        with self.lock:
            if self.running_id is not None:
                running = self.jobs[self.running_id]
                if running["symbol"] == symbol:
                    return self._public(running)
                raise ServiceError(429, "busy", "another analysis is running; try again in a minute")
        cached = pipeline.peek_cached_brief(symbol, config)
        if cached is not None:
            return self._public(self._new_job(symbol, status="done", brief=cached, running=False))
        fallback_reason = None
        if len(config.missing()) > 0:
            fallback_reason = {"code": "model_not_configured", "message": "live model is not configured"}
        else:
            allowed, reason = self.limiter.acquire()
            if not allowed:
                fallback_reason = {"code": reason, "message": "the live-analysis cap has been reached"}
        if fallback_reason is not None:
            fallback = self.fallback_fn(symbol, fallback_reason) if self.fallback_fn else None
            if fallback is None:
                raise ServiceError(429 if fallback_reason["code"].startswith("rate") else 503,
                                   fallback_reason["code"], fallback_reason["message"])
            return self._public(self._new_job(symbol, status="done", brief=fallback, running=False))
        with self.lock:
            if self.running_id is not None:
                raise ServiceError(429, "busy", "another analysis is running; try again in a minute")
            job = self._new_job(symbol, status="running", running=True)
        threading.Thread(target=self._run, args=(job["job_id"], symbol, config), daemon=True).start()
        watchdog = threading.Timer(self.hard_limit_s, self._expire, args=(job["job_id"],))
        watchdog.daemon = True
        watchdog.start()
        return self._public(job)

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return self._public(job)

    # -------------------------------------------------------------- internal

    def _new_job(self, symbol, status, brief=None, running=False):
        with self.lock:
            job_id = uuid.uuid4().hex[:12]
            job = {"job_id": job_id, "symbol": symbol, "status": status, "stages": [], "brief": brief,
                   "error": None, "started": time.time(), "finished": None if running else time.time()}
            self.jobs[job_id] = job
            self.order.append(job_id)
            while len(self.order) > self.max_jobs:
                old = self.order.pop(0)
                self.jobs.pop(old, None)
            if running:
                self.running_id = job_id
            return job

    def _expire(self, job_id):
        """Watchdog: a job that is still running after the hard limit is marked failed, so the page never waits
        forever and the next analysis can start. The worker thread may still finish; its result is then dropped."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job["status"] != "running":
                return
            job["status"] = "failed"
            job["error"] = {"code": "job_timeout", "message": "the analysis did not finish within "
                            + str(self.hard_limit_s) + " s and was stopped; nothing was invented"}
            job["finished"] = time.time()
            if self.running_id == job_id:
                self.running_id = None

    def _progress(self, job_id, stages):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job["stages"] = stages

    def _run(self, job_id, symbol, config):
        outcome = None
        try:
            outcome = self.runner(symbol, config, callback=lambda stages: self._progress(job_id, stages))
        except Exception as error:  # never leak details beyond the type name
            outcome = {"ok": False, "error": {"code": "internal_error", "message": type(error).__name__},
                       "stages": []}
        brief = outcome.get("brief") if outcome.get("ok") else None
        error = None if outcome.get("ok") else outcome.get("error")
        if brief is not None:
            none_ok = all(s["status"] != "ok" for s in brief["sections"].values())
            if none_ok and self.fallback_fn:
                fallback = self.fallback_fn(symbol, {"code": "live_sections_empty",
                                                     "message": "no section could be produced from live documents"})
                if fallback is not None:
                    fallback["live_attempt"] = brief
                    brief = fallback
        elif self.fallback_fn:
            fallback = self.fallback_fn(symbol, error or {"code": "failed", "message": "analysis failed"})
            if fallback is not None:
                fallback["live_attempt_stages"] = outcome.get("stages")
                brief = fallback
                error = None
        timing.set_stage("idle")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None and job["status"] != "running":
                job = None  # the watchdog already closed this job; drop the late result
            if job is not None:
                job["brief"] = brief
                job["error"] = error
                job["diagnostics"] = outcome.get("diagnostics")
                if outcome.get("stages"):
                    job["stages"] = outcome["stages"]
                job["status"] = "done" if brief is not None else "failed"
                job["finished"] = time.time()
            if self.running_id == job_id:
                self.running_id = None

    def _public(self, job):
        out = {"job_id": job["job_id"], "symbol": job["symbol"], "status": job["status"],
               "instance": INSTANCE, "stages": job["stages"],
               "elapsed_s": round((job["finished"] or time.time()) - job["started"], 1)}
        brief = job.get("brief")
        if brief is not None:
            out["brief"] = brief
            out["outcome"] = brief.get("outcome") or (
                "snapshot_fallback" if brief.get("answer_source") == "snapshot_fallback" else "complete")
        elif job["status"] == "failed":
            out["outcome"] = "failed"
        if job.get("error") is not None:
            out["error"] = job["error"]
        if job.get("diagnostics"):
            out["diagnostics"] = job["diagnostics"]
        return out
