"""Keeps model calls under the provider's per-minute limits.

Two budgets, because Groq enforces both: total tokens per minute (free key: about 8,000) and, on the key this app
was deployed with, OUTPUT tokens per minute. The saved 429 from the Tata Elxsi run said so in the provider's own
words: "... on output tokens per minute (OTPM): Limit 1000, Used 625, Requested 600". A total-tokens pacer cannot
prevent that error, because three calls that each ask for 500-600 output tokens never fit in 1,000 within a minute
even when the total is far below 7,000.

  FINN_LLM_TPM   total tokens per minute budget (default 7000)
  FINN_LLM_OTPM  output tokens per minute budget (default 1000, as reported by the provider; 0 turns it off, for
                 keys on a higher tier)
"""

import os
import threading
import time


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class TokenPacer:
    def __init__(self, limit_per_minute=None, output_limit_per_minute=None):
        if limit_per_minute is None:
            limit_per_minute = _env_int("FINN_LLM_TPM", 7000)
        if output_limit_per_minute is None:
            output_limit_per_minute = _env_int("FINN_LLM_OTPM", 1000)
        self.limit = limit_per_minute
        self.output_limit = output_limit_per_minute
        self.events = []  # (time, total_tokens, output_tokens)
        self.lock = threading.Lock()

    def wait_for(self, estimated_tokens, sleep=time.sleep, now=time.time, max_wait_s=70.0, estimated_output=0):
        """Block until the call fits in the last-60-seconds budgets. Returns seconds waited."""
        waited = 0.0
        while True:
            with self.lock:
                current = now()
                self.events = [e for e in self.events if current - e[0] < 60]
                used = sum(e[1] for e in self.events)
                used_output = sum(e[2] for e in self.events)
                fits_total = used + estimated_tokens <= self.limit
                fits_output = (not self.output_limit) or used_output + estimated_output <= self.output_limit
                if (fits_total and fits_output) or len(self.events) == 0:
                    return waited
                oldest = self.events[0][0]
                pause = 60 - (current - oldest) + 0.5
            if waited + pause > max_wait_s:
                return waited
            sleep(pause)
            waited = waited + pause

    def record(self, tokens, now=time.time, output_tokens=None):
        """`output_tokens` None means unknown: count the whole call as output, the safe side for the output budget."""
        with self.lock:
            self.events.append((now(), tokens, tokens if output_tokens is None else output_tokens))
