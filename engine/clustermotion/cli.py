"""`cm`: the ClusterMotion command-line interface."""
from __future__ import annotations

import argparse
import json
import sys

from . import config as config_mod
from .config import other


def _ctx(args):
    from .events import Recorder
    from .kube import KubeClients
    cfg = config_mod.load(args.config)
    return cfg, KubeClients(cfg), Recorder(cfg, args.run_id)


def cmd_plan(args):
    from . import planner
    if args.manifests:
        objects = planner.objects_from_manifests(args.manifests)
    else:
        cfg, clients, _ = _ctx(args)
        objects = planner.objects_from_cluster(clients, args.from_cluster, args.namespace)
    items = planner.classify(objects)
    print(planner.render(items, as_json=args.json))
    return 0


def cmd_register(args):
    from .checks import register
    cfg, clients, _ = _ctx(args)
    register(cfg, clients, args.color, args.db_primary)
    return 0


def cmd_wait_synced(args):
    from .checks import wait_synced
    cfg, clients, rec = _ctx(args)
    wait_synced(cfg, clients, args.color, args.timeout)
    rec.emit("wait-synced", "completed", color=args.color)
    return 0


def cmd_preflight(args):
    from .checks import preflight, print_checks
    cfg, clients, rec = _ctx(args)
    ok = print_checks(f"Preflight {args.frm} -> {args.to}", preflight(cfg, clients, args.frm, args.to))
    rec.emit("preflight", "passed" if ok else "failed")
    return 0 if ok else 1


def cmd_smoke(args):
    from .checks import print_checks, smoke
    cfg, _, rec = _ctx(args)
    ok = print_checks(f"Smoke tests against {args.color} (header routing)", smoke(cfg, args.color))
    rec.emit("smoke", "passed" if ok else "failed", color=args.color)
    return 0 if ok else 1


def cmd_shadow(args):
    from . import shadow
    cfg, _, rec = _ctx(args)
    paths = None
    if args.paths_file:
        paths = [p.strip() for p in open(args.paths_file) if p.strip()]
    result = shadow.run(cfg, args.frm, args.to, args.minutes, args.max_requests, paths)
    print(json.dumps(result, indent=2))
    judged = result["match"] + result["mismatch"]
    if judged < args.min_samples:
        verdict, reason = "failed", f"only {judged} comparable requests (need {args.min_samples})"
    elif result["mismatch_ratio"] > args.max_mismatch:
        verdict, reason = "failed", f"mismatch ratio {result['mismatch_ratio']:.2%} > {args.max_mismatch:.2%}"
    else:
        verdict, reason = "passed", f"mismatch ratio {result['mismatch_ratio']:.2%}"
    rec.emit("shadow", verdict, reason=reason, **{k: result[k] for k in ("requests", "match", "mismatch", "noisy")})
    print(f"shadow gate {verdict}: {reason}")
    return 0 if verdict == "passed" else 1


def cmd_traffic(args):
    from . import traffic
    cfg, _, rec = _ctx(args)
    services = [args.service] if getattr(args, "service", None) else cfg["shift_order"]
    if args.action == "status":
        for s in cfg["shift_order"]:
            print(f"{s:10s} {traffic.current_weights(cfg, s)}")
        return 0
    if args.action == "set":
        for s in services:
            traffic.set_weights(cfg, s, {"blue": args.blue, "green": args.green})
            rec.emit("traffic-set", "done", service=s, blue=args.blue, green=args.green)
        return 0
    steps = [int(x) for x in args.steps.split(",")]
    for s in services:
        if not traffic.shift(cfg, s, args.to, steps, args.hold, rec):
            return 1
    return 0


def cmd_db(args):
    from . import database
    cfg, clients, rec = _ctx(args)
    if args.action == "status":
        print(json.dumps(database.status(cfg, clients), indent=2))
        return 0
    if args.action == "point":
        host = database.lb_hostname(cfg, clients, args.to)
        database.point_dns(cfg, host)
        print(f"{cfg['db']['record']} -> {host}")
        return 0
    pause = database.switchover(cfg, clients, args.to, rec, args.timeout)
    print(f"switchover to {args.to} complete; primary unavailable for writes for ~{pause}s")
    return 0


