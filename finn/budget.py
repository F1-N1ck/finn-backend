"""In-memory limiter for LIVE model calls (protects the provider's free-tier quota).
State resets when the instance restarts or spins down; that is acceptable because the
provider enforces its own limits too. Defaults are deliberately low."""

import threading
import time


class LiveBudget:
    def __init__(self, per_minute, per_day):
        self.per_minute = per_minute
        self.per_day = per_day
        self.minute_stamps = []
        self.day_stamps = []
        self.lock = threading.Lock()

    def try_acquire(self, now=None):
        if now is None:
            now = time.time()
        with self.lock:
            self.minute_stamps = [t for t in self.minute_stamps if now - t < 60]
            self.day_stamps = [t for t in self.day_stamps if now - t < 86400]
            if len(self.minute_stamps) >= self.per_minute:
                return False, "live_budget_exhausted_per_minute"
            if len(self.day_stamps) >= self.per_day:
                return False, "live_budget_exhausted_per_day"
            self.minute_stamps.append(now)
            self.day_stamps.append(now)
            return True, None
