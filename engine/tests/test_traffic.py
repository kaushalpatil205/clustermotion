from clustermotion import traffic

SLO = {"max_5xx_ratio": 0.01, "max_p95_ratio": 1.5, "min_p95_seconds": 0.3, "min_requests": 20}
SOURCE = {"requests": 1000, "errors": 0, "p95": 0.4}


def test_pass():
    assert traffic.evaluate({"requests": 100, "errors": 0, "p95": 0.45}, SOURCE, SLO)[0] == "pass"


def test_error_ratio_fails():
    verdict, reason = traffic.evaluate({"requests": 100, "errors": 5, "p95": 0.1}, SOURCE, SLO)
    assert verdict == "fail" and "5xx" in reason


def test_latency_regression_fails():
    assert traffic.evaluate({"requests": 100, "errors": 0, "p95": 0.9}, SOURCE, SLO)[0] == "fail"


def test_latency_floor_avoids_false_alarm_on_fast_services():
    fast_source = {"requests": 1000, "errors": 0, "p95": 0.01}
    assert traffic.evaluate({"requests": 100, "errors": 0, "p95": 0.05}, fast_source, SLO)[0] == "pass"


def test_insufficient_traffic_is_inconclusive():
    assert traffic.evaluate({"requests": 3, "errors": 3, "p95": 9}, SOURCE, SLO)[0] == "inconclusive"


class FakeRec:
    def __init__(self):
        self.events = []

    def emit(self, step, status, **data):
        self.events.append((step, status, data))


def test_shift_rolls_back_on_breach(monkeypatch):
    calls = []
    monkeypatch.setattr(traffic, "set_weights", lambda cfg, s, w: calls.append(dict(w)))
    stats = {"green": {"requests": 100, "errors": 50, "p95": 0.1}, "blue": SOURCE}
    monkeypatch.setattr(traffic, "tg_stats", lambda cfg, s, color, a, b: stats[color])
    rec = FakeRec()
    ok = traffic.shift({"slo": SLO}, "catalog", "green", [5, 25], hold=60, rec=rec, sleep=lambda s: None)
    assert ok is False
    assert calls == [{"green": 5, "blue": 95}, {"blue": 100, "green": 0}]
    assert rec.events[-1][1] == "rolled-back"


def test_shift_completes(monkeypatch):
    calls = []
    monkeypatch.setattr(traffic, "set_weights", lambda cfg, s, w: calls.append(dict(w)))
    monkeypatch.setattr(traffic, "tg_stats", lambda cfg, s, color, a, b: {"requests": 100, "errors": 0, "p95": 0.2})
    rec = FakeRec()
    assert traffic.shift({"slo": SLO}, "orders", "green", [50, 100], hold=30, rec=rec, sleep=lambda s: None)
    assert calls[-1] == {"green": 100, "blue": 0}
