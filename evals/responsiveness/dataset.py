"""Emulated default-profile user sessions for the responsiveness benchmark.

Each SESSION is an ordered list of turns a real user sends to the front desk
(Telegram/Weixin DM is the canonical responsive surface). A turn carries the
*ground-truth ideal*, not just what the code does today:

  text          the user message, verbatim.
  kind          "trivial" | "short" | "long_work" | "command" — what the user
                actually wants. Drives the latency model in score.py.
  source        "dm" | "group". Group turns are deliberately NOT acked (the
                gateway keeps group trigger discipline authoritative), so they
                exercise eligibility gating, not the text heuristic.
  is_command    True for slash commands the gateway answers instantly.
  expect_ack    The IDEAL: should this turn get an immediate pre-LLM ack?
                True only for long_work turns on an eligible DM source.
  known_gap     True when today's heuristic cannot yet meet `expect_ack`
                (genuine long work phrased without a trigger verb/topic). These
                are the forward-looking targets — xfail(strict=True) in the
                test suite, exactly like the orchestrator benchmark's [TDD]
                cases. GREEN turns (known_gap False) must match expect_ack now.
  run_seconds   long_work only: how long the agent actually works the turn.
                Feeds the public-progress cadence simulation.
  phases        long_work only: [[at_second, phase], ...] the agent passes
                through (phase labels per gateway.run._public_progress_phase).
  tool_safe     OK to actually EXECUTE with real tools (live TTFT --tools full)?
                False for turns with destructive side effects — writing files,
                opening PRs, sending/drafting mail, running shell, creating
                reminders. Defaults (in all_turns) to True for trivial/short/
                command turns; long_work defaults to False. The live runner
                refuses to run tool_safe=False turns under --tools full unless
                --include-unsafe is also passed.
"""

from __future__ import annotations

# Default-profile surface under test: a Telegram DM is the canonical eligible
# responsive surface (see gateway.run._pre_llm_ack_eligible_source).
PLATFORM = "telegram"
PROFILE_NAME = "default"
HERMES_HOME = "/Users/verkyyi/.hermes"


SESSIONS: list[dict] = [
    {
        "name": "morning_checkin",
        "turns": [
            {
                "id": "morning.greet",
                "text": "good morning",
                "kind": "trivial",
                "expect_ack": False,
            },
            {
                "id": "morning.calendar",
                "text": "what's on my calendar today",
                "kind": "short",
                "expect_ack": False,
            },
            {
                "id": "morning.gateway_logs",
                "text": (
                    "check the hermes gateway logs and tell me why weixin "
                    "replies were slow this morning"
                ),
                "kind": "long_work",
                "expect_ack": True,
                "run_seconds": 180,
                "phases": [[0, "files"], [60, "command"], [140, "verification"]],
            },
        ],
    },
    {
        "name": "research_then_build",
        "turns": [
            {
                "id": "research.hey",
                "text": "hey",
                "kind": "trivial",
                "expect_ack": False,
            },
            {
                "id": "research.vector_dbs",
                "text": (
                    "research the top 3 managed vector databases and their "
                    "pricing, then write the comparison into a new file "
                    "~/notes/vector-dbs.md"
                ),
                "kind": "long_work",
                "expect_ack": True,
                "run_seconds": 300,
                "phases": [[0, "research"], [120, "files"], [240, "working"]],
            },
            {
                "id": "research.thanks",
                "text": "thanks!",
                "kind": "trivial",
                "expect_ack": False,
            },
        ],
    },
    {
        "name": "quick_qa_burst",
        "turns": [
            {
                "id": "quick.time",
                "text": "what time is it",
                "kind": "short",
                "expect_ack": False,
            },
            {
                "id": "quick.math",
                "text": "2+2?",
                "kind": "short",
                "expect_ack": False,
            },
            {
                "id": "quick.remind",
                "text": "remind me to call mom at 6",
                "kind": "short",
                "expect_ack": False,
                "tool_safe": False,  # may create a real reminder/cron entry
            },
            {
                "id": "quick.ok",
                "text": "ok thanks",
                "kind": "trivial",
                "expect_ack": False,
            },
        ],
    },
    {
        "name": "perf_debug",
        "turns": [
            {
                "id": "perf.investigate",
                "text": (
                    "the gateway feels sluggish lately — can you investigate "
                    "the dispatcher tick latency and the kanban queue wait "
                    "times, then summarize what's actually slow"
                ),
                "kind": "long_work",
                "expect_ack": True,
                "run_seconds": 240,
                "phases": [[0, "research"], [90, "command"], [200, "verification"]],
            },
            {
                "id": "perf.fix_and_pr",
                "text": "great, can you also fix the slowest one and open a PR",
                "kind": "long_work",
                "expect_ack": True,
                "run_seconds": 200,
                "phases": [[0, "files"], [80, "command"], [160, "verification"]],
            },
        ],
    },
    {
        "name": "slash_commands",
        "turns": [
            {
                "id": "cmd.status",
                "text": "/status",
                "kind": "command",
                "is_command": True,
                "expect_ack": False,
            },
            {
                "id": "cmd.usage",
                "text": "/usage today",
                "kind": "command",
                "is_command": True,
                "expect_ack": False,
            },
        ],
    },
    {
        "name": "group_chat_discipline",
        "turns": [
            {
                # Long work, but in a group: the gateway intentionally does NOT
                # ack (group trigger discipline stays authoritative). Ideal here
                # is therefore *no* ack — this guards the eligibility gate, not
                # the text heuristic.
                "id": "group.long_work",
                "text": (
                    "check the hermes gateway logs and figure out why the cron "
                    "jobs keep failing overnight"
                ),
                "kind": "long_work",
                "source": "group",
                "expect_ack": False,
                "run_seconds": 160,
                "phases": [[0, "files"], [80, "research"]],
            },
        ],
    },
    {
        "name": "casual_long_work",  # forward-looking gaps (known_gap / [TDD])
        "turns": [
            {
                # Genuine multi-step planning, phrased with no trigger verb or
                # topic. The user still waits a long time, so the ideal is an
                # ack — today's heuristic misses it.
                "id": "casual.birthday_plan",
                "text": (
                    "can you put together a birthday plan for my mom — venue "
                    "ideas, a gift shortlist, and a rough budget"
                ),
                "kind": "long_work",
                "expect_ack": True,
                "known_gap": True,
                "tool_safe": True,  # planning/research — no destructive writes
                "run_seconds": 150,
                "phases": [[0, "research"], [90, "working"]],
            },
            {
                "id": "casual.recruiter_reply",
                "text": (
                    "draft a thoughtful reply to the recruiter email and a "
                    "short counter-offer, then wait for me to approve"
                ),
                "kind": "long_work",
                "expect_ack": True,
                "known_gap": True,
                "run_seconds": 130,
                "phases": [[0, "working"]],
            },
        ],
    },
]


def all_turns() -> list[dict]:
    """Flatten sessions into turns, tagging each with its session name."""
    out: list[dict] = []
    for session in SESSIONS:
        for turn in session["turns"]:
            row = dict(turn)
            row.setdefault("source", "dm")
            row.setdefault("is_command", False)
            row.setdefault("known_gap", False)
            # Daily turns are safe to execute with real tools; long_work turns
            # may have side effects, so they default to unsafe unless flagged.
            row.setdefault("tool_safe", turn["kind"] in {"trivial", "short", "command"})
            row["session"] = session["name"]
            out.append(row)
    return out
