import pytest

from clustermotion.lease import decide

NOW = 1_000_000.0


@pytest.mark.parametrize("state,me,drained,expected", [
    (None, "blue", False, "off"),
    ({"holder": "blue", "desired": "blue", "renewed_at": NOW}, "blue", False, "on"),
    ({"holder": "blue", "desired": "blue", "renewed_at": NOW}, "green", False, "off"),
    ({"holder": "blue", "desired": "green", "renewed_at": NOW}, "blue", False, "drain"),
    ({"holder": "blue", "desired": "green", "renewed_at": NOW}, "blue", True, "release"),
    ({"holder": "blue", "desired": "green", "renewed_at": NOW}, "green", False, "off"),
    ({"holder": "", "desired": "green", "renewed_at": NOW}, "green", False, "acquire"),
    ({"holder": "blue", "desired": "green", "renewed_at": NOW - 120}, "green", False, "acquire"),
    ({"holder": "", "desired": "blue", "renewed_at": NOW}, "green", False, "off"),
])
def test_decide(state, me, drained, expected):
    assert decide(state, me, NOW, ttl=30, drained=drained) == expected
