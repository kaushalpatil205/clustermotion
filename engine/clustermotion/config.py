"""Engine configuration. Produced by `terraform output -json engine_config`."""
from __future__ import annotations

import json
import os
from pathlib import Path

COLORS = ("blue", "green")


def other(color: str) -> str:
    if color not in COLORS:
        raise ValueError(f"unknown colour {color!r}; expected one of {COLORS}")
    return "green" if color == "blue" else "blue"


def load(path: str | None = None) -> dict:
    path = path or os.getenv("CM_CONFIG", "/etc/clustermotion/config.json")
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise SystemExit(f"config not found at {path}; run `make engine-config` or set CM_CONFIG")
