import unittest
import ssl

from event_sync_metrics import CountingHttp, RuntimeMetrics


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, uri, method="GET", **kwargs):
        self.calls.append((uri, method, kwargs))
        return "response", b"body"


class FlakyHttp:
    def __init__(self):
        self.calls = 0
        self.http = type("ConnectionPool", (), {"connections": {"stale": object()}})()

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ssl.SSLEOFError("stale TLS connection")
        return "response", b"body"


class RuntimeMetricsTests(unittest.TestCase):
    def test_aggregates_api_worker_and_trigger_activity(self):
        clock = iter([10.0, 16.5])
        metrics = RuntimeMetrics(clock=lambda: next(clock), memory_probe=lambda: 42.5)

        metrics.record_api("gmail")
        metrics.record_api("gmail")
        metrics.record_api("sheets")
        metrics.record_worker("sync_active_label.py")
        metrics.record_trigger("notification", duration=3.25, success=True, changed_threads=2)
        snapshot = metrics.snapshot()

        self.assertEqual({"gmail": 2, "sheets": 1}, snapshot["api_requests"])
        self.assertEqual({"sync_active_label.py": 1}, snapshot["worker_launches"])
        self.assertEqual(1, snapshot["triggers"]["notification"]["runs"])
        self.assertEqual(2, snapshot["triggers"]["notification"]["changed_threads"])
        self.assertEqual(3.25, snapshot["processing_seconds"])
        self.assertEqual(6.5, snapshot["uptime_seconds"])
        self.assertEqual(42.5, snapshot["rss_mb"])

    def test_counting_http_records_each_real_request_before_delegating(self):
        metrics = RuntimeMetrics(clock=lambda: 1.0, memory_probe=lambda: 0.0)
        delegate = FakeHttp()
        http = CountingHttp(delegate, metrics, "gmail")

        result = http.request("https://gmail.googleapis.com/test", method="POST", body=b"x")

        self.assertEqual(("response", b"body"), result)
        self.assertEqual({"gmail": 1}, metrics.snapshot()["api_requests"])
        self.assertEqual(1, len(delegate.calls))

    def test_transient_ssl_eof_discards_stale_connection_and_retries(self):
        metrics = RuntimeMetrics(clock=lambda: 1.0, memory_probe=lambda: 0.0)
        delegate = FlakyHttp()
        sleeps = []
        http = CountingHttp(delegate, metrics, "sheets", sleeper=sleeps.append)

        result = http.request("https://sheets.googleapis.com/test")

        self.assertEqual(("response", b"body"), result)
        self.assertEqual(2, delegate.calls)
        self.assertEqual({}, delegate.http.connections)
        self.assertEqual([1], sleeps)
        self.assertEqual({"sheets": 2}, metrics.snapshot()["api_requests"])


if __name__ == "__main__":
    unittest.main()