def cmd_lease(args):
    from . import lease
    if args.action == "agent":
        lease.run_agent()
        return 0
    cfg, _, rec = _ctx(args)
    table = lease.LeaseTable.from_cfg(cfg)
    if args.action == "status":
        print(json.dumps(table.read(), indent=2))
    elif args.action == "init":
        table.init(args.holder)
        print(f"lease initialised, holder={args.holder}")
    elif args.action == "handoff":
        took = lease.handoff(cfg, args.to, rec, args.timeout)
        print(f"lease now held by {args.to} (handoff took {took}s)")
    return 0


def cmd_verify(args):
    from . import verify
    cfg, _, rec = _ctx(args)
    result = verify.reconcile(cfg, args.confirmed, host=args.db_host)
    print(verify.render(result))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
    rec.emit("verify", "passed" if result["passed"] else "failed",
             **{k: result[k] for k in ("confirmed_writes", "lost_writes", "double_fulfilled",
                                       "missed_slots", "max_write_gap_s")})
    return 0 if result["passed"] else 1


def cmd_report(args):
    import boto3

    from .events import render_markdown
    cfg, _, rec = _ctx(args)
    text = render_markdown(rec.run_id, rec.events(), args.status)
    print(text)
    if cfg.get("artifacts_bucket"):
        key = f"runs/{rec.run_id}/report.md"
        boto3.client("s3", region_name=cfg["region"]).put_object(
            Bucket=cfg["artifacts_bucket"], Key=key, Body=text.encode(), ContentType="text/markdown")
        print(f"report uploaded to s3://{cfg['artifacts_bucket']}/{key}")
    return 0


