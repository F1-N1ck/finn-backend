"""Live refresh of NSE announcements for the fixed demo portfolio.

Why this exists: the digest used to be a committed snapshot only. This module re-fetches the last 7 days of
announcements per symbol from NSE's own API (no cookie warm-up; it timed out from Render and was not needed),
keeps the result in memory and in a small file, and tells the digest exactly what it knows per symbol:

  live             fetched now, filings found
  valid_empty      fetched now, none in the window, but NSE returns filings for this symbol over 90 days
  ambiguous_empty  fetched now, nothing even over 90 days (NSE also answers [] for unknown symbols)
  failed           the fetch did not work (timeout, HTTP error, bad body); older data is kept and dated
  snapshot         never refreshed; the committed 31 Oct snapshot is all there is

Symbols come from the request (a user's own selection) or default to the demo portfolio. Each one must be a valid NSE
ticker; the host is fixed (NSE) and the URL is built here, so this cannot be used as a proxy. No selection is stored as a
list: only per-company results are kept, and they are facts about companies, not about any user.
Nothing here touches prices: live prices and live filings are separate things with separate times.
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from finn import digest, nse
from finn.research import cache as research_cache

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW_DAYS = 7
TEXT_CHARS = 400
COOLDOWN_S = 30
CALL_TIMEOUT_S = 25
RUN_DEADLINE_S = 170
WORKERS = 4
FILE_PREFIX = "https://nsearchives.nseindia.com/"
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
MAX_SYMBOLS = 40
MAX_KEPT_EXTRA_SYMBOLS = 200  # live results kept for symbols outside the demo portfolio (oldest dropped first)
FRESH_STATES = ("live", "valid_empty", "ambiguous_empty")
GOOD_STATES = FRESH_STATES + ("stale_live",)


def clean_symbols(symbols, default):
    """Validated, upper-cased, de-duplicated tickers (at most MAX_SYMBOLS). Invalid ones are dropped, never used."""
    if symbols is None:
        return list(default)
    seen = []
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if SYMBOL_RE.match(symbol) and symbol not in seen:
            seen.append(symbol)
    return seen[:MAX_SYMBOLS]


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path():
    return os.path.join(research_cache.cache_dir(), "announcements_live.json")


def normalise_item(raw):
    """NSE field names -> the snapshot's field names. Anything missing becomes an empty string, never a guess."""
    text = (raw.get("attchmntText") or raw.get("text") or "").strip()
    link = raw.get("attchmntFile") or raw.get("file") or None
    if link is not None and not str(link).startswith(FILE_PREFIX):
        link = None  # only NSE's own archive host is ever linked
    return {"an_dt": raw.get("an_dt") or "", "desc": (raw.get("desc") or "").strip(), "text": text[:TEXT_CHARS],
            "file": link}


def default_fetch(symbol):
    """One symbol: NSE verdict dict (see nse.classify_window). A fresh session each call; no warm-up."""
    session = nse.make_session(warm=False)
    today = datetime.now(IST).replace(tzinfo=None)
    return nse.classify_window(session, symbol, window_days=WINDOW_DAYS, today=today, timeout=CALL_TIMEOUT_S)


def result_from_verdict(verdict, fetched_utc):
    kind = verdict.get("verdict")
    if kind == "ok_nonempty":
        items = [normalise_item(i) for i in verdict.get("items", []) if i.get("an_dt")]
        return {"state": "live", "fetched_utc": fetched_utc, "items": items, "count": len(items)}
    if kind == "valid_empty_window":
        return {"state": "valid_empty", "fetched_utc": fetched_utc, "items": [], "count": 0,
                "newest_earlier": verdict.get("newest_filing_in_wide_window"),
                "widened_count": verdict.get("widened_count")}
    if kind == "ambiguous_empty":
        return {"state": "ambiguous_empty", "fetched_utc": fetched_utc, "items": [], "count": 0,
                "detail": verdict.get("detail")}
    return None


