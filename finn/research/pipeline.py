"""The research flow: ticker -> discover documents -> fetch + extract -> three cited sections.

Everything the brief says comes from the two fetched primary documents (annual report and
earnings-call transcript). Each claim carries document title, date label, URL, PDF page and a
verbatim excerpt. Where evidence is missing the section says so instead of guessing.
"""

import copy
import os
import re
import time

from finn import grounded_qa, llm_adapter
from finn.research import cache, discover, extract, pacer as pacer_module, safe_fetch, sections

DISCOVERY_TTL_S = 6 * 3600
BRIEF_TTL_S = 24 * 3600
BSE_HOME = "https://www.bseindia.com/"

DISCLAIMER = ("Built only from the two documents named above, retrieved as top-k passages (not the whole "
              "document). Automatic checks confirm the quote is verbatim and the numbers appear in the cited "
              "passage; they do not confirm meaning. Page numbers are PDF page indexes. Not investment advice.")


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def limits():
    return {
        "max_bytes": _int_env("FINN_RESEARCH_MAX_BYTES", 35000000),
        "download_s": _int_env("FINN_RESEARCH_DOWNLOAD_S", 75),
        "extract_s": _int_env("FINN_RESEARCH_EXTRACT_S", 80),
        "max_pages": _int_env("FINN_RESEARCH_MAX_PAGES", 600),
        "deadline_s": _int_env("FINN_RESEARCH_DEADLINE_S", 270),
        "max_rss_mb": _int_env("FINN_RESEARCH_MAX_RSS_MB", 380),
    }


class Tracker:
    """Collects stage records and tells a callback after every change."""

    def __init__(self, callback=None):
        self.stages = []
        self.callback = callback
        self.started = time.monotonic()

    def start(self, name, note=None):
        record = {"name": name, "status": "running", "note": note, "seconds": None, "_t": time.monotonic()}
        self.stages.append(record)
        self._tell()
        return record

    def finish(self, record, status, note=None):
        record["status"] = status
        if note is not None:
            record["note"] = note
        record["seconds"] = round(time.monotonic() - record["_t"], 2)
        self._tell()

    def elapsed(self):
        return time.monotonic() - self.started

    def public(self):
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.stages]

    def _tell(self):
        if self.callback:
            try:
                self.callback(self.public())
            except Exception:
                pass


# ------------------------------------------------------------------ discovery

def discovery_for(symbol, session, resolver):
    key = cache.key_for("discovery|" + symbol)
    hit = cache.load("discovery", key, ttl_s=DISCOVERY_TTL_S)
    if hit is not None:
        hit["discovery_source"] = "disk_cache"
        return hit
    info = discover.discover(symbol, session=session, resolver=resolver)
    info["discovery_source"] = "fetched_now"
    info["fetched_at"] = llm_adapter.now_iso()
    if info.get("state") == "ok":
        cache.save("discovery", key, info)
    return info


def requested_urls(info):
    selection = info.get("selection") or {}
    ar = selection.get("annual_report_candidates") or []
    calls = selection.get("concall_candidates") or []
    return (ar[0]["url"] if ar else "", calls[0]["transcript_url"] if calls else "")


def brief_key(symbol, info, model):
    ar_url, call_url = requested_urls(info)
    return cache.key_for("|".join([symbol, ar_url, call_url, model or "", sections.PROMPT_VERSION]))


def peek_cached_brief(symbol, config):
    """A free cache hit: discovery and brief are both cached. Used to skip the live budget."""
    hit = cache.load("discovery", cache.key_for("discovery|" + symbol), ttl_s=DISCOVERY_TTL_S)
    if hit is None or hit.get("state") != "ok":
        return None
    brief = cache.load("brief", brief_key(symbol, hit, config.model), ttl_s=BRIEF_TTL_S)
    if brief is None:
        return None
    return mark_cached(brief)


def mark_cached(brief):
    brief = copy.deepcopy(brief)
    brief["answer_source"] = "cached_brief"
    for doc in brief.get("documents", {}).values():
        if doc.get("status") == "ok":
            doc["fetch"]["source"] = "cached_with_brief"  # fetched_at below is when it was REALLY fetched
    brief["label"] = ("CACHED brief | generated " + str(brief.get("generated_at")) + " | model "
                      + str(brief.get("model")) + " | served from the server's disk cache, not regenerated now")
    return brief


# ------------------------------------------------------------------ documents

