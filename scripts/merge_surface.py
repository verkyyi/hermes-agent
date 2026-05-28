#!/usr/bin/env python3
"""Report this fork's merge surface against the upstream mirror.

Merge *conflict* risk is not about how many lines a fork adds — new files and
pure additions (``+N -0``) almost never conflict on an upstream merge. What
conflicts is **edits to lines upstream also owns**, i.e. the deletions/
modifications column of ``git diff --numstat``. This script ranks tracked source
files by that column so you can see where the real merge pain lives, watch it
across syncs, and (with ``--check``) keep it under a budget in CI.

Defaults compare the running branch against the local upstream mirror:

    baseline = first of [--baseline], ``main``, ``upstream/main``
    target   = first of [--target],   ``verky/deploy``, ``HEAD``

Usage:
    python scripts/merge_surface.py                 # ranked report
    python scripts/merge_surface.py --json           # machine-readable
    python scripts/merge_surface.py --check 700      # exit 1 if any source
                                                     # file's modified-line
                                                     # count exceeds 700

See docs/LOCAL_PATCHES.md — keeping this number from creeping is the point of the
"move patches to extension points" work (e.g. patches #6, #14).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Paths whose divergence is low-merge-risk by fork policy: local-only tests live
# under tests/local/, and docs/evals/plugins are additive new trees. They're
# reported separately so they don't drown out the hot source files.
_LOW_RISK_PREFIXES = ("tests/", "docs/", "evals/", "plugins/")


def _run(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _first_existing_ref(candidates: list[str]) -> str | None:
    for ref in candidates:
        if not ref:
            continue
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return ref
    return None


def _numstat(baseline: str, target: str) -> list[tuple[int, int, str]]:
    """Return [(added, deleted, path), ...]; skips binary (``-`` counts)."""
    out = _run("diff", "--numstat", f"{baseline}", f"{target}")
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        if added_s == "-" or deleted_s == "-":
            continue  # binary file
        rows.append((int(added_s), int(deleted_s), path))
    return rows


def _is_low_risk(path: str) -> bool:
    return path.startswith(_LOW_RISK_PREFIXES)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default="", help="upstream-mirror ref (default: main / upstream/main)")
    ap.add_argument("--target", default="", help="fork ref (default: verky/deploy / HEAD)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--top", type=int, default=20, help="rows to show (default 20)")
    ap.add_argument("--check", type=int, metavar="N", default=None,
                    help="exit 1 if any source file's modified-line count exceeds N")
    args = ap.parse_args(argv)

    baseline = _first_existing_ref([args.baseline, "main", "upstream/main"])
    target = _first_existing_ref([args.target, "verky/deploy", "HEAD"])
    if baseline is None or target is None:
        print("merge_surface: could not resolve baseline/target refs", file=sys.stderr)
        return 2

    rows = _numstat(baseline, target)
    # Conflict surface = deletions/modifications on non-low-risk source files.
    source = [(a, d, p) for (a, d, p) in rows if not _is_low_risk(p)]
    modified = sorted([r for r in source if r[1] > 0], key=lambda r: r[1], reverse=True)
    additive = [r for r in source if r[1] == 0]
    low_risk = [(a, d, p) for (a, d, p) in rows if _is_low_risk(p)]

    total_modified = sum(d for _, d, _ in source)
    worst = modified[0] if modified else None

    if args.json:
        print(json.dumps({
            "baseline": baseline, "target": target,
            "total_modified_lines": total_modified,
            "modified_source_files": len(modified),
            "additive_source_files": len(additive),
            "worst_file": ({"path": worst[2], "modified": worst[1], "added": worst[0]}
                           if worst else None),
            "modified": [{"path": p, "added": a, "modified": d} for a, d, p in modified],
        }, indent=2))
    else:
        print(f"merge surface: {target}  vs  {baseline}")
        print(f"  conflict surface (modified upstream lines, source only): {total_modified}")
        print(f"  modified source files: {len(modified)}   additive source files: {len(additive)}")
        _skipped = ", ".join(p.rstrip("/") for p in _LOW_RISK_PREFIXES)
        print(f"  low-risk paths skipped ({_skipped}): {len(low_risk)} files")
        print()
        print("  CONFLICT RISK — source files with in-place upstream edits (rank by modified):")
        for a, d, p in modified[: args.top]:
            print(f"    ~{d:<6} +{a:<6} {p}")
        if not modified:
            print("    (none — fork is purely additive against upstream)")

    if args.check is not None and worst is not None and worst[1] > args.check:
        print(f"\nmerge_surface: FAIL — {worst[2]} has {worst[1]} modified lines "
              f"(budget {args.check})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
