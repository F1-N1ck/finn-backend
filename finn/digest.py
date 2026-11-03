"""Push-side digest: price/volume stats, filing tiers, de-duplication.

Everything here is rule-based (no model). Tier rules are keyed on NSE's own `desc` field
plus a short keyword escalation list, and every filing carries the reason for its tier.
Inputs are the committed snapshot files, so the digest works even if every live source is down.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from finn import snapshot

IST = timezone(timedelta(hours=5, minutes=30))

HIGH_DESC = {
    "outcome of board meeting", "change in management", "cessation", "appointment", "resignation",
    "acquisition", "sale or disposal", "dividend", "bagging/receiving of orders/contracts",
    "action(s) taken or orders passed", "other restructuring", "fund raising", "credit rating",
}
LOW_DESC = {
    "copy of newspaper publication", "esop/esos/esps",
    "certificate under sebi (depositories and participants) regulations, 2018",
}
ESCALATE_WORDS = ["managing director", "resign", "fraud", "default", "penalty", "insolvency", "cessation"]

PORTFOLIO = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ITC", "PIDILITIND", "KPITTECH", "ICICIBANK",
             "SBIN", "BHARTIARTL", "LT", "HINDUNILVR", "SUNPHARMA", "MARUTI", "TITAN", "ULTRACEMCO"]


def _load_json(name, directory):
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return None
    file_handle = open(path, encoding="utf-8")
    data = json.load(file_handle)
    file_handle.close()
    return data


def tier_for(desc, text):
    """Return (tier, reason). tier is high, medium or low."""
    key = (desc or "").strip().lower()
    lowered = (text or "").lower()
    if key in HIGH_DESC:
        return "high", "NSE category '" + desc + "'"
    for word in ESCALATE_WORDS:
        if word in lowered:
            return "high", "text mentions '" + word + "'"
    if key in LOW_DESC:
        return "low", "NSE category '" + desc + "' (routine)"
    return "medium", "default for NSE category '" + (desc or "unknown") + "'"


def price_stats(bars):
    """bars = [[date, close, volume], ...] oldest first. Needs at least 21 bars for the volume average."""
    if len(bars) < 2:
        return None
    last = bars[-1]
    prev = bars[-2]
    prior = bars[-21:-1]
    average = None
    ratio = None
    if len(prior) >= 20:
        total = 0
        for bar in prior:
            total = total + bar[2]
        average = total / len(prior)
        if average > 0:
            ratio = round(last[2] / average, 2)
    change = None
    if prev[1] > 0:
        change = round((last[1] / prev[1] - 1) * 100, 2)
    return {"last_date": last[0], "close": last[1], "prev_close": prev[1], "change_pct": change,
            "volume": last[2], "avg_volume_20d": round(average) if average else None, "volume_ratio": ratio}


def parse_time(text):
    when = datetime.strptime(text, "%d-%b-%Y %H:%M:%S")
    return when.replace(tzinfo=IST)


def build_filings(raw_announcements):
    filings = []
    for symbol in raw_announcements:
        seen = {}
        for item in raw_announcements[symbol].get("items", []):
            key = (item["an_dt"], item["desc"], item["text"])
            if key in seen:
                seen[key]["dup_count"] = seen[key]["dup_count"] + 1
                continue
            tier, reason = tier_for(item["desc"], item["text"])
            row = {"symbol": symbol, "at": parse_time(item["an_dt"]).isoformat(), "desc": item["desc"],
                   "text": item["text"], "tier": tier, "tier_reason": reason, "url": item.get("file"),
                   "dup_count": 1, "source": item.get("source", "snapshot"),
                   "fetched_utc": item.get("fetched_utc")}
            seen[key] = row
            filings.append(row)
    filings.sort(key=lambda row: row["at"], reverse=True)
    return filings


def research_symbols(questions):
    found = set()
    for question in questions:
        if question.get("symbol"):
            found.add(question["symbol"])
    return found


def _empty_note(symbol, status, meta):
    """One honest sentence for a symbol with no filings in view. Says which kind of 'nothing' it is."""
    state = status["state"]
    if state == "valid_empty":
        newest = status.get("newest_earlier")
        return ("No filings in the last 7 days. Checked live on NSE at " + status["fetched_utc"] + "; NSE does list "
                "earlier filings for this symbol" + (" (newest " + newest + ")." if newest else "."))
    if state == "ambiguous_empty":
        return ("NSE returned nothing for this symbol, even over 90 days. NSE also returns nothing for unknown "
                "symbols, so this cannot be read as 'no filings'.")
    if state in ("failed_snapshot", "stale_live"):
        return "The live refresh failed for this symbol (" + str(status.get("detail") or "no detail") + ")."
    check = meta.get("empty_window_checks", {}).get(symbol)
    if check:
        return ("No filings in the snapshot window. Checked: " + str(check["count_since_01_08_2026"]) +
                " filings since 1 Aug 2026, newest " + check["newest_filing"][:11] + ".")
    return None


def build_digest(directory=None, questions=(), live_state=None, running_status=None):
    from finn import announcements  # late import: announcements imports this module
    directory = directory or snapshot.snapshot_dir()
    raw_ann = _load_json("raw_announcements.json", directory)
    raw_px = _load_json("raw_prices.json", directory)
    meta = _load_json("digest_meta.json", directory)
    if raw_ann is None or raw_px is None or meta is None:
        return None
    snapshot_fetched = meta["announcements"]["fetched_utc"]
    merged, statuses = announcements.merge_into_raw(raw_ann, live_state)
    filings = build_filings(merged)
    with_research = research_symbols(questions)
    portfolio = []
    for symbol in PORTFOLIO:
        entry = {"symbol": symbol, "name": None, "has_research": symbol in with_research}
        px = raw_px.get(symbol)
        if px and px.get("bars"):
            entry["name"] = px.get("name")
            stats = price_stats(px["bars"])
            if stats:
                entry.update(stats)
        mine = [f for f in filings if f["symbol"] == symbol]
        status = statuses.get(symbol, {"state": "snapshot"})
        entry["filings_count"] = len(mine)
        entry["filings_high"] = len([f for f in mine if f["tier"] == "high"])
        entry["filings_state"] = status["state"]
        if len(mine) == 0 or status["state"] in ("valid_empty", "ambiguous_empty"):
            note = _empty_note(symbol, status, meta)
            if note:
                entry["no_filings_note"] = note
        portfolio.append(entry)
    summary = announcements.summarise(live_state, statuses, snapshot_fetched, running_status)
    as_of = summary["last_success_utc"] or snapshot_fetched
    if summary["last_success_utc"]:
        label = ("FILINGS: NSE API, last successful refresh " + summary["last_success_utc"] + " (" + summary["mode"] +
                 "); symbols not refreshed show the " + snapshot_fetched + " snapshot | prices fetched " +
                 meta["prices"]["fetched_utc"] + " (Yahoo), last bar " + meta["prices"]["last_bar"])
    else:
        label = ("SNAPSHOT | filings fetched " + snapshot_fetched + " (NSE API) | prices fetched " +
                 meta["prices"]["fetched_utc"] + " (Yahoo), last bar " + meta["prices"]["last_bar"] +
                 " | fetched via the builder's browser, not live")
    return {
        "source_type": "snapshot" if summary["mode"] == "snapshot" else "live_filings",
        "label": label,
        "as_of_utc": as_of,
        "meta": meta,
        "filings_status": summary,
        "portfolio": portfolio,
        "filings": filings,
    }
