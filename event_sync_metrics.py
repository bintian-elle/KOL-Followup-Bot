"""Lightweight runtime counters for event-sync acceptance and health logs."""

import resource
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path


def current_rss_mb():
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(float(line.split()[1]) / 1024, 2)
    amount = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if amount > 10_000_000 else 1024
    return round(amount / divisor, 2)


class RuntimeMetrics:
    def __init__(self, clock=time.monotonic, memory_probe=current_rss_mb):
        self.clock = clock
        self.memory_probe = memory_probe
        self.started = clock()
        self.api_requests = Counter()
        self.worker_launches = Counter()
        self.triggers = defaultdict(lambda: {"runs": 0, "failures": 0, "changed_threads": 0})
        self.processing_seconds = 0.0
        self._lock = threading.Lock()

    def record_api(self, service):
        with self._lock:
            self.api_requests[service] += 1

    def record_worker(self, name):
        with self._lock:
            self.worker_launches[name] += 1

    def record_trigger(self, trigger, duration, success, changed_threads=0):
        with self._lock:
            item = self.triggers[trigger]
            item["runs"] += 1
            item["failures"] += int(not success)
            item["changed_threads"] += int(changed_threads)
            self.processing_seconds += float(duration)

    def snapshot(self):
        with self._lock:
            return {
                "uptime_seconds": round(self.clock() - self.started, 2),
                "rss_mb": self.memory_probe(),
                "api_requests": dict(self.api_requests),
                "worker_launches": dict(self.worker_launches),
                "triggers": {name: dict(values) for name, values in self.triggers.items()},
                "processing_seconds": round(self.processing_seconds, 2),
            }


class CountingHttp:
    def __init__(self, delegate, metrics, service):
        self.delegate = delegate
        self.metrics = metrics
        self.service = service

    def request(self, *args, **kwargs):
        self.metrics.record_api(self.service)
        return self.delegate.request(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.delegate, name)
