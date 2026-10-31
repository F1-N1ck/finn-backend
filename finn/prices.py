"""Live Yahoo price refresh for the fixed demo portfolio (works from Render: tested 31 Oct, ~0.2 s per symbol).

Only symbols from digest.PORTFOLIO are fetched, so the endpoint cannot be used as a proxy.
Results are cached in memory for 120 s. Never raises: each symbol reports ok or an error type.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from finn import digest

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_SECONDS = 120
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def fetch_symbol(symbol, now=None):
    if symbol not in digest.PORTFOLIO or not SYMBOL_RE.match(symbol):
        return {"symbol": symbol, "ok": False, "error": "symbol not in demo portfolio"}
    try:
        response = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + ".NS",
                                params={"range": "3mo", "interval": "1d"}, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return {"symbol": symbol, "ok": False, "error": "HTTP " + str(response.status_code)}
        result = response.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        bars = []
        for index in range(len(stamps)):
            close = quote["close"][index]
            volume = quote["volume"][index]
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
        return stats
    except Exception as error:
        return {"symbol": symbol, "ok": False, "error": type(error).__name__}


def refresh_all():
    """Returns (payload, retry_after_seconds). Cached for CACHE_SECONDS."""
    with _lock:
        age = time.time() - _cache["at"]
        if _cache["data"] is not None and age < CACHE_SECONDS:
            return _cache["data"], 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(fetch_symbol, digest.PORTFOLIO))
        payload = {"source_type": "live", "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "label": "LIVE prices | Yahoo chart | fetched from the server just now",
                   "rows": rows, "ok_count": len([r for r in rows if r.get("ok")])}
        if payload["ok_count"] > 0:
            _cache["at"] = time.time()
            _cache["data"] = payload
        return payload, 0
