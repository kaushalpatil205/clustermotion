from pathlib import Path

from clustermotion import planner

FIXTURE = Path(__file__).parent / "fixtures" / "shop-rendered.yaml"


def plan():
    return {i.name: i for i in planner.classify(planner.objects_from_manifests(str(FIXTURE)))}


def test_strategies():
    items = plan()
    assert items["catalog"].strategy == "traffic-shift"
    assert items["fulfillment-worker"].strategy == "lease-handoff"
    assert items["order-sweeper"].strategy == "lease-handoff"
    assert items["orders-db-blue"].strategy == "db-switchover"
    assert items["lease-agent"].strategy == "skip"


def test_pvc_workload_needs_approval_with_estimate():
    legacy = plan()["legacy-redis"]
    assert legacy.strategy == "snapshot-restore"
    assert legacy.needs_approval
    assert legacy.est_downtime_s == 60 + 20 * 8


def test_unlabelled_cronjob_flagged():
    assert plan()["unlabelled-report"].needs_approval


def test_order_is_safe():
    strategies = [i.strategy for i in planner.classify(planner.objects_from_manifests(str(FIXTURE)))]
    assert strategies.index("traffic-shift") < strategies.index("db-switchover") < strategies.index("lease-handoff")


def test_render_summary():
    text = planner.render(planner.classify(planner.objects_from_manifests(str(FIXTURE))))
    assert "Plan: 7 workloads" in text and "2 need approval" in text


def test_quantities():
    assert planner._gib("512Mi") == 0.5
    assert planner._gib("2Ti") == 2048
    assert planner._gib(None) == 10.0
