"""Grounded question answering: passages with source IDs in, checked answer out.

The model may only use the numbered passages. Every claim must name a source ID and
quote that passage verbatim. check_claims() verifies the mechanical parts (source ID
exists, quote is verbatim, numbers appear in the cited passage). It does NOT verify
meaning; a human check is still required before an answer is marked as checked.
"""

import json
import re

from finn import llm_adapter
from finn import retrieval

SYSTEM_TEXT = (
    "You answer strictly from the numbered passages. Never use outside knowledge.\n"
    'Return JSON only: {"answer": string, "not_stated": boolean, '
    '"claims": [{"text": string, "source": "S1", "quote": string}]}.\n'
    "Every quote must be copied verbatim from the cited passage. "
    "If the passages do not contain the answer, set not_stated to true and claims to []."
)

QUESTIONS = [
    {
        "id": "Q1_concall_guidance",
        "doc": "KPIT_CONCALL_AUG2026",
        "symbol": "KPITTECH",
        "short": "Forward guidance (Q1 FY27 concall)",
        "question": "What explicit forward guidance did management give on revenue growth or margins? "
                    "If none is stated in the passages, say so.",
        "mode": "turns",
        "top_k": 4,
    },
    {
        "id": "Q2_ar_headline_numbers",
        "doc": "KPIT_AR_2026",
        "symbol": "KPITTECH",
        "short": "Headline numbers (Annual Report FY26)",
        "question": "What were revenue from operations and profit for the year in the consolidated "
                    "statement of profit and loss, and for which year? Use only numbers in the passages.",
        "mode": "required_phrases",
        "required": ["consolidated statement of profit and loss", "revenue from operations", "profit for the year"],
    },
]


# ---------------------------------------------------------------- passages

def passages_by_windows(pages, keywords, top_k=3, size=1800, step=600):
    scored = []
    for page in pages:
        text = page["text"]
        lowered = text.lower()
        best_score = 0
        best_start = 0
        start = 0
        while start < max(1, len(text) - size + step):
            chunk = lowered[start:start + size]
            score = 0
            for keyword in keywords:
                weight = 1
                if isinstance(keywords, dict):
                    weight = keywords[keyword]
                score = score + weight * chunk.count(keyword)
            if score > best_score:
                best_score = score
                best_start = start
            start = start + step
        if best_score > 0:
            scored.append((best_score, page["page"], best_start, text[best_start:best_start + size]))
    scored.sort(reverse=True)
    passages = []
    for rank, item in enumerate(scored[:top_k]):
        passages.append({"source_id": "S" + str(rank + 1), "page": item[1],
                         "char_start": item[2], "text": item[3]})
    return passages


def passages_by_required_phrases(pages, required, top_k=2, max_chars=6000):
    """Whole-page passages. Used for financial statements, where the statement title can
    sit at the END of the page text (seen on the KPIT annual report)."""
    matches = []
    for page in pages:
        lowered = page["text"].lower()
        all_found = True
        for phrase in required:
            if phrase not in lowered:
                all_found = False
        if all_found:
            matches.append(page)
    passages = []
    for rank, page in enumerate(matches[:top_k]):
        passages.append({"source_id": "S" + str(rank + 1), "page": page["page"],
                         "char_start": 0, "text": page["text"][:max_chars]})
    return passages


def build_passages(question, pages):
    if question["mode"] == "required_phrases":
        return passages_by_required_phrases(pages, question["required"])
    if question["mode"] == "turns":
        return retrieval.retrieve_turns(pages, retrieval.GUIDANCE_TERMS, top_k=question.get("top_k", 4))
    return passages_by_windows(pages, question["keywords"])


def build_messages(question_text, passages):
    blocks = []
    for passage in passages:
        header = "[" + passage["source_id"] + "] (PDF page " + str(passage["page"])
        if passage.get("speaker"):
            header = header + ", speaker " + passage["speaker"] + " (" + passage.get("role", "") + ")"
        blocks.append(header + ")\n" + passage["text"])
    user_text = "\n\n".join(blocks) + "\n\nQuestion: " + question_text
    return [{"role": "system", "content": SYSTEM_TEXT}, {"role": "user", "content": user_text}]


# ---------------------------------------------------------------- parsing + checking