CALL_DATE_RES = [
    re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d\d\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December),?\s+20\d\d\b"),
]


def find_call_date(pages):
    for page in pages[:2]:
        for regex in CALL_DATE_RES:
            match = regex.search(page["text"])
            if match:
                return match.group(0)
    return None


def warm_up_bse(session, resolver):
    safe_fetch.fetch(BSE_HOME, session=session, max_bytes=2000000, total_timeout_s=20, resolver=resolver)


TRANSIENT_STATES = ("timeout", "inaccessible", "inaccessible_http")
MONTH_NUMBERS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
                 "nov": 11, "dec": 12}


def label_matches_text(label, text_date):
    """True/False if the month and year in the Screener label agree with the date printed in the transcript;
    None when either is missing or unreadable."""
    if not label or not text_date:
        return None
    label_match = re.search(r"([A-Za-z]{3})[a-z]*\W+((?:19|20)\d\d)", label)
    year_match = re.search(r"(?<!\d)((?:19|20)\d\d)(?!\d)", text_date)
    month_match = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", text_date, re.I)
    if not (label_match and year_match and month_match):
        return None
    return (MONTH_NUMBERS[label_match.group(1).lower()] == MONTH_NUMBERS[month_match.group(1).lower()[:3]]
            and label_match.group(2) == year_match.group(1))


def get_document(kind, candidates, company_name, session, resolver, tracker, lim):
    """Try up to 2 candidates (newest first). Returns (doc, skipped). doc is None if none worked."""
    skipped = []
    attempts = 0
    for entry in candidates:
        if attempts >= 2:
            break
        left = lim["deadline_s"] - tracker.elapsed()
        if left < 45:
            skipped.append({"label": entry.get("label"), "state": "deadline_before_fetch"})
            break
        attempts = attempts + 1
        url = entry.get("url") or entry.get("transcript_url")
        label = entry.get("label") or ""
        if kind == "annual_report":
            title = (company_name or "") + " - " + (label or "Annual report")
            date_label = label
        else:
            title = (company_name or "") + " - Earnings call transcript, " + (label or "date not listed")
            date_label = label or None
        doc_key = cache.key_for("doc|" + url)
        stage = tracker.start(kind + ":" + label)
        cached = cache.load("docs", doc_key)
        if cached is not None:
            record = cached["record"]
            record["fetch"]["source"] = "disk_cache"
            tracker.finish(stage, "ok", "from disk cache; originally fetched " + str(record["fetch"].get("fetched_at")))
            return {"record": record, "pages": cached["pages"]}, skipped
        if safe_fetch.host_of(url) == "www.bseindia.com":
            warm_up_bse(session, resolver)
        got = safe_fetch.fetch(url, session=session, max_bytes=lim["max_bytes"],
                               total_timeout_s=min(lim["download_s"], left - 20),
                               expect_pdf=True, resolver=resolver, referer=BSE_HOME)
        first_try = None
        if not got["ok"] and got["state"] in TRANSIENT_STATES:
            # BSE is sometimes slow to answer. Try the same (newest) document once more, with a longer wait,
            # before settling for an older one.
            left = lim["deadline_s"] - tracker.elapsed()
            if left > 90:
                first_try = {"state": got["state"], "seconds": got["seconds"]}
                SLEEP(2.0)
                got = safe_fetch.fetch(url, session=session, max_bytes=lim["max_bytes"],
                                       total_timeout_s=min(lim["download_s"], left - 40), read_timeout_s=35.0,
                                       expect_pdf=True, resolver=resolver, referer=BSE_HOME)
        if not got["ok"]:
            skipped.append({"label": label, "url": url, "state": got["state"],
                            "tries": 2 if first_try else 1, "first_try": first_try})
            tracker.finish(stage, "failed", "download: " + got["state"] + " after " + str(got["seconds"]) + " s"
                           + (" (second try; first was " + first_try["state"] + ")" if first_try else ""))
            continue
        fetched_bytes = got["bytes"]
        fetched_seconds = got["seconds"]
        left = lim["deadline_s"] - tracker.elapsed()
        extracted = extract.extract_pdf(got["data"], max_pages=lim["max_pages"],
                                        budget_s=max(5, min(lim["extract_s"], left - 40)), max_rss_mb=lim["max_rss_mb"])
        got = None  # release the downloaded bytes before the next step
        if extracted["state"] not in ("ok", "partial"):
            skipped.append({"label": label, "url": url, "state": extracted["state"]})
            tracker.finish(stage, "failed", "extraction: " + extracted["state"] + " after " + str(extracted["seconds"]) + " s")
            continue
        pages = extracted["pages"]
        record = {
            "kind": kind, "title": title.strip(" -"), "label": label, "url": url,
            "host": safe_fetch.host_of(url), "date_label": date_label,
            "date_in_text": find_call_date(pages) if kind == "concall" else None,
            "date_matches_label": None,
            "fetch": {"source": "fetched_now", "fetched_at": llm_adapter.now_iso(), "bytes": fetched_bytes,
                      "seconds": fetched_seconds},
            "extraction": {"state": extracted["state"], "partial_reason": extracted["partial_reason"],
                           "page_count": extracted["page_count"], "pages_extracted": extracted["pages_extracted"],
                           "pdf_title": extracted["pdf_title"], "pdf_created": extracted["pdf_created"],
                           "seconds": extracted["seconds"], "rss_mb_after": extracted.get("rss_mb_after")},
        }
        record["date_matches_label"] = label_matches_text(label, record["date_in_text"])
        cache.save("docs", doc_key, {"record": record, "pages": pages})
        tracker.finish(stage, "ok", str(extracted["pages_extracted"]) + " of " + str(extracted["page_count"])
                       + " pages, " + str(fetched_bytes) + " bytes; download " + str(fetched_seconds)
                       + " s, extract " + str(extracted["seconds"]) + " s")
        return {"record": record, "pages": pages}, skipped
    return None, skipped


