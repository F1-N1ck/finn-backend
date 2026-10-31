"""Operator script (run on your Mac, then commit data/snapshot/).
Copies extracted page text into data/snapshot/text and writes MANIFEST.json with hashes.
Usage: python scripts/build_snapshot.py data/text
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from finn import snapshot
import extract_test

source_dir = sys.argv[1] if len(sys.argv) > 1 else "data/text"
target_dir = os.path.join("data", "snapshot")
os.makedirs(os.path.join(target_dir, "text"), exist_ok=True)

manifest = {"retrieved": "2026-10-31", "note": "Public filings from BSE; text extracted with PyMuPDF.", "files": {}}
for doc in extract_test.DOCS:
    name = doc["id"] + ".pages.jsonl"
    source = os.path.join(source_dir, name)
    target = os.path.join(target_dir, "text", name)
    shutil.copyfile(source, target)
    pages = 0
    file_handle = open(target)
    for line in file_handle:
        pages = pages + 1
    file_handle.close()
    manifest["files"]["text/" + name] = {"sha256": snapshot.sha256_of(target), "pages": pages,
                                         "source_url": doc["url"], "kind": doc["kind"]}
file_handle = open(os.path.join(target_dir, "MANIFEST.json"), "w")
json.dump(manifest, file_handle, indent=2)
file_handle.close()
print("snapshot written:", json.dumps({k: v["pages"] for k, v in manifest["files"].items()}))
