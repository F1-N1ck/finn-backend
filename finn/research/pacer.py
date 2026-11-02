"""Keeps model calls under the provider's tokens-per-minute limit (Groq free key: 8,000/min)."""

import os
import threading
import time


class TokenPacer:
    def __init__(self, limit_per_minute=None):
        if limit_per_minute is None:
            try:
                limit_per_minute = int(os.environ.get("FINN_LLM_TPM", "7000"))
            except ValueError:
                limit_per_minute = 7000
        self.limit = limit_per_minute
        self.events = []
        self.lock = threading.Lock()

    def wait_for(self, estimated_tokens, sleep=time.sleep, now=time.time, max_wait_s=70.0):
        """Block until the call fits in the last-60-seconds budget. Returns seconds waited."""
        waited = 0.0
        while True:
            with self.lock:
                current = now()
                self.events = [e for e in self.events if current - e[0] < 60]
                used = sum(e[1] for e in self.events)
                if used + estimated_tokens <= self.limit or len(self.events) == 0:
                    return waited
                oldest = self.events[0][0]
                pause = 60 - (current - oldest) + 0.5
            if waited + pause > max_wait_s:
                return waited
            sleep(pause)
            waited = waited + pause

    def record(self, tokens, now=time.time):
        with self.lock:
            self.events.append((now(), tokens))
