"""Operator script. Writes the two OPERATOR-WRITTEN cached answers into data/snapshot/demo_answers.json.
These are NOT model output. Each quote is checked mechanically against the snapshot text before
writing; the script aborts if any check fails.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from finn import snapshot, grounded_qa

CHECKED_ON = "2026-10-31"
VERIFICATION = ("mechanical check + manual read by Claude on " + CHECKED_ON +
                " against the cited page text; no human review")


def page_text(pages, number):
    for page in pages:
        if page["page"] == number:
            return page["text"]
    raise SystemExit("page missing: " + str(number))


def passage_around(text, quote, source_id, page, speaker=None, role=None, context=500):
    flat = re.sub(r"\s+", " ", text)
    index = flat.lower().find(re.sub(r"\s+", " ", quote).lower())
    if index == -1:
        raise SystemExit("quote not found on page " + str(page) + ": " + quote)
    start = max(0, index - context)
    end = min(len(flat), index + len(quote) + context)
    passage = {"source_id": source_id, "page": page, "char_start": start, "text": flat[start:end]}
    if speaker:
        passage["speaker"] = speaker
        passage["role"] = role
    return passage


def main():
    concall = snapshot.load_pages("KPIT_CONCALL_AUG2026")
    annual = snapshot.load_pages("KPIT_AR_2026")
    entries = {}

    # Q2: whole statement page (so the title at the end of the page is included)
    q2_text = page_text(annual, 149)
    q2_passages = [{"source_id": "S1", "page": 149, "char_start": 0, "text": q2_text}]
    q2_claims = [
        {"text": "Consolidated revenue from operations: 64,549.31 (year ended 31 March 2026) vs 58,423.45 (prior year), Rs million.",
         "source": "S1", "quote": "Revenue from operations 35 64,549.31 58,423.45"},
        {"text": "Consolidated profit for the year: 6,373.70 (year ended 31 March 2026) vs 8,396.00 (prior year), Rs million.",
         "source": "S1", "quote": "Profit for the year 6,373.70 8,396.00"},
        {"text": "The page is the consolidated statement of profit and loss.",
         "source": "S1", "quote": "CONSOLIDATED STATEMENT OF PROFIT AND LOSS"},
    ]
    q2_result = {"answer": "Consolidated, year ended 31 March 2026 (Rs million): revenue from operations 64,549.31 "
                           "(prior year 58,423.45); profit for the year 6,373.70 (prior year 8,396.00).",
                 "not_stated": False, "claims": q2_claims}
    entries["KPIT_AR_2026|Q2_ar_headline_numbers"] = (q2_result, q2_passages)

    # Q1: management guidance statements, verbatim from named pages
    p4 = page_text(concall, 4)
    p7 = page_text(concall, 7)
    p9 = page_text(concall, 9)
    q1_passages = [
        passage_around(p4, "we have just mentioned that H2 will be better than H1, and we will return to growth in H2 by Q4",
                       "S1", 4, "Kishor Patil", "management"),
        passage_around(p9, "the margins will improve, but it will improve incrementally, not significantly until our revenues go back to growing ways",
                       "S2", 9, "Kishor Patil", "management"),
        passage_around(p7, "On the commercial vehicle, we will be on for growth next quarter",
                       "S3", 7, "Kishor Patil", "management"),
        passage_around(p7, "the medium-term outlook remains good",
                       "S4", 7, "Kishor Patil", "management"),
    ]
    q1_claims = [
        {"text": "Management said H2 will be better than H1 and the company will return to growth in H2 by Q4.",
         "source": "S1", "quote": "we have just mentioned that H2 will be better than H1, and we will return to growth in H2 by Q4"},
        {"text": "Margins will improve incrementally, not significantly, until revenues return to growth.",
         "source": "S2", "quote": "the margins will improve, but it will improve incrementally, not significantly until our revenues go back to growing ways"},
        {"text": "Commercial vehicle revenue is expected to grow next quarter.",
         "source": "S3", "quote": "On the commercial vehicle, we will be on for growth next quarter"},
        {"text": "Management said the medium-term outlook remains good.",
         "source": "S4", "quote": "the medium-term outlook remains good"},
    ]
    q1_result = {"answer": "Guidance in these passages is qualitative: H2 better than H1 with a return to growth by Q4; "
                           "margins improve only incrementally until revenue grows; commercial vehicle growth next quarter; "
                           "medium-term outlook remains good. No new numeric growth or margin figure is given in these passages.",
                 "not_stated": False, "claims": q1_claims}
    entries["KPIT_CONCALL_AUG2026|Q1_concall_guidance"] = (q1_result, q1_passages)

    cache = {}
    for key in entries:
        result, passages = entries[key]
        report = grounded_qa.check_claims(result, passages)
        print(key, "auto_pass", report["auto_pass_count"], "/", report["claim_count"],
              "unsupported answer numbers:", report["answer_numbers_not_in_passages"])
        if report["auto_pass_count"] != report["claim_count"] or report["answer_numbers_not_in_passages"]:
            raise SystemExit("mechanical check failed for " + key)
        cache[key] = {"source_type": "operator_extract", "model": None, "generated_at": CHECKED_ON,
                      "verification": VERIFICATION, "result": result, "passages": passages}
    path = snapshot.cache_path()
    file_handle = open(path, "w")
    json.dump(cache, file_handle, indent=2)
    file_handle.close()
    print("wrote", path)


main()
