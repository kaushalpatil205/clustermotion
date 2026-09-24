#!/usr/bin/env python3
"""Keep the documentation and the source tree identical.

Every code block in docs/*.md that is introduced by a line of the form

    **File:** `path/to/file.ext`

is the single source of truth for that file. This script writes those blocks
to disk (default) or verifies that the files on disk still match the docs
(--check, used in CI so the docs never drift from the code).

Usage:
    python3 scripts/docs_to_code.py            # write / refresh files
    python3 scripts/docs_to_code.py --check    # exit 1 if anything differs
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BLOCK = re.compile(
    r"^\*\*File:\*\* `(?P<path>[^`]+)`[^\n]*\n+"
    r"(?P<fence>`{3,})[^\n]*\n"
    r"(?P<body>.*?)\n?"
    r"^(?P=fence)[ \t]*$",
    re.S | re.M,
)


def collect() -> dict[str, str]:
    files: dict[str, str] = {}
    for doc in sorted((ROOT / "docs").glob("*.md")):
        for match in BLOCK.finditer(doc.read_text()):
            path, body = match["path"], match["body"] + "\n"
            if path in files and files[path] != body:
                sys.exit(f"{doc.name}: {path} is defined twice with different content")
            files[path] = body
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only verify, do not write")
    args = parser.parse_args()

    files = collect()
    drift = []
    for rel, body in files.items():
        target = ROOT / rel
        current = target.read_text() if target.exists() else None
        if current == body:
            continue
        drift.append(rel)
        if not args.check:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            if rel.endswith(".sh"):
                target.chmod(0o755)

    verb = "differ from docs" if args.check else "written"
    print(f"{len(files)} files in docs, {len(drift)} {verb}")
    for rel in drift:
        print(f"  {rel}")
    return 1 if (args.check and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