# ------------------------------------------------------------------ model sections

def call_model(config, messages, max_tokens, pacer, chat_fn, max_wait_s=70.0):
    estimated = int(sum(len(m["content"]) for m in messages) / 3.0) + max_tokens  # providers count max_tokens too
    waited = pacer.wait_for(estimated, max_wait_s=max_wait_s)
    local = copy.copy(config)
    local.max_tokens = max_tokens
    try:
        raw = chat_fn(local, messages)
    except llm_adapter.LLMRateLimited as error:
        # one patient retry: the provider said how long to wait; give up if that is longer than we can afford
        pause = error.retry_after if error.retry_after is not None else 20.0
        if pause > max_wait_s - waited:
            raise
        SLEEP(pause + 1.0)
        waited = waited + pause + 1.0
        raw = chat_fn(local, messages)
    usage = raw.get("usage") or {}
    pacer.record(usage.get("total_tokens") or estimated)
    parsed = grounded_qa.parse_answer_json(raw["text"])
    if not isinstance(parsed, dict):
        raise ValueError("model reply was not a JSON object")
    return parsed, {"latency_s": raw.get("latency_s"), "usage": usage, "paced_wait_s": round(waited, 1)}


_QUOTE_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def page_of_quote(passage, quote):
    """PDF page where the quote actually sits. A passage can run across a page break, so the page where the
    passage starts is not always the page of the quote."""
    marks = passage.get("page_marks")
    if not marks or len(marks) < 2 or not quote.strip():
        return passage["page"], "passage_page"
    tokens = [re.escape(t) for t in quote.translate(_QUOTE_MAP).split()]
    if not tokens:
        return passage["page"], "passage_page"
    match = re.search(r"\s+".join(tokens), passage["text"].translate(_QUOTE_MAP), re.I)
    if match is None:
        return passage["page"], "passage_page"
    page = marks[0][1]
    for offset, mark_page in marks:
        if offset <= match.start():
            page = mark_page
    return page, "located_in_passage"


def _citation(doc, passage, quote):
    record = doc["record"]
    page, basis = page_of_quote(passage, quote)
    date_text = record.get("date_label") or "date not listed"
    if record.get("date_in_text"):
        date_text = date_text + " (call dated " + record["date_in_text"] + " in the transcript)"
    return {"doc_title": record["title"], "doc_date": date_text, "url": record["url"],
            "pdf_page": page, "page_basis": basis, "excerpt": quote, "speaker": passage.get("speaker"),
            "source_id": passage["source_id"]}


def _public_passages(passages):
    return [{"source_id": p["source_id"], "doc": p.get("doc"), "page": p["page"],
             "speaker": p.get("speaker"), "role": p.get("role"), "text": p["text"]} for p in passages]


def _only_dicts(items):
    return [item for item in (items or []) if isinstance(item, dict)]


