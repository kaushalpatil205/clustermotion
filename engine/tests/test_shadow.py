from clustermotion.shadow import (SHADOW_UA, Response, compare, flatten, parse_alb_log_line,
                                  select_requests)

LINE = ('http 2026-09-23T10:00:00.123456Z app/clustermotion/50dc6c495c0c9188 10.0.1.10:45678 '
        '10.0.11.23:8000 0.000 0.012 0.000 200 200 120 945 '
        '"GET http://clustermotion-1.us-east-1.elb.amazonaws.com:80/api/catalog/products?page=1 HTTP/1.1" '
        '"shop-loadgen/1.0" - - arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/cm-catalog-blue/73e2 '
        '"Root=1-58337262-36d228ad5d99923122bbe354" "-" "-" 100 2026-09-23T10:00:00.111000Z '
        '"forward" "-" "-" "10.0.11.23:8000" "200" "-" "-" TID_1')


def test_parse_alb_line():
    entry = parse_alb_log_line(LINE)
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/catalog/products?page=1"
    assert entry["user_agent"] == "shop-loadgen/1.0"
    assert entry["elb_status"] == "200"


def test_garbage_line_is_ignored():
    assert parse_alb_log_line("not a log line") is None


def test_select_drops_own_replays_and_health():
    entries = [
        {"method": "GET", "path": "/api/catalog/products", "user_agent": "x", "elb_status": "200"},
        {"method": "GET", "path": "/api/catalog/products", "user_agent": SHADOW_UA, "elb_status": "200"},
        {"method": "GET", "path": "/api/catalog/healthz", "user_agent": "x", "elb_status": "200"},
        {"method": "POST", "path": "/api/orders", "user_agent": "x", "elb_status": "201"},
        {"method": "GET", "path": "/api/orders/abc", "user_agent": "x", "elb_status": "503"},
    ]
    assert select_requests(entries, ["/api/"], 10) == ["/api/catalog/products"]


def test_noise_fields_are_ignored():
    p = Response(200, {"generated_at": "t1", "items": [{"price_cents": 100}]})
    s = Response(200, {"generated_at": "t2", "items": [{"price_cents": 100}]})
    c = Response(200, {"generated_at": "t3", "items": [{"price_cents": 100}]})
    assert compare(p, s, c).outcome == "match"


def test_real_regression_is_detected():
    p = Response(200, {"generated_at": "t1", "items": [{"price_cents": 100}]})
    s = Response(200, {"generated_at": "t2", "items": [{"price_cents": 100}]})
    c = Response(200, {"generated_at": "t3", "items": [{"price_cents": 101}]})
    verdict = compare(p, s, c)
    assert verdict.outcome == "mismatch" and verdict.fields == ["items[0].price_cents"]


def test_status_mismatch_and_noisy_status():
    assert compare(Response(200, {}), Response(200, {}), Response(500, {})).outcome == "mismatch"
    assert compare(Response(200, {}), Response(503, {}), Response(200, {})).outcome == "noisy"


def test_flatten_lists():
    assert flatten({"a": [1, {"b": 2}]}) == {"a.#len": 2, "a[0]": 1, "a[1].b": 2}
