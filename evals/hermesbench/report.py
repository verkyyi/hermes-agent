"""Render a HermesBench run as a human report, with deltas vs the prior run."""

from __future__ import annotations


def _fmt_delta(cur, prev) -> str:
    if cur is None or prev is None:
        return ""
    d = cur - prev
    if abs(d) < 0.005:
        return "  (=)"
    return f"  ({'+' if d >= 0 else ''}{d:.1f})"


def _suite_status(s: dict) -> str:
    if s.get("skipped"):
        return "SKIP"
    if s.get("error"):
        return "ERR "
    if s.get("passed") is True:
        return "PASS"
    if s.get("passed") is False:
        return "FAIL"
    return "??? "


def render(report: dict, previous: dict | None = None) -> str:
    prev_suites = {s["id"]: s for s in (previous or {}).get("suites", [])}
    h = report["harness"]
    lines = [
        "=" * 64,
        "HERMESBENCH  " + report["run_id"],
        "=" * 64,
        f"ts={report['ts']}  tier={report['tier']}  suites_ran={report['suites_ran']}",
        f"harness: git={(h.get('git_sha') or '?')[:10]} "
        f"model={h.get('model_id') or '?'} "
        f"profile={(h.get('profile_hash') or '?')[:10]}",
        "",
    ]

    overall = report.get("overall_score")
    prev_overall = (previous or {}).get("overall_score")
    verdict = "PASS" if report["passed"] else "FAIL"
    overall_str = "n/a" if overall is None else f"{overall:.1f}"
    lines.append(
        f"OVERALL  {overall_str}{_fmt_delta(overall, prev_overall)}   [{verdict}]"
    )
    lines.append("")
    lines.append(f"  {'suite':<16}{'mode':<10}{'tier':<6}{'score':>7}  status")
    lines.append("  " + "-" * 56)

    for s in report["suites"]:
        prev = prev_suites.get(s["id"])
        score = s.get("score")
        score_str = "  -  " if score is None else f"{score:6.1f}"
        delta = _fmt_delta(score, (prev or {}).get("score")) if prev else ""
        lines.append(
            f"  {s['id']:<16}{s.get('mode',''):<10}{s.get('tier',''):<6}"
            f"{score_str:>7}  {_suite_status(s)}{delta}"
        )

    notes = []
    for s in report["suites"]:
        if s.get("skipped"):
            notes.append(f"  - {s['id']}: skipped ({s.get('skip_reason')})")
        elif s.get("error"):
            notes.append(f"  - {s['id']}: ERROR {s.get('error')}")
    if notes:
        lines.append("")
        lines.append("notes:")
        lines.extend(notes)

    return "\n".join(lines)
