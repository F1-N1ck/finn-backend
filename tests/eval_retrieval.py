"""Retrieval evaluation on the KPIT Q1 FY27 transcript (ONE document).

Ground truth = PDF pages where I (Claude) read management giving forward guidance:
4, 6, 7, 9, 11, 16. Weakness: one transcript, ground truth chosen by the same person who
tuned the term list, so recall here is optimistic. The ablation (old terms, new structure)
shows how much comes from turn/role structure rather than vocabulary.
Run: python tests/eval_retrieval.py data/text/KPIT_CONCALL_AUG2026.pages.jsonl
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from finn import retrieval, grounded_qa

GROUND_TRUTH_PAGES = {4, 6, 7, 9, 11, 16}
KEY_PHRASES = ["h2 will be better than h1", "back to the growth", "return to growth in h2"]


def load(path):
    pages = []
    file_handle = open(path)
    for line in file_handle:
        pages.append(json.loads(line))
    file_handle.close()
    return pages


def report(name, passages):
    pages_hit = sorted({p["page"] for p in passages} & GROUND_TRUTH_PAGES)
    joined = " ".join(p["text"].lower().replace("\n", " ") for p in passages)
    has_key = False
    for phrase in KEY_PHRASES:
        if phrase in joined:
            has_key = True
    roles = [p.get("role", "-") for p in passages]
    print("%-34s k=%d pages=%s ground_truth_pages_hit=%s key_phrase_present=%s roles=%s" % (
        name, len(passages), [p["page"] for p in passages], pages_hit, has_key, roles))


def main():
    pages = load(sys.argv[1])
    print("management surnames parsed:", sorted(retrieval.management_surnames(pages)))
    turns = retrieval.split_turns(pages)
    roles = {}
    for turn in turns:
        roles[turn["role"]] = roles.get(turn["role"], 0) + 1
    print("turns:", len(turns), roles)
    for k in (3, 4, 5):
        old = grounded_qa.passages_by_windows(pages, retrieval.OLD_TERMS, top_k=k)
        report("OLD page-window, old terms", old)
        ablation = retrieval.retrieve_turns(pages, retrieval.OLD_TERMS, top_k=k)
        report("NEW turns+roles, OLD terms", ablation)
        new = retrieval.retrieve_turns(pages, retrieval.GUIDANCE_TERMS, top_k=k)
        report("NEW turns+roles, NEW terms", new)
        print()


main()
