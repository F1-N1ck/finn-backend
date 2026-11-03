"""Management guidance versus reported outcome, with a comparability gate.

A comparison is only drawn when the guidance and the reported figure agree on all four of:
metric, period, unit and scope (and each is actually stated, not inferred or missing). Otherwise the answer is
'not directly comparable' with the exact fields that differ. The gate never does arithmetic on a pair that fails it.

Records are written by hand (data/snapshot/guidance_outcome.json) after reading the PDFs; the cited page text is
stored beside them (guidance_sources.json) so every quote can be checked mechanically and shown to the reader.
This module does not read documents or call a model.
"""

import json
import os

from finn import snapshot

FIELDS = ("metric", "period", "unit", "scope")
NOT_DIRECTLY = "Not directly comparable"


def _load(name):
    path = os.path.join(snapshot.snapshot_dir(), name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def compare(guidance, outcome):
    """Gate. Returns {'verdict': 'comparable'|'not_directly_comparable', 'reasons': [...], 'caveats': [...],
    'position': ...}. position is only set for a comparable pair with numbers on both sides."""
    reasons = []
    caveats = []
    for field in FIELDS:
        g = guidance.get(field) or {}
        o = outcome.get(field) or {}
        gk, ok = g.get("key"), o.get("key")
        if gk in (None, "not_stated") or ok in (None, "not_stated"):
            which = []
            if gk in (None, "not_stated"):
                which.append("guidance")
            if ok in (None, "not_stated"):
                which.append("reported figure")
            reasons.append({"field": field, "guidance": g.get("label"), "outcome": o.get("label"),
                            "why": field + " is not stated for the " + " and the ".join(which) +
                            ", so a match cannot be confirmed"})
        elif gk != ok:
            reasons.append({"field": field, "guidance": g.get("label"), "outcome": o.get("label"),
                            "why": "different " + field})
        elif g.get("basis") == "inferred" or o.get("basis") == "inferred":
            caveats.append(field + " matched only by inference: " + (g.get("basis_note") or o.get("basis_note") or ""))
    if reasons:
        return {"verdict": "not_directly_comparable", "reasons": reasons, "caveats": caveats, "position": None}
    position = None
    low, high, value = guidance.get("low"), guidance.get("high"), outcome.get("value")
    if value is not None and low is not None and high is not None:
        position = "below" if value < low else ("above" if value > high else "within")
    return {"verdict": "comparable", "reasons": [], "caveats": caveats, "position": position}


def quote_in_source(sources, source_id, page, quote):
    source = sources.get(source_id) or {}
    text = (source.get("pages") or {}).get(str(page))
    return text is not None and quote in text


def _side(sources, side, extras=True):
    source = sources.get(side["source_id"]) or {}
    page_text = (source.get("pages") or {}).get(str(side["pdf_page"]))
    out = {"source_id": side["source_id"], "title": source.get("title"), "call_date": source.get("call_date"),
           "url": source.get("url"), "company_url": source.get("company_url"), "url_note": source.get("url_note"),
           "pdf_page": side["pdf_page"], "quote": side["quote"], "page_text": page_text,
           "quote_verbatim_in_stored_page": quote_in_source(sources, side["source_id"], side["pdf_page"], side["quote"]),
           "metric": side["metric"], "period": side["period"], "unit": side["unit"], "scope": side["scope"]}
    if extras:
        out["extra_quotes"] = [dict(e, verbatim=quote_in_source(sources, side["source_id"], e["pdf_page"], e["quote"]))
                               for e in side.get("extra_quotes", [])]
    return out


def evaluate_all():
    data = _load("guidance_outcome.json")
    sources = (_load("guidance_sources.json") or {}).get("sources", {})
    if data is None:
        return {"examples": [], "verified_by": None, "not_verified": None}
    examples = []
    for ex in data["examples"]:
        gate = compare(ex["guidance"], ex["outcome"])
        guidance_side = _side(sources, ex["guidance"])
        outcome_side = _side(sources, ex["outcome"], extras=False)
        # a comparison is withheld if a cited quote is not found in the stored page text
        quotes_ok = guidance_side["quote_verbatim_in_stored_page"] and outcome_side["quote_verbatim_in_stored_page"] \
            and all(e["verbatim"] for e in guidance_side["extra_quotes"])
        if not quotes_ok:
            gate = {"verdict": "not_directly_comparable", "position": None, "caveats": [],
                    "reasons": [{"field": "quote", "guidance": None, "outcome": None,
                                 "why": "a cited quote is not in the stored page text"}]}
        examples.append({"id": ex["id"], "symbol": ex["symbol"], "company": ex["company"], "title": ex["title"],
                         "guidance": guidance_side, "outcome": outcome_side, "gate": gate,
                         "management_framing": ex.get("management_framing"),
                         "guided_range": {"low": ex["guidance"].get("low"), "high": ex["guidance"].get("high")},
                         "reported_value": ex["outcome"].get("value")})
    return {"examples": examples, "verified_by": data.get("verified_by"), "not_verified": data.get("not_verified")}
