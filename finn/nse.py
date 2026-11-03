"""NSE corporate-announcements client with an honest verdict for empty results.

Facts verified in a browser on 31 Oct 2026:
- The API answers HTTP 200 with [] for (a) a valid window with no filings (TCS, 24-31 Oct),
  (b) a lowercase symbol ('tcs'), and (c) a nonexistent symbol. So [] alone proves nothing.
- Symbols are case-sensitive: always upper-case them.
classify_window() separates: ok_nonempty / valid_empty_window / ambiguous_empty /
fetch_error / parse_error.
"""

import re
from datetime import datetime, timedelta

import requests

PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
API_URL = "https://www.nseindia.com/api/corporate-announcements"
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def normalise_symbol(symbol):
    cleaned = symbol.strip().upper()
    if not SYMBOL_RE.match(cleaned):
        raise ValueError("invalid NSE symbol")
    return cleaned


def make_session(warm=True):
    """warm=False skips the page visit that sets cookies. From Render the visit timed out (30 s) and the API
    answered without it (see RENDER_HOST_CHECK_RESULTS.md), so the live refresh uses warm=False."""
    session = requests.Session()
    session.headers.update(HEADERS)
    if warm:
        session.get(PAGE_URL, timeout=30)  # sets the cookies the API expects
    return session


def _fmt(day):
    return day.strftime("%d-%m-%Y")


def fetch(session, symbol, from_date, to_date, timeout=30):
    """One API call. Returns {'status','kind','items','error'}; never raises on HTTP/parse problems."""
    params = {"index": "equities", "symbol": symbol, "from_date": _fmt(from_date), "to_date": _fmt(to_date)}
    try:
        response = session.get(API_URL, params=params, timeout=timeout)
    except requests.exceptions.RequestException as error:
        return {"status": None, "kind": "fetch_error", "items": None, "error": type(error).__name__}
    if response.status_code != 200:
        return {"status": response.status_code, "kind": "fetch_error", "items": None,
                "error": "HTTP " + str(response.status_code)}
    try:
        data = response.json()
    except ValueError:
        return {"status": 200, "kind": "parse_error", "items": None, "error": "body is not JSON"}
    if not isinstance(data, list):
        return {"status": 200, "kind": "parse_error", "items": None,
                "error": "JSON is not a list (" + type(data).__name__ + ")"}
    return {"status": 200, "kind": "list", "items": data, "error": None}


def newest_filing(items):
    newest = None
    for item in items:
        try:
            when = datetime.strptime(item.get("an_dt", ""), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            continue
        if newest is None or when > newest:
            newest = when
    return newest


def classify_window(session, symbol, window_days=7, widen_days=90, today=None, timeout=30):
    symbol = normalise_symbol(symbol)
    if today is None:
        today = datetime.now()
    first = fetch(session, symbol, today - timedelta(days=window_days), today, timeout)
    verdict = {"symbol": symbol, "window_days": window_days, "status": first["status"]}
    if first["kind"] != "list":
        verdict["verdict"] = first["kind"]
        verdict["detail"] = first["error"]
        return verdict
    verdict["count"] = len(first["items"])
    if len(first["items"]) > 0:
        verdict["verdict"] = "ok_nonempty"
        verdict["items"] = first["items"]
        return verdict
    wide = fetch(session, symbol, today - timedelta(days=widen_days), today, timeout)
    if wide["kind"] != "list":
        verdict["verdict"] = wide["kind"]
        verdict["detail"] = "while widening window: " + str(wide["error"])
        return verdict
    verdict["widened_days"] = widen_days
    verdict["widened_count"] = len(wide["items"])
    if len(wide["items"]) > 0:
        newest = newest_filing(wide["items"])
        verdict["verdict"] = "valid_empty_window"
        verdict["newest_filing_in_wide_window"] = newest.strftime("%d-%b-%Y") if newest else None
    else:
        verdict["verdict"] = "ambiguous_empty"
        verdict["detail"] = ("empty even over " + str(widen_days) + " days; NSE returns [] for unknown "
                             "symbols too, so cannot tell 'no filings' from 'wrong symbol'")
    return verdict


def symbol_in_market_window(session, symbol, window_days=7, today=None):
    """Heavy (about 3 MB): all equities announcements in the window; is the symbol among them?"""
    symbol = normalise_symbol(symbol)
    if today is None:
        today = datetime.now()
    params = {"index": "equities", "from_date": _fmt(today - timedelta(days=window_days)),
              "to_date": _fmt(today)}
    try:
        response = session.get(API_URL, params=params, timeout=60)
        data = response.json()
    except (requests.exceptions.RequestException, ValueError):
        return {"ok": False, "error": "market-wide fetch failed"}
    if response.status_code != 200 or not isinstance(data, list):
        return {"ok": False, "error": "unexpected market-wide response"}
    symbols = set()
    for item in data:
        symbols.add(item.get("symbol"))
    return {"ok": True, "records": len(data), "distinct_symbols": len(symbols),
            "symbol_present": symbol in symbols}
