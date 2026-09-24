import datetime as dt

from clustermotion.verify import load_confirmed, max_gap, overlap_seconds


def test_max_gap():
    assert max_gap([1000, 1100, 1200, 9200, 9300]) == (8.0, 1200)
    assert max_gap([5]) == (0.0, None)


def test_load_confirmed_skips_noise(tmp_path):
    f = tmp_path / "confirmed.jsonl"
    f.write_text('{"event":"confirmed","t":1,"key":"k1","order_id":"a"}\n'
                 'some k6 banner line\n'
                 '{"event":"retry","t":2}\n')
    assert [r["key"] for r in load_confirmed(str(f))] == ["k1"]


def test_overlap():
    t = lambda s: dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=s)
    assert overlap_seconds([("blue", t(0), t(100)), ("green", t(101), t(200))]) == 0.0
    assert overlap_seconds([("blue", t(0), t(100)), ("green", t(90), t(200))]) == 10.0
