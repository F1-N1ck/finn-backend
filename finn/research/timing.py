"""Evidence for slow polls on the free server. Nothing here changes behaviour; it only records.

Why: in test runs a status poll sometimes took 10-46 s while the job itself was healthy. The cause was not known, so
this module records what can tell the candidates apart, and the poll response carries it back to the test script:

  * a probe THREAD that sleeps 0.25 s and notes any oversleep. If a whole Python process stops being scheduled
    (CPU quota throttling, a long C call that keeps the interpreter lock, the VM being paused) the probe oversleeps.
    It also records how much CPU time the process used during the oversleep: about as much as the oversleep means
    the process was busy computing; close to zero means it was not running at all.
  * a probe TASK on the web server's event loop. If only this one oversleeps, something blocked the event loop
    while other threads ran.
  * per request: when the web app first saw the request and when it started the response, sent back as the headers
    X-Received-At and X-Responded-At (seconds since the epoch). Compared with the client's own send and receive
    times they split a slow poll into: before the app saw it / inside the handler / after the handler.
  * which pipeline stage was running at the time.

Only numbers and stage names are recorded: no URLs, no document text, no keys.
"""

import asyncio
import logging
import threading
import time
from collections import deque

LOG = logging.getLogger("finn.timing")
INTERVAL_S = 0.25
THRESHOLD_S = 0.75
KEEP = 40

_events = deque(maxlen=KEEP)
_slow_requests = deque(maxlen=KEEP)
_state = {"stage": None, "thread_started": False}
_lock = threading.Lock()
BOOTED_AT = time.time()


def set_stage(name):
    _state["stage"] = name


def _cpu():
    return time.process_time()


def record_event(kind, lag_s, cpu_during_s):
    event = {"kind": kind, "at": round(time.time(), 3), "lag_s": round(lag_s, 2),
             "cpu_during_s": None if cpu_during_s is None else round(cpu_during_s, 2), "stage": _state["stage"]}
    with _lock:
        _events.append(event)
    LOG.warning("probe_lag kind=%s lag_s=%.2f cpu_during_s=%s stage=%s", kind, lag_s, event["cpu_during_s"], event["stage"])


def _thread_probe():
    while True:
        wall0 = time.monotonic()
        cpu0 = _cpu()
        time.sleep(INTERVAL_S)
        lag = time.monotonic() - wall0 - INTERVAL_S
        if lag >= THRESHOLD_S:
            record_event("thread", lag, _cpu() - cpu0)


def start_thread_probe():
    with _lock:
        if _state["thread_started"]:
            return
        _state["thread_started"] = True
    threading.Thread(target=_thread_probe, daemon=True, name="timing-probe").start()


async def loop_probe():
    while True:
        wall0 = time.monotonic()
        await asyncio.sleep(INTERVAL_S)
        lag = time.monotonic() - wall0 - INTERVAL_S
        if lag >= THRESHOLD_S:
            record_event("event_loop", lag, None)


def note_request(path_kind, received_at, responded_at):
    duration = responded_at - received_at
    if duration >= 1.0:
        with _lock:
            _slow_requests.append({"path": path_kind, "received_at": round(received_at, 3),
                                   "handler_s": round(duration, 2), "stage": _state["stage"]})
        LOG.warning("slow_handler path=%s handler_s=%.2f stage=%s", path_kind, duration, _state["stage"])


def _rss_mb():
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


def cpu_seconds():
    """CPU time this process has used. A stage whose wall time is far above its CPU time was waiting (for the
    network, the disk, or for the CPU to be given back to it), not computing."""
    return _cpu()


def _read_kv(path):
    values = {}
    try:
        with open(path) as handle:
            for line in handle:
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                    values[parts[0]] = int(parts[1])
    except Exception:
        return None
    return values


def host_cpu_limits():
    """What the platform says about CPU sharing, so 'the process was starved of CPU' can be checked instead of
    guessed. Every source may be missing (not Linux, no cgroup); missing keys are simply absent.
      cgroup_cpu_stat  nr_periods / nr_throttled / throttled_usec (cgroup v2): time the container was held back
      cgroup_cpu_max   the quota, for example '10000 100000' = 10 % of one CPU
      psi_cpu_some     'some avg10/avg60/total' from /proc/pressure/cpu: share of time runnable work waited for CPU
      steal_ticks      /proc/stat steal column: time the hypervisor ran something else
    """
    out = {}
    stat = _read_kv("/sys/fs/cgroup/cpu.stat")
    if stat:
        out["cgroup_cpu_stat"] = {key: stat[key] for key in ("nr_periods", "nr_throttled", "throttled_usec", "usage_usec")
                                  if key in stat}
    try:
        with open("/sys/fs/cgroup/cpu.max") as handle:
            out["cgroup_cpu_max"] = handle.read().strip()
    except Exception:
        pass
    try:
        with open("/proc/pressure/cpu") as handle:
            for line in handle:
                if line.startswith("some"):
                    out["psi_cpu_some"] = line.strip()
    except Exception:
        pass
    try:
        with open("/proc/stat") as handle:
            first = handle.readline().split()
        if first and first[0] == "cpu" and len(first) > 8:
            out["steal_ticks"] = int(first[8])
    except Exception:
        pass
    return out


def snapshot():
    with _lock:
        events = list(_events)
        slow = list(_slow_requests)
    return {"server_time": round(time.time(), 3), "uptime_s": round(time.time() - BOOTED_AT, 1),
            "cpu_s": round(_cpu(), 2), "rss_mb": _rss_mb(), "stage": _state["stage"],
            "probe_events": events[-20:], "slow_handlers": slow[-10:], "host_cpu": host_cpu_limits()}