def text_section(parsed, passages, docs, note=None):
    parsed = dict(parsed)
    parsed["claims"] = _only_dicts(parsed.get("claims"))
    checks = sections.check_text_claims(parsed, passages)
    shown = []
    rejected = []
    for claim, check in zip(parsed["claims"], checks):
        passage = sections._find_passage(passages, claim.get("source"))
        row = {"text": str(claim.get("text", "")), "auto_pass": check["auto_pass"],
               "checks": {"source_exists": check["source_exists"],
                          "quote_verbatim": check["quote_verbatim_in_source"],
                          "numbers_missing_from_source": check["numbers_missing_from_cited_source"]},
               "citation": None}
        if passage is not None:
            row["citation"] = _citation(docs[passage["doc"]], passage, str(claim.get("quote", "")))
        if check["auto_pass"]:
            shown.append(row)
        else:
            rejected.append(row)
    section = {"summary": str(parsed.get("summary", "")), "claims": shown, "items": [], "failed_checks": rejected,
               "passages": _public_passages(passages), "note": note}
    if parsed.get("not_stated") or (len(shown) == 0 and len(rejected) == 0):
        section["status"] = "not_found"
        section["reason"] = "the retrieved passages do not state this"
        section["summary"] = ""
    elif len(shown) == 0:
        section["status"] = "unverified"
        section["reason"] = "every statement failed the automatic check, so none is shown as a finding"
        section["summary"] = ""
    else:
        section["status"] = "ok"
        if len(rejected) > 0:
            section["summary"] = ""  # the model's summary may rely on claims that failed
    return section


def numbers_section(parsed, passages, docs):
    parsed = dict(parsed)
    parsed["items"] = _only_dicts(parsed.get("items"))
    rows = sections.check_number_items(parsed, passages)
    shown = []
    rejected = []
    for row in rows:
        passage = sections._find_passage(passages, row.get("source"))
        item = dict(row)
        item["citation"] = _citation(docs[passage["doc"]], passage, row["quote"]) if passage else None
        if row["auto_pass"]:
            shown.append(item)
        else:
            rejected.append(item)
    section = {"summary": "", "claims": [], "items": shown, "failed_checks": rejected,
               "passages": _public_passages(passages)}
    if parsed.get("not_stated") or (len(shown) == 0 and len(rejected) == 0):
        section["status"] = "not_found"
        section["reason"] = "no income-statement page with these figures was found in the extracted text"
    elif len(shown) == 0:
        section["status"] = "unverified"
        section["reason"] = "every item failed the automatic check, so none is shown as a finding"
    else:
        section["status"] = "ok"
    return section


def unavailable(reason, message):
    return {"status": "unavailable", "reason": reason, "message": message, "claims": [], "items": [],
            "failed_checks": [], "passages": [], "summary": ""}


def not_found(reason):
    return {"status": "not_found", "reason": reason, "claims": [], "items": [], "failed_checks": [],
            "passages": [], "summary": ""}


def run_section(name, config, messages, max_tokens, pacer, chat_fn, builder, tracker, deadline_s=270):
    stage = tracker.start(name)
    left = deadline_s - tracker.elapsed()
    if left < 15:
        tracker.finish(stage, "failed", "skipped: job time limit reached")
        return unavailable("deadline_exceeded", "the job time limit was reached before this step; nothing was invented")
    try:
        parsed, meta = call_model(config, messages, max_tokens, pacer, chat_fn, max_wait_s=min(70.0, left - 10))
    except llm_adapter.LLMError as error:
        tracker.finish(stage, "failed", "model: " + str(error.code))
        result = unavailable("model_" + str(error.code), "the model call failed; nothing was invented")
        result["provider_detail"] = str(error)[:300]  # already stripped of the key by the adapter
        result["retry_after_s"] = getattr(error, "retry_after", None)
        return result
    except ValueError:
        tracker.finish(stage, "failed", "model reply was not valid JSON")
        return unavailable("model_bad_json", "the model reply could not be parsed; nothing was invented")
    section = builder(parsed)
    section["model_call"] = meta
    tracker.finish(stage, "ok" if section["status"] == "ok" else section["status"],
                   "paced wait " + str(meta["paced_wait_s"]) + " s")
    return section


# ------------------------------------------------------------------ the whole flow

