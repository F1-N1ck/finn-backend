"""Promote ONE manually verified LIVE result into the committed demo snapshot.

Usage (run on your Mac, after you have saved the host-check JSON and checked every claim):
    python scripts/promote_to_snapshot.py results/host_check.json Q1_concall_guidance "Nikhil"

It copies the result and its passages into data/snapshot/demo_answers.json with
source_type "model" and your name/date as reviewer. It refuses anything that is not a
LIVE model result, or whose mechanical checks did not all pass. Then commit and redeploy.
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finn import grounded_qa
from finn import llm_adapter
from finn import snapshot


def main():
    if len(sys.argv) != 4:
        print("usage: promote_to_snapshot.py <host_check.json> <question_id> <reviewer name>")
        return 2
    report_path, question_id, reviewer = sys.argv[1], sys.argv[2], sys.argv[3]
    file_handle = open(report_path)
    report = json.load(file_handle)
    file_handle.close()
    # the file may be the polled job (report nested) or the report itself
    if "report" in report:
        report = report["report"]

    requests_list = (report.get("hosted_model") or {}).get("requests") or []
    record = None
    for item in requests_list:
        if item.get("id") == question_id:
            record = item
    if record is None:
        print("no such question in the report")
        return 1
    if record.get("outcome") != "live":
        print("refusing: outcome is", record.get("outcome"), "- only LIVE model results can be promoted")
        return 1
    check = record.get("auto_check") or {}
    if check.get("claim_count", 0) == 0 or check.get("auto_pass_count") != check.get("claim_count") \
            or check.get("answer_numbers_not_in_passages"):
        print("refusing: mechanical checks did not all pass")
        return 1

    question = None
    for candidate in grounded_qa.QUESTIONS:
        if candidate["id"] == question_id:
            question = candidate
    key = question["doc"] + "|" + question_id
    today = date.today().isoformat()
    entry = {
        "source_type": "model",
        "model": record.get("model"),
        "generated_at": record.get("generated_at"),
        "verification": "mechanical checks passed; claims and quotes manually checked against the source PDF by "
                        + reviewer + " on " + today,
        "result": record["result"],
        "passages": record["passages"],
        "meta": record.get("meta"),
    }
    llm_adapter.save_cache_entry(snapshot.cache_path(), key, entry)
    print("promoted", key, "into data/snapshot/demo_answers.json. Now commit and redeploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