class FilingsRefresher:
    def __init__(self, fetch_fn=None, state_file=None, symbols=None, workers=WORKERS, deadline_s=RUN_DEADLINE_S,
                 cooldown_s=COOLDOWN_S, clock=time.time):
        self.fetch_fn = fetch_fn or default_fetch
        self.state_file = state_file
        self.symbols = list(symbols or digest.PORTFOLIO)
        self.workers = workers
        self.deadline_s = deadline_s
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.lock = threading.Lock()
        self.state = {"per_symbol": {}, "last_success_utc": None, "last_attempt_utc": None, "last_run": None}
        self.running = None  # {"started": t, "done": n, "total": n} while a run is in flight
        self.last_start = 0.0
        self.inflight = set()  # symbols queued or being fetched right now
        self.queue = []
        self.last_attempt = {}  # symbol -> clock() of its last start (per-symbol cooldown)
        self._load()

    # ------------------------------------------------------------ persistence (best effort)

    def _path(self):
        return self.state_file or state_path()

    def _load(self):
        try:
            with open(self._path(), encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("per_symbol"), dict):
                self.state = data
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path()), exist_ok=True)
            tmp = self._path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle)
            os.replace(tmp, self._path())
        except OSError:
            pass  # the in-memory state still works; a restart just falls back to the snapshot

    # ------------------------------------------------------------ public

    def live_state(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def status(self, symbols=None):
        """symbols=None: the whole refresher. With symbols: running means one of THESE symbols is queued or fetching."""
        with self.lock:
            run = self.running
            if symbols is None:
                running = run is not None
            else:
                running = run is not None and len(self.inflight.intersection(symbols)) > 0
            return {"running": running,
                    "progress": ({"done": run["done"], "total": run["total"],
                                  "elapsed_s": round(self.clock() - run["started"], 1)} if run and running else None),
                    "inflight": sorted(self.inflight) if symbols is None else sorted(self.inflight.intersection(symbols)),
                    "last_success_utc": self.state.get("last_success_utc"),
                    "last_attempt_utc": self.state.get("last_attempt_utc"),
                    "last_run": self.state.get("last_run")}

    def start(self, symbols=None):
        """Start (or join) a background refresh for these symbols (default: the demo portfolio).
        Returns (started, reason). A symbol already queued is not queued twice; one attempted within the cooldown is skipped."""
        wanted = clean_symbols(symbols, self.symbols)
        spawn = False
        with self.lock:
            busy = [s for s in wanted if s in self.inflight]
            now = self.clock()
            fresh = [s for s in wanted if s not in self.inflight and now - self.last_attempt.get(s, -10 ** 9) >= self.cooldown_s]
            if not fresh:
                return False, ("already_running" if busy else "too_soon")
            for symbol in fresh:
                self.inflight.add(symbol)
                self.last_attempt[symbol] = now
            self.last_start = now
            if self.running is None:
                self.running = {"started": now, "done": 0, "total": len(fresh)}
                self.queue = list(fresh)
                spawn = True
            else:
                self.running["total"] = self.running["total"] + len(fresh)
                self.queue.extend(fresh)
        if spawn:
            threading.Thread(target=self._drain, daemon=True).start()
        return True, None

    def _drain(self):
        while True:
            with self.lock:
                batch = list(self.queue)
                self.queue = []
            if not batch:
                return
            self.run(batch)

    def run(self, symbols=None):
        """Blocking refresh of these symbols (default: every demo symbol). Safe to call directly in tests."""
        symbols = clean_symbols(symbols, self.symbols)
        with self.lock:
            if self.running is None:
                self.running = {"started": self.clock(), "done": 0, "total": len(symbols)}
            started = self.clock()
        started_utc = utc_now()
        outcomes = {}

        def one(symbol):
            if self.clock() - started > self.deadline_s:
                outcomes[symbol] = {"state": "failed", "detail": "run deadline reached before this symbol"}
            else:
                try:
                    verdict = self.fetch_fn(symbol)
                    fetched = utc_now()
                    result = result_from_verdict(verdict, fetched)
                    if result is None:
                        result = {"state": "failed", "detail": (verdict.get("verdict") or "fetch_error") + ": " +
                                  str(verdict.get("detail") or verdict.get("status") or "")}
                    outcomes[symbol] = result
                except Exception as error:  # a bad symbol must not stop the others
                    outcomes[symbol] = {"state": "failed", "detail": type(error).__name__}
            with self.lock:
                self.running["done"] = self.running["done"] + 1

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(one, symbols))

        ok = [s for s in symbols if outcomes[s]["state"] != "failed"]
        finished_utc = utc_now()
        covers_demo = set(self.symbols).issubset(set(symbols))
        with self.lock:
            for symbol in symbols:
                outcome = outcomes[symbol]
                entry = self.state["per_symbol"].setdefault(symbol, {"good": None})
                entry["attempt_utc"] = finished_utc
                entry["attempt_state"] = "failed" if outcome["state"] == "failed" else "ok"
                entry["detail"] = outcome.get("detail")
                if outcome["state"] != "failed":
                    entry["good"] = outcome
            self._trim_extras()
            run_summary = {"started_utc": started_utc, "finished_utc": finished_utc, "ok": len(ok),
                           "failed": len(symbols) - len(ok), "total": len(symbols),
                           "failed_symbols": [s for s in symbols if s not in ok]}
            if covers_demo:
                # the run-level summary describes the demo portfolio only when the run covered all of it
                self.state["last_attempt_utc"] = finished_utc
                if ok:
                    self.state["last_success_utc"] = finished_utc
                self.state["last_run"] = run_summary
            for symbol in symbols:
                self.inflight.discard(symbol)
            if not self.queue:
                self.running = None
            self._save()
        return run_summary

    def _trim_extras(self):
        """Keep the state file bounded: forget the oldest results for symbols outside the demo portfolio."""
        extras = [s for s in self.state["per_symbol"] if s not in self.symbols]
        if len(extras) <= MAX_KEPT_EXTRA_SYMBOLS:
            return
        extras.sort(key=lambda s: self.state["per_symbol"][s].get("attempt_utc") or "")
        for symbol in extras[:len(extras) - MAX_KEPT_EXTRA_SYMBOLS]:
            del self.state["per_symbol"][symbol]


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def merge_into_raw(raw_ann, live_state, symbols=None):
    """Return (raw_with_sources, per_symbol_status). Live items replace snapshot items inside the live window;
    snapshot items older than the live window are kept (they are older, not contradicted).
    With symbols, only those are returned. A symbol with no snapshot and no live result is 'unchecked' (never 'no filings')."""
    merged = {}
    statuses = {}
    per_symbol = (live_state or {}).get("per_symbol", {})
    names = list(symbols) if symbols is not None else sorted(set(list(raw_ann.keys()) + list(per_symbol.keys())))
    for symbol in names:
        in_snapshot = symbol in raw_ann
        snap_items = [dict(i, source="snapshot") for i in raw_ann.get(symbol, {}).get("items", [])]
        entry = per_symbol.get(symbol)
        good = entry.get("good") if entry else None
        if good is None:
            failed = bool(entry and entry.get("attempt_state") == "failed")
            if failed:
                state = "failed_snapshot" if in_snapshot else "failed"
            else:
                state = "snapshot" if in_snapshot else "unchecked"
            statuses[symbol] = {"state": state, "detail": entry.get("detail") if entry else None,
                                "attempt_utc": entry.get("attempt_utc") if entry else None, "in_snapshot": in_snapshot}
            merged[symbol] = {"items": snap_items}
            continue
        fetched = good["fetched_utc"]
        window_start = parse_utc(fetched).astimezone(IST).date() - timedelta(days=WINDOW_DAYS)
        items = [dict(i, source="live", fetched_utc=fetched) for i in good.get("items", [])]
        for item in snap_items:
            try:
                when = digest.parse_time(item["an_dt"]).date()
            except ValueError:
                continue
            if when < window_start:
                items.append(item)
        merged[symbol] = {"items": items}
        stale = entry.get("attempt_state") == "failed"
        statuses[symbol] = {"state": ("stale_live" if stale else good["state"]), "fetched_utc": fetched,
                            "detail": entry.get("detail") if stale else good.get("detail"),
                            "newest_earlier": good.get("newest_earlier"), "count_live": good.get("count", 0),
                            "attempt_utc": entry.get("attempt_utc"), "in_snapshot": in_snapshot}
    return merged, statuses


