"""Run the default-profile responsiveness benchmark and print a JSON report.

Deterministic — no live LLM, no kanban DB. Just drives the real gateway
decision functions over the emulated session dataset.

    venv/bin/python -m evals.responsiveness.run            # full report
    venv/bin/python -m evals.responsiveness.run --rows     # + per-turn detail
"""

from __future__ import annotations

import json
import sys

from evals.responsiveness.dataset import (
    HERMES_HOME,
    PLATFORM,
    PROFILE_NAME,
    all_turns,
)
from evals.responsiveness.score import score_dataset


def run_benchmark() -> dict:
    return score_dataset(
        all_turns(),
        platform=PLATFORM,
        profile_name=PROFILE_NAME,
        hermes_home=HERMES_HOME,
    )


def _format_report(report: dict, *, with_rows: bool) -> str:
    m = report["metrics"]
    lines = [
        "=== default-profile responsiveness benchmark ===",
        f"platform={PLATFORM} profile={PROFILE_NAME}",
        f"PASS={report['passed']}  "
        f"(green={m['green_turns']}, gap/TDD={m['gap_turns']}, total={m['turns_total']})",
        "",
        "ack policy (GREEN slice):",
        f"  accuracy        {m['ack_accuracy']:.3f}  (threshold >= {report['thresholds']['ack_accuracy']})",
        f"  recall          {m['ack_recall']:.3f}  (long-work turns acked)",
        f"  false-ack rate  {m['false_ack_rate']:.3f}  (threshold <= {report['thresholds']['false_ack_rate']})",
        "",
        "time to first feedback (GREEN long-work turns):",
        f"  mean            {m['long_work_mean_ttff_s']:.2f}s   with pre-LLM ack",
        f"  p95             {m['long_work_p95_ttff_s']:.2f}s   (threshold <= {report['thresholds']['long_work_p95_ttff_s']:.2f}s)",
        f"  mean (no ack)   {m['long_work_mean_ttff_no_ack_s']:.2f}s   <- baseline the ack improves on",
        "",
        "public progress cadence:",
        f"  ok rate         {m['cadence_ok_rate']:.3f}  (threshold >= {report['thresholds']['cadence_ok_rate']})",
        "",
        f"forward-looking: {m['gap_turns_acked']}/{m['gap_turns']} known-gap turns acked today",
    ]
    if with_rows:
        lines.append("")
        lines.append("per-turn:")
        for r in report["rows"]:
            tag = "GAP " if r["known_gap"] else "    "
            ack = "ACK " if r["ack"] else "----"
            cad = ""
            if "cadence" in r:
                c = r["cadence"]
                cad = f"  cadence={'ok' if c['ok'] else 'BAD'} notices={c['num_notices']} first={c['first_notice_at']}"
            lines.append(
                f"  {tag}{ack} ttff={r['ttff_s']:>6.2f}s "
                f"[{r['kind']}/{r['source']}] {r['id']} "
                f"({r['ack_info']['heuristic_reason']}){cad}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    with_rows = "--rows" in sys.argv[1:]
    as_json = "--json" in sys.argv[1:]
    report = run_benchmark()
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_report(report, with_rows=with_rows))
    sys.exit(0 if report["passed"] else 1)