def parse_answer_json(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    raise ValueError("no valid JSON object in the model reply")


def normalise(text):
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def numbers_in(text):
    found = re.findall(r"\d[\d,]*\.?\d*", text)
    cleaned = []
    for token in found:
        token = token.replace(",", "").rstrip(".")
        if token != "":
            cleaned.append(token)
    return cleaned


def year_alias_in(number, source_numbers):
    """A source that says full year '27 supports a claim that says 2027, and the other way round."""
    if len(number) == 4 and number[:2] in ("19", "20"):
        return number[2:] in source_numbers
    if len(number) == 2:
        return ("20" + number) in source_numbers or ("19" + number) in source_numbers
    return False


def check_claims(parsed, passages, year_aliases=False):
    by_id = {}
    all_numbers = set()
    for passage in passages:
        by_id[passage["source_id"]] = passage["text"]
        for number in numbers_in(passage["text"]):
            all_numbers.add(number)

    report = {"claims": [], "answer_numbers_not_in_passages": []}
    claims = parsed.get("claims") or []
    passed = 0
    for claim in claims:
        source = claim.get("source")
        quote = claim.get("quote") or ""
        text = claim.get("text") or ""
        row = {"text": text, "source": source, "quote": quote}
        row["source_exists"] = source in by_id
        source_numbers = set()
        row["quote_verbatim_in_source"] = False
        if row["source_exists"]:
            row["quote_verbatim_in_source"] = normalise(quote) in normalise(by_id[source])
            source_numbers = set(numbers_in(by_id[source]))
        missing = []
        for number in numbers_in(text):
            if number not in source_numbers:
                if year_aliases and year_alias_in(number, source_numbers):
                    continue
                missing.append(number)
        row["numbers_missing_from_cited_source"] = missing
        row["auto_pass"] = (row["source_exists"] and row["quote_verbatim_in_source"] and len(missing) == 0)
        if row["auto_pass"]:
            passed = passed + 1
        report["claims"].append(row)

    for number in numbers_in(parsed.get("answer") or ""):
        if number not in all_numbers:
            report["answer_numbers_not_in_passages"].append(number)
    report["claim_count"] = len(claims)
    report["auto_pass_count"] = passed
    report["note"] = ("Mechanical checks only (source ID, verbatim quote, numbers). "
                      "Meaning must be checked by a human.")
    return report


# ---------------------------------------------------------------- run one question

def verification_text(parsed, passages):
    if not isinstance(parsed, dict):
        return "automatic checks: not run"
    report = check_claims(parsed, passages)
    text = "automatic checks: " + str(report["auto_pass_count"]) + "/" + str(report["claim_count"]) + " claims passed"
    if report["answer_numbers_not_in_passages"]:
        text = text + "; numbers in answer not in passages: " + ",".join(report["answer_numbers_not_in_passages"])
    return text + "; no human review"


def run_question(config, question, pages, cache_path, allow_live=True):
    passages = build_passages(question, pages)
    record = {"id": question["id"], "question": question["question"]}
    if len(passages) == 0:
        record["outcome"] = "no_passages"
        record["error"] = "no passage matched; not calling the model"
        return record
    messages = build_messages(question["question"], passages)
    cache_key = question["doc"] + "|" + question["id"]

    def verification_fn(parsed):
        return verification_text(parsed, passages)

    try:
        outcome = llm_adapter.answer_with_fallback(config, cache_key, messages, parse_answer_json,
                                                   cache_path, allow_live=allow_live,
                                                   verification_fn=verification_fn)
    except llm_adapter.NoAnswerAvailable as error:
        record["outcome"] = "no_answer"
        record["passages"] = passages
        record["error_code"] = error.reason_code
        record["error_message"] = error.reason_message
        return record
    record["outcome"] = outcome["mode"]
    record["label"] = outcome["label"]
    record["generated_at"] = outcome["generated_at"]
    record["model"] = outcome["model"]
    record["source_type"] = outcome.get("source_type", "model")
    record["result"] = outcome["result"]
    record["meta"] = outcome.get("meta")
    if outcome["mode"] == "live":
        record["passages"] = passages
        if isinstance(outcome["result"], dict):
            record["auto_check"] = check_claims(outcome["result"], passages)
    else:
        record["passages"] = outcome.get("passages") or []
        record["fallback_reason"] = outcome.get("fallback_reason")
        if isinstance(outcome["result"], dict) and record["passages"]:
            record["auto_check"] = check_claims(outcome["result"], record["passages"])
    return record