def analyze(symbol, config, callback=None, session=None, resolver=None, chat_fn=None, pacer=None):
    """Run the flow for one validated ticker. Returns {"ok": True, "brief": ...} or {"ok": False, "error": ...}."""
    lim = limits()
    tracker = Tracker(callback)
    session = session or safe_fetch.make_session()
    chat_fn = chat_fn or llm_adapter.chat
    pacer = pacer or PACER

    stage = tracker.start("discover")
    info = discovery_for(symbol, session, resolver)
    state = info.get("state")
    if state != "ok":
        tracker.finish(stage, "failed", str(state))
        return {"ok": False, "stages": tracker.public(),
                "error": {"code": state, "message": ERROR_TEXT.get(state, "could not discover documents")},
                "diagnostics": (info.get("parsed") or {}).get("diagnostics")}
    parsed = info["parsed"]
    company = {"name": parsed["company_name"], "nse_symbol": parsed["nse_symbol"], "bse_code": parsed["bse_code"],
               "screener_url": info["screener_url"], "discovery_source": info["discovery_source"],
               "discovery_fetched_at": info.get("fetched_at"), "parse_mode": parsed["diagnostics"]["parse_mode"]}
    tracker.finish(stage, "ok", info["discovery_source"] + "; " + str(len(parsed["annual_reports"])) + " annual reports and "
                   + str(len(parsed["concalls"])) + " concalls listed")

    key = brief_key(symbol, info, config.model)
    hit = cache.load("brief", key, ttl_s=BRIEF_TTL_S)
    if hit is not None:
        brief = mark_cached(hit)
        brief["stages"] = tracker.public()
        return {"ok": True, "brief": brief}

    selection = info["selection"]
    notes = list(selection["notes"])
    docs = {}
    skipped = {}
    call_doc, skipped["concall"] = get_document("concall", selection["concall_candidates"], parsed["company_name"],
                                                session, resolver, tracker, lim)
    ar_doc, skipped["annual_report"] = get_document("annual_report", selection["annual_report_candidates"],
                                                    parsed["company_name"], session, resolver, tracker, lim)
    if call_doc:
        docs["concall"] = call_doc
    if ar_doc:
        docs["annual_report"] = ar_doc

    documents = {}
    for kind in ("annual_report", "concall"):
        if kind in docs:
            documents[kind] = dict(docs[kind]["record"], skipped=skipped[kind], status="ok")
        else:
            documents[kind] = {"status": "not_available", "skipped": skipped[kind],
                               "reason": _missing_reason(kind, selection, skipped[kind])}

    result_sections = {}
    ar_pages = docs["annual_report"]["pages"] if "annual_report" in docs else None
    call_pages = docs["concall"]["pages"] if "concall" in docs else None

    # outlook
    if call_pages is not None:
        passages, method = sections.outlook_passages(call_pages)
        note = "passages chosen by: " + method
        prompt = sections.OUTLOOK_PROMPT
        if method != "management_turns":
            prompt = sections.OUTLOOK_NO_SPEAKER_PROMPT
            note = ("Speaker roles could not be detected in this transcript, so the passages may include analysts' "
                    "remarks. Read each finding in its source before relying on it.")
        skipped_calls = [x for x in skipped["concall"] if x.get("url")]
        if skipped_calls:
            older = "; ".join(str(x.get("label")) + " (" + str(x.get("state")) + ")" for x in skipped_calls)
            note = ("The newest listed transcript could not be used: " + older + ". This section is from the "
                    + str(docs["concall"]["record"].get("label")) + " call, an older call than the newest listed. " + note)
        if docs["concall"]["record"].get("date_matches_label") is False:
            note = ("The date printed in the transcript (" + str(docs["concall"]["record"].get("date_in_text"))
                    + ") does not match the listed label (" + str(docs["concall"]["record"].get("label"))
                    + "). Check the call date in the source. " + note)
        if len(passages) == 0:
            result_sections["outlook"] = not_found("no passage in the transcript mentions guidance or outlook terms")
        else:
            messages = sections.build_messages(prompt, passages,
                                               "Question: What explicit forward-looking statements did management make?")
            result_sections["outlook"] = run_section(
                "model:outlook", config, messages, 600, pacer, chat_fn,
                lambda p: text_section(p, passages, docs, note), tracker, lim["deadline_s"])
    else:
        result_sections["outlook"] = not_found("no earnings-call transcript could be retrieved ("
                                               + documents["concall"]["reason"] + ")")

    if ar_pages is not None:
        passages = sections.overview_passages(ar_pages)
        if len(passages) == 0:
            result_sections["overview"] = not_found("no passage in the annual report describes the business")
        else:
            messages = sections.build_messages(sections.OVERVIEW_PROMPT, passages,
                                               "Question: What does the company do?")
            result_sections["overview"] = run_section(
                "model:overview", config, messages, 500, pacer, chat_fn,
                lambda p: text_section(p, passages, docs), tracker, lim["deadline_s"])
        passages = sections.numbers_passages(ar_pages)
        if len(passages) == 0:
            result_sections["numbers"] = not_found("no page in the extracted annual-report text looks like an income statement")
        else:
            messages = sections.build_messages(sections.NUMBERS_PROMPT, passages,
                                               "Task: report the selected numbers.")
            result_sections["numbers"] = run_section(
                "model:numbers", config, messages, 600, pacer, chat_fn,
                lambda p: numbers_section(p, passages, docs), tracker, lim["deadline_s"])
    else:
        reason = "no annual report could be retrieved (" + documents["annual_report"]["reason"] + ")"
        result_sections["overview"] = not_found(reason)
        result_sections["numbers"] = not_found(reason)

    brief = {
        "ticker": symbol, "company": company, "answer_source": "live_model",
        "label": "LIVE brief | generated " + llm_adapter.now_iso() + " | model " + str(config.model),
        "generated_at": llm_adapter.now_iso(), "model": config.model, "prompt_version": sections.PROMPT_VERSION,
        "documents": documents, "sections": result_sections, "notes": notes,
        "limitations": limitations(documents, result_sections),
        "stages": tracker.public(), "total_seconds": round(tracker.elapsed(), 1), "disclaimer": DISCLAIMER,
    }
    found = [name for name, section in result_sections.items() if section["status"] == "ok"]
    brief["outcome"] = "complete" if len(found) == 3 else ("partial" if found else "no_findings")
    brief["sections_found"] = found
    any_unavailable = any(s["status"] == "unavailable" for s in result_sections.values())
    if not any_unavailable and docs:
        cache.save("brief", key, brief)
    return {"ok": True, "brief": brief}