def summarise(live_state, statuses, snapshot_fetched_utc, running_status=None):
    """The one object the UI uses to say what the Filings section is."""
    last_success = (live_state or {}).get("last_success_utc")
    last_run = (live_state or {}).get("last_run")
    if last_success is None:
        mode = "snapshot" if last_run is None else "refresh_failed"
    elif last_run and last_run["failed"] == 0:
        mode = "live"
    elif last_run and last_run["ok"] == 0:
        mode = "refresh_failed"
    else:
        mode = "partial"
    counts = {}
    for status in statuses.values():
        counts[status["state"]] = counts.get(status["state"], 0) + 1
    return {"mode": mode, "last_success_utc": last_success,
            "last_attempt_utc": (live_state or {}).get("last_attempt_utc"), "last_run": last_run,
            "snapshot_fetched_utc": snapshot_fetched_utc, "symbol_counts": counts, "symbols": statuses,
            "running": bool(running_status and running_status.get("running")),
            "progress": running_status.get("progress") if running_status else None}


def summarise_for(symbols, live_state, statuses, snapshot_fetched, running_status=None):
    """The same object as summarise(), but computed from these symbols' own states, so one user's selection
    never borrows the run summary of another's. mode: snapshot | unchecked | live | partial | refresh_failed."""
    per = (live_state or {}).get("per_symbol", {})
    fresh = [s for s in symbols if statuses[s]["state"] in FRESH_STATES]
    kept = [s for s in symbols if statuses[s]["state"] in GOOD_STATES]
    attempted = [s for s in symbols if per.get(s, {}).get("attempt_utc")]
    failed_now = [s for s in symbols if per.get(s, {}).get("attempt_state") == "failed"]
    fetched = [per[s]["good"]["fetched_utc"] for s in kept if per.get(s, {}).get("good")]
    attempts = [per[s]["attempt_utc"] for s in attempted]
    if not symbols:
        mode = "empty"
    elif not attempted:
        mode = "snapshot" if any(statuses[s].get("in_snapshot") for s in symbols) else "unchecked"
    elif not fresh:
        mode = "refresh_failed"
    elif failed_now or len(fresh) < len(symbols):
        mode = "partial"
    else:
        mode = "live"
    counts = {}
    for status in statuses.values():
        counts[status["state"]] = counts.get(status["state"], 0) + 1
    last_run = None
    if attempted:
        last_run = {"ok": len(fresh), "failed": len(failed_now), "total": len(symbols), "failed_symbols": failed_now,
                    "not_checked": [s for s in symbols if statuses[s]["state"] == "unchecked"]}
    return {"mode": mode, "last_success_utc": max(fetched) if fetched else None,
            "last_attempt_utc": max(attempts) if attempts else None, "last_run": last_run,
            "snapshot_fetched_utc": snapshot_fetched, "symbol_counts": counts, "symbols": statuses,
            "running": bool(running_status and running_status.get("running")),
            "progress": running_status.get("progress") if running_status else None,
            "inflight": (running_status or {}).get("inflight", [])}
