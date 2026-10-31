"""Read-only access to the committed demo snapshot (extracted text + cached answers).

Render's free filesystem is wiped on restart/redeploy, so nothing is ever written here at
runtime. The snapshot lives in the git repo and ships with every deploy.
"""

import hashlib
import json
import os


def snapshot_dir():
    return os.environ.get("FINN_SNAPSHOT_DIR", "data/snapshot")


def sha256_of(path):
    digest = hashlib.sha256()
    file_handle = open(path, "rb")
    while True:
        block = file_handle.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
    file_handle.close()
    return digest.hexdigest()


def load_manifest(directory=None):
    directory = directory or snapshot_dir()
    path = os.path.join(directory, "MANIFEST.json")
    if not os.path.exists(path):
        return None
    file_handle = open(path)
    data = json.load(file_handle)
    file_handle.close()
    return data


def verify(directory=None):
    """Check that committed files still match the manifest hashes."""
    directory = directory or snapshot_dir()
    manifest = load_manifest(directory)
    if manifest is None:
        return {"ok": False, "reason": "MANIFEST.json missing", "files": {}}
    files = {}
    all_ok = True
    for name in manifest.get("files", {}):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            files[name] = "missing"
            all_ok = False
            continue
        matches = sha256_of(path) == manifest["files"][name]["sha256"]
        files[name] = "ok" if matches else "hash_mismatch"
        if not matches:
            all_ok = False
    return {"ok": all_ok, "retrieved": manifest.get("retrieved"), "files": files}


def load_pages(doc_id, directory=None):
    directory = directory or snapshot_dir()
    path = os.path.join(directory, "text", doc_id + ".pages.jsonl")
    if not os.path.exists(path):
        return None
    pages = []
    file_handle = open(path)
    for line in file_handle:
        pages.append(json.loads(line))
    file_handle.close()
    return pages


def cache_path(directory=None):
    return os.path.join(directory or snapshot_dir(), "demo_answers.json")
