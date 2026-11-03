"""Live Yahoo prices and ticker checks for ANY NSE symbol a user selects (works from Render: tested 31 Oct, ~0.2 s per symbol).

What this module does and does not do:
- It talks to ONE fixed host (Yahoo Finance) and builds the URL itself from a validated ticker. No URL, host or path
  from a request is ever used, so it cannot be used as a general proxy.
- It holds no portfolio. Each call gets a list of tickers from the request; the per-symbol cache is a cache of facts
  about companies (120 s), not a list of anyone's selection.
- Every fetch is counted against one global limit, so a burst of tickers cannot hammer Yahoo.
- It never raises: each symbol reports ok or an error type.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from finn import digest

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
QUERY_RE = re.compile(r"^[A-Za-z0-9 &.\-]{2,30}$")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_SECONDS = 120
MAX_CACHED_SYMBOLS = 400
FETCH_LIMIT_PER_MIN = 240
_rows = {}  # symbol -> (stored_at, row)
_calls = []  # timestamps of live Yahoo calls in the last minute
_lock = threading.Lock()


def clear_cache():
    with _lock:
        _rows.clear()
        del _calls[:]


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def allow_calls(count):
    """Global limit on live Yahoo calls. True and records them if allowed."""
    now = time.time()
    with _lock:
        _calls[:] = [t for t in _calls if now - t < 60]
        if len(_calls) + count > FETCH_LIMIT_PER_MIN:
            return False
        _calls.extend([now] * count)
        return True


def _chart(symbol, range_):
    return requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="") + ".NS",
                        params={"range": range_, "interval": "1d"}, headers=HEADERS, timeout=15)


def fetch_symbol(symbol, now=None):
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        return {"symbol": symbol, "ok": False, "error": "invalid symbol"}
    try:
        response = _chart(symbol, "3mo")
        if response.status_code == 404:
            return {"symbol": symbol, "ok": False, "error": "not_found"}
        if response.status_code != 200:
            return {"symbol": symbol, "ok": False, "error": "HTTP " + str(response.status_code)}
        result = response.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        quote_data = result["indicators"]["quote"][0]
        bars = []
        for index in range(len(stamps)):
            close = quote_data["close"][index]
            volume = quote_data["volume"][index]
            if close is None or volume is None:
                continue
            day = datetime.fromtimestamp(stamps[index], IST).strftime("%Y-%m-%d")
            bars.append([day, round(close, 2), volume])
        stats = digest.price_stats(bars[-22:])
        if stats is None:
            return {"symbol": symbol, "ok": False, "error": "not enough bars"}
        if now is None:
            now = datetime.now(IST)
        stats["partial"] = (stats["last_date"] == now.strftime("%Y-%m-%d") and now.weekday() < 5
                            and (now.hour, now.minute) < (15, 45))
        stats["symbol"] = symbol
        stats["ok"] = True
        meta = result.get("meta") or {}
        name = meta.get("longName") or meta.get("shortName")
        if name:
            stats["name"] = str(name)[:80]
        return stats
    except Exception as error:
        return {"symbol": symbol, "ok": False, "error": type(error).__name__}


def cached_rows(symbols):
    """Fresh-enough rows already held for these symbols: {symbol: row}. Never fetches."""
    now = time.time()
    found = {}
    with _lock:
        for symbol in symbols:
            hit = _rows.get(symbol)
            if hit and now - hit[0] < CACHE_SECONDS and hit[1].get("ok"):
                found[symbol] = hit[1]
    return found


def last_known_rows(symbols):
    """Last good row per symbol however old (the row carries its own fetched_utc). Never fetches."""
    found = {}
    with _lock:
        for symbol in symbols:
            hit = _rows.get(symbol)
            if hit and hit[1].get("ok"):
                found[symbol] = hit[1]
    return found


def _store(row):
    with _lock:
        _rows[row["symbol"]] = (time.time(), row)
        if len(_rows) > MAX_CACHED_SYMBOLS:
            oldest = sorted(_rows.items(), key=lambda item: item[1][0])[:len(_rows) - MAX_CACHED_SYMBOLS]
            for key, _ in oldest:
                del _rows[key]


def refresh(symbols):
    """Rows for the given (already validated) symbols, in order. Cached per symbol for CACHE_SECONDS."""
    fresh = cached_rows(symbols)
    todo = [s for s in symbols if s not in fresh]
    fetched = {}
    if todo:
        if allow_calls(len(todo)):
            with ThreadPoolExecutor(max_workers=4) as pool:
                for row in pool.map(fetch_symbol, todo):
                    fetched[row["symbol"]] = row
        else:
            for symbol in todo:
                fetched[symbol] = {"symbol": symbol, "ok": False, "error": "rate_limited"}
    stamp = _utc_now()
    rows = []
    for symbol in symbols:
        if symbol in fresh:
            rows.append(fresh[symbol])
            continue
        row = fetched[symbol]
        if row.get("ok"):
            row = dict(row, fetched_utc=stamp)
            _store(row)
        rows.append(row)
    ok = [r for r in rows if r.get("ok")]
    return {"source_type": "live", "fetched_utc": stamp,
            "label": "LIVE prices | Yahoo chart | fetched from the server just now (end-of-day bars; not an exchange feed)",
            "rows": rows, "ok_count": len(ok)}


def refresh_all():
    return refresh(list(digest.PORTFOLIO)), 0


def check_symbol(symbol):
    """Is this an NSE-listed equity? state: valid | not_found | check_failed (could not tell: nothing may be assumed)."""
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        return {"symbol": symbol, "state": "invalid_format"}
    if not allow_calls(1):
        return {"symbol": symbol, "state": "check_failed", "error": "rate_limited"}
    try:
        response = _chart(symbol, "5d")
        if response.status_code == 404:
            return {"symbol": symbol, "state": "not_found"}
        if response.status_code != 200:
            return {"symbol": symbol, "state": "check_failed", "error": "HTTP " + str(response.status_code)}
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        exchange = meta.get("exchangeName")
        kind = meta.get("instrumentType")
        if exchange not in ("NSI", "NSE") or (kind and kind != "EQUITY"):
            return {"symbol": symbol, "state": "not_found", "detail": "found, but not an NSE equity"}
        closes = [c for c in (result.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
        return {"symbol": symbol, "state": "valid", "name": str(meta.get("longName") or meta.get("shortName") or symbol)[:80],
                "last_close": round(closes[-1], 2) if closes else None}
    except Exception as error:
        return {"symbol": symbol, "state": "check_failed", "error": type(error).__name__}


def search_companies(query):
    """NSE equities matching a name or ticker fragment. Raises RuntimeError when the source cannot be used."""
    if not isinstance(query, str) or not QUERY_RE.match(query):
        raise ValueError("bad query")
    if not allow_calls(1):
        raise RuntimeError("rate_limited")
    try:
        response = requests.get("https://query2.finance.yahoo.com/v1/finance/search",
                                params={"q": query, "quotesCount": 10, "newsCount": 0, "listsCount": 0},
                                headers=HEADERS, timeout=10)
        if response.status_code != 200:
            raise RuntimeError("HTTP " + str(response.status_code))
        found = []
        for item in response.json().get("quotes", []):
            symbol = str(item.get("symbol", ""))
            if item.get("quoteType") != "EQUITY" or not symbol.endswith(".NS"):
                continue
            symbol = symbol[:-3]
            if SYMBOL_RE.match(symbol):
                found.append({"symbol": symbol, "name": str(item.get("longname") or item.get("shortname") or symbol)[:80]})
        return found[:8]
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(type(error).__name__)