def cmd_migrate(args):
    """Manual end-to-end run (what the Argo workflow does, step by step)."""
    import time

    frm, to = args.frm, other(args.frm)
    run_id = args.run_id or time.strftime("manual-%Y%m%d-%H%M%S")
    base = (["--config", args.config] if args.config else []) + ["--run-id", run_id]
    steps = [
        ["preflight", "--from", frm, "--to", to],
        ["plan", "--from-cluster", frm],
        ["smoke", "--color", to],
        ["shadow", "--from", frm, "--to", to],
        ["traffic", "shift", "--to", to, "--steps", args.steps, "--hold", str(args.hold)],
        ["db", "switchover", "--to", to],
        ["lease", "handoff", "--to", to],
        ["smoke", "--color", to],
    ]
    if args.skip_shadow:
        steps = [s for s in steps if s[0] != "shadow"]
    status = "Succeeded"
    for step in steps:
        if step[0] == "db" and not args.yes:
            if input(f"\nTraffic is on {to}. Switch the database primary to {to}? [y/N] ").lower() != "y":
                print("stopped before the database switchover; move traffic back with "
                      f"`cm traffic set --{frm} 100 --{to} 0`")
                status = "Stopped"
                break
        print(f"\n=== cm {' '.join(step)}")
        if main(base + step):
            print(f"step failed: cm {' '.join(step)}")
            status = "Failed"
            break
    main(base + ["report", "--status", status])
    return 0 if status == "Succeeded" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cm", description="ClusterMotion migration engine")
    p.add_argument("--config", help="path to config.json (default $CM_CONFIG)")
    p.add_argument("--run-id", help="run identifier (default $CM_RUN_ID or a timestamp)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("plan", help="classify workloads into migration strategies")
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-cluster", choices=config_mod.COLORS)
    src.add_argument("--manifests", help="YAML file or directory (e.g. helm template output)")
    s.add_argument("--namespace", action="append", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("register", help="add a cluster to Argo CD and seed secrets")
    s.add_argument("--color", required=True, choices=config_mod.COLORS)
    s.add_argument("--db-primary", required=True, choices=config_mod.COLORS)
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("wait-synced", help="wait for Argo CD apps of a cluster")
    s.add_argument("--color", required=True, choices=config_mod.COLORS)
    s.add_argument("--timeout", type=int, default=1800)
    s.set_defaults(func=cmd_wait_synced)

    s = sub.add_parser("preflight", help="verify the starting state")
    s.add_argument("--from", dest="frm", required=True, choices=config_mod.COLORS)
    s.add_argument("--to", required=True, choices=config_mod.COLORS)
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("smoke", help="call a cluster directly through header routing")
    s.add_argument("--color", required=True, choices=config_mod.COLORS)
    s.set_defaults(func=cmd_smoke)

    s = sub.add_parser("shadow", help="replay real GET traffic and diff responses")
    s.add_argument("--from", dest="frm", required=True, choices=config_mod.COLORS)
    s.add_argument("--to", required=True, choices=config_mod.COLORS)
    s.add_argument("--minutes", type=int, default=15)
    s.add_argument("--max-requests", type=int, default=300)
    s.add_argument("--min-samples", type=int, default=50)
    s.add_argument("--max-mismatch", type=float, default=0.01)
    s.add_argument("--paths-file", help="replay these paths instead of reading ALB logs")
    s.set_defaults(func=cmd_shadow)

    s = sub.add_parser("traffic", help="ALB weights: status | set | shift")
    tsub = s.add_subparsers(dest="action", required=True)
    tsub.add_parser("status")
    t = tsub.add_parser("set")
    t.add_argument("--service")
    t.add_argument("--blue", type=int, required=True)
    t.add_argument("--green", type=int, required=True)
    t = tsub.add_parser("shift")
    t.add_argument("--service", help="default: every service in shift_order")
    t.add_argument("--to", required=True, choices=config_mod.COLORS)
    t.add_argument("--steps", default="5,25,50,100")
    t.add_argument("--hold", type=int, default=120, help="seconds to observe each step")
    s.set_defaults(func=cmd_traffic)

    s = sub.add_parser("db", help="database: status | point | switchover")
    dsub = s.add_subparsers(dest="action", required=True)
    dsub.add_parser("status")
    d = dsub.add_parser("point", help="point db DNS at a cluster (bootstrap only)")
    d.add_argument("--to", required=True, choices=config_mod.COLORS)
    d = dsub.add_parser("switchover", help="zero-data-loss primary move")
    d.add_argument("--to", required=True, choices=config_mod.COLORS)
    d.add_argument("--timeout", type=int, default=900)
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("lease", help="singleton lease: status | init | handoff | agent")
    lsub = s.add_subparsers(dest="action", required=True)
    lsub.add_parser("status")
    lsub.add_parser("agent", help="run the in-cluster lease agent")
    l_ = lsub.add_parser("init")
    l_.add_argument("--holder", required=True, choices=config_mod.COLORS)
    l_ = lsub.add_parser("handoff")
    l_.add_argument("--to", required=True, choices=config_mod.COLORS)
    l_.add_argument("--timeout", type=int, default=900)
    s.set_defaults(func=cmd_lease)

    s = sub.add_parser("verify", help="reconcile client-confirmed writes with the database")
    s.add_argument("--confirmed", required=True, help="k6 confirmed-writes JSONL file")
    s.add_argument("--db-host", help="override database host (default: the db DNS record)")
    s.add_argument("--json-out")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("report", help="render the run timeline")
    s.add_argument("--status", default="")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("migrate", help="run all steps in order (manual mode)")
    s.add_argument("--from", dest="frm", required=True, choices=config_mod.COLORS)
    s.add_argument("--steps", default="5,25,50,100")
    s.add_argument("--hold", type=int, default=120)
    s.add_argument("--yes", action="store_true", help="do not ask before the database switchover")
    s.add_argument("--skip-shadow", action="store_true", help="skip shadow replay (no ALB logs yet)")
    s.set_defaults(func=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "plan" and not args.namespace:
        args.namespace = ["shop"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
