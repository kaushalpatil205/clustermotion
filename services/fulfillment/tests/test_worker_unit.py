from worker.main import parse_order_id


def test_parse_valid_message():
    assert parse_order_id('{"order_id": "abc"}') == "abc"


def test_parse_malformed_messages():
    assert parse_order_id("not json") is None
    assert parse_order_id('{"other": 1}') is None
    assert parse_order_id("[1, 2]") is None