ERROR_TEXT = {
    "ticker_not_found": "Screener has no company page for this ticker.",
    "source_unreachable": "The document index (Screener) could not be reached.",
    "company_not_resolved": "The page did not identify a company.",
    "ticker_mismatch": "The page found is for a different NSE symbol.",
    "documents_not_listed": "The company page lists no annual reports or concalls.",
}


def _missing_reason(kind, selection, skipped):
    key = "annual_report_candidates" if kind == "annual_report" else "concall_candidates"
    if len(selection[key]) == 0 and len(skipped) == 0:
        too_old = [n for n in selection["notes"] if n.startswith("Skipped as too old")]
        if kind == "concall" and too_old:
            return "no recent transcript on a trusted host. " + too_old[0]
        listed_but_untrusted = [n for n in selection["notes"] if kind.split("_")[0] in n.lower() or "transcript" in n.lower()]
        if listed_but_untrusted:
            return "none listed on a trusted host; " + listed_but_untrusted[0]
        return "none listed with a usable link"
    states = [str(s.get("state")) for s in skipped]
    return "listed but not retrievable: " + ", ".join(states)


def limitations(documents, result_sections):
    items = ["Page numbers are PDF page indexes, which can differ from printed page numbers.",
             "Only the two named documents are used; nothing from news, filings elsewhere or the web.",
             "The disk cache is lost when the free server restarts or is redeployed."]
    for kind in ("annual_report", "concall"):
        doc = documents[kind]
        if doc["status"] != "ok":
            items.append(kind.replace("_", " ") + " unavailable: " + doc["reason"])
            continue
        if doc["extraction"]["state"] == "partial":
            items.append(kind.replace("_", " ") + " extracted only partly (" + str(doc["extraction"]["partial_reason"])
                         + "): " + str(doc["extraction"]["pages_extracted"]) + " of "
                         + str(doc["extraction"]["page_count"]) + " pages.")
        if doc["skipped"]:
            items.append(kind.replace("_", " ") + ": a newer candidate was skipped, an older one was used ("
                         + ", ".join(str(s.get("state")) for s in doc["skipped"]) + ").")
    return items


PACER = pacer_module.TokenPacer()
SLEEP = time.sleep
