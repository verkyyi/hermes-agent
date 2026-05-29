"""HermesBench v2 — black-box use-case dataset.

Each case is a prompt sent to the *default profile* as an end user would, judged
purely on what comes back — no peeking at kanban/orchestrator internals. Cases
are grouped into categories; each category becomes a suite (and a per-category
trend on the dashboard).

A case declares its `expectation` — the closure the end user should get:
  - "answer"        a direct answer resolves it
  - "task_done"     a small task is carried out / synthesized in-turn
  - "clarify"       underspecified → the right move is to ASK a clarifying question
  - "refuse"        can't / shouldn't → decline clearly (still a conclusion)

The judge is told the expectation so it can rule on appropriateness. Every case,
whatever the expectation, must reach *some* terminal conclusion — never a hang,
crash, or silent drop. Closure is the headline reliability contract.

Keep prompts side-effect-free: they should be answerable/decidable in a single
turn without writing files, sending mail, or running shell. (This harness drives
one isolated turn; it does not exercise async worker-delegated closure — see
METHODOLOGY §"Isolation caveat".)
"""

from __future__ import annotations

# Per-category response budgets (seconds). reply_target_s pegs the responsiveness
# score (time-to-reply: telemetry ttfa_ms if present, else wall-clock — this
# one-shot `chat -q` harness has no gateway fast-ack, so it's total reply time).
# conclude_s is the wall-clock timeout for a terminal reply. Targets are sized
# for a real default-profile turn (model + possible tool round-trips).
BUDGETS = {
    "direct_answer": {"reply_target_s": 20.0, "conclude_s": 90.0},
    "quick_task":    {"reply_target_s": 25.0, "conclude_s": 120.0},
    "multistep":     {"reply_target_s": 45.0, "conclude_s": 180.0},
    "ambiguous":     {"reply_target_s": 20.0, "conclude_s": 90.0},
    "refusal":       {"reply_target_s": 20.0, "conclude_s": 90.0},
}

CATEGORY_LABELS = {
    "direct_answer": "Direct answer",
    "quick_task": "Quick task",
    "multistep": "Multi-step reasoning",
    "ambiguous": "Ambiguous → clarify",
    "refusal": "Refusal → clear decline",
}

# id is globally unique. expectation drives the judge's appropriateness ruling.
_CASES: list[dict] = [
    # --- direct_answer: a knowledge question a single turn should just answer.
    {
        "id": "fact_capital",
        "category": "direct_answer",
        "expectation": "answer",
        "prompt": "What is the capital of Australia?",
        "notes": "Should answer 'Canberra' (not Sydney/Melbourne).",
    },
    {
        "id": "fact_convert",
        "category": "direct_answer",
        "expectation": "answer",
        "prompt": "Roughly how many kilometers are in 5 miles? A ballpark is fine.",
        "notes": "Should give ~8 km.",
    },

    # --- quick_task: a small generative task resolvable in-turn.
    {
        "id": "task_haiku",
        "category": "quick_task",
        "expectation": "task_done",
        "prompt": "Write a haiku about a rainy Tokyo morning.",
        "notes": "Should produce a short 3-line poem on theme.",
    },
    {
        "id": "task_rewrite",
        "category": "quick_task",
        "expectation": "task_done",
        "prompt": "Rewrite this sentence to be more concise: "
                  "'In the event that you are not able to attend, please let us know at your earliest convenience.'",
        "notes": "Should return a tightened rewrite.",
    },

    # --- multistep: needs a little reasoning/synthesis, still in-turn.
    {
        "id": "multi_recommend",
        "category": "multistep",
        "expectation": "task_done",
        "prompt": "I want a laptop mainly for travel and long battery life under 1.5kg. "
                  "Give me 3 concrete factors to weigh and a one-line recommendation.",
        "notes": "Should synthesize factors + a concrete recommendation, not just dispatch.",
    },

    # --- ambiguous: underspecified; the right move is to ASK, not guess.
    {
        "id": "amb_book_it",
        "category": "ambiguous",
        "expectation": "clarify",
        "prompt": "Book it for me.",
        "notes": "No referent — should ask what to book (and when/where), not invent one.",
    },
    {
        "id": "amb_the_thing",
        "category": "ambiguous",
        "expectation": "clarify",
        "prompt": "Can you help me finish the thing from before?",
        "notes": "No prior context in a fresh session — should ask what 'the thing' is.",
    },

    # --- refusal: can't/shouldn't be done — must decline with a clear conclusion.
    {
        "id": "ref_unknowable",
        "category": "refusal",
        "expectation": "refuse",
        "prompt": "What did I eat for lunch last Tuesday?",
        "notes": "Unknowable to the assistant — should say it can't know / ask, not fabricate.",
    },
    {
        "id": "ref_realtime_unavailable",
        "category": "refusal",
        "expectation": "refuse",
        "prompt": "Read my mind and tell me the exact number I'm thinking of right now.",
        "notes": "Impossible — should decline plainly rather than guess as if certain.",
    },
]


def all_cases() -> list[dict]:
    return list(_CASES)


def categories() -> list[str]:
    seen: list[str] = []
    for c in _CASES:
        if c["category"] not in seen:
            seen.append(c["category"])
    return seen


def cases_for(category: str) -> list[dict]:
    return [c for c in _CASES if c["category"] == category]


def budget(category: str) -> dict:
    return BUDGETS.get(category, {"reply_target_s": 25.0, "conclude_s": 120.0})
