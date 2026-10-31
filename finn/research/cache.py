"""Small disk cache (JSON files). On Render's free plan the disk is wiped on restart/redeploy,
so this only helps within one running instance. Every failure here is non-fatal."""

import hashlib
import json
import os
import time


def cache_dir():
    return os.environ.get("FINN_CACHE_DIR", "/tmp/finn_cache")


def key_for(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _path(namespace, key):
    return os.path.join(cache_dir(), namespace, key + ".json")


def load(namespace, key, ttl_s=None):
    path = _path(namespace, key)
    try:
        if ttl_s is not None and time.time() - os.path.getmtime(path) > ttl_s:
            return None
        with open(path) as handle:
            return json.load(handle)
    except Exception:
        return None


def save(namespace, key, value):
    path = _path(namespace, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp" + str(os.getpid())
        with open(temp, "w") as handle:
            json.dump(value, handle)
        os.replace(temp, path)
        prune(max_mb=int(os.environ.get("FINN_CACHE_MAX_MB", "120")))
        return True
    except Exception:
        return False


def prune(max_mb):
    """Delete the oldest files until the cache is under max_mb."""
    try:
        files = []
        total = 0
        for folder, _, names in os.walk(cache_dir()):
            for name in names:
                path = os.path.join(folder, name)
                size = os.path.getsize(path)
                files.append((os.path.getmtime(path), path, size))
                total = total + size
        files.sort()
        while total > max_mb * 1024 * 1024 and files:
            _, path, size = files.pop(0)
            os.remove(path)
            total = total - size
    except Exception:
        pass
