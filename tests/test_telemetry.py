import sqlite3
import time

from agent.telemetry import TelemetryStore, format_metrics_summary, summarize_metrics


def test_sqlite_writer_span_nesting_and_pruning(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db", max_turns=2)

    for idx in range(3):
        turn = store.begin_turn(session_id=f"s{idx}", platform="cli", model="m")
        turn.set_current()
        with turn.start_span("outer", idx=idx):
            time.sleep(0.001)
            with turn.start_span("inner"):
                time.sleep(0.001)
        turn.mark_ack()
        turn.mark_output("hello")
        turn.finish(api_calls=1)

    rows = store.query_turns(0)
    assert len(rows) == 2
    assert {r["session_id"] for r in rows} == {"s1", "s2"}
    assert all(r["ttfa_ms"] is not None for r in rows)
    assert all(r["ttft_ms"] is not None for r in rows)
    assert all(r["ttlt_ms"] is not None for r in rows)

    with sqlite3.connect(tmp_path / "telemetry.db") as conn:
        spans = conn.execute("SELECT turn_id, parent_id, name FROM spans").fetchall()
    assert spans
    names = [s[2] for s in spans]
    assert "turn.total" in names
    assert "outer" in names
    assert "inner" in names
    kept_turn_ids = {r["id"] for r in rows}
    assert {s[0] for s in spans}.issubset(kept_turn_ids)


def test_percentile_summary_and_format(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db", max_turns=10)
    for i, delay in enumerate([10, 20, 30, 40]):
        turn = store.begin_turn(session_id=str(i), platform="telegram", attributes={})
        now = turn.started_at_ms
        turn.first_ack_ms = now + delay
        turn.first_token_ms = now + delay + 5
        turn.output_start_ms = now + delay + 5
        turn.output_end_ms = now + delay + 20
        turn.output_chars = 100
        turn.ended_at_ms = now + delay + 50
        turn.finish()

    summary = summarize_metrics(store=store)
    group = summary["groups"][0]
    assert group["platform"] == "telegram"
    assert group["count"] == 4
    assert group["ttfa_p50_ms"] == 25
    assert group["ttfa_p95_ms"] > 35
    text = format_metrics_summary(store=store)
    assert "Hermes metrics" in text
    assert "TTFA" in text
    assert "telegram" in text


def test_telemetry_fail_open_on_write_error(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db", max_turns=10)
    turn = store.begin_turn(session_id="s", platform="cli")
    turn.mark_output("ok")

    def boom(_turn):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "write_turn", boom)
    # Must not raise: telemetry is best-effort and fail-open.
    turn.finish()


def test_record_span_event_and_stage_summary(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db", max_turns=10)

    store.record_span_event(
        "queue.wait",
        platform="kanban",
        profile="worker",
        duration_ms=123,
        attributes={"profile": "worker", "task_status": "claimed", "secret_obj": {"ok": True}},
    )
    store.record_span_event(
        "adapter.send.telegram",
        platform="gateway",
        duration_ms=45,
        status="ok",
        attributes={"platform": "telegram", "notification_mode": "synthesize"},
    )

    spans = store.query_spans(0)
    names = {row["name"] for row in spans}
    assert "queue.wait" in names
    assert "adapter.send.telegram" in names

    summary = summarize_metrics(store=store)
    stage_names = {row["name"] for row in summary["spans"]}
    assert {"queue.wait", "adapter.send.telegram"} <= stage_names
    text = format_metrics_summary(store=store)
    assert "span/stage" in text
    assert "queue.wait" in text



def test_request_mix_and_async_kanban_correlation_summary(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db", max_turns=20)
    base_ms = int(time.time() * 1000)

    direct = store.begin_turn(session_id="direct", platform="telegram", attributes={"request_id": "req_direct"}, started_at_ms=base_ms)
    direct.started_at_ms = base_ms
    direct.first_ack_ms = base_ms + 10
    direct.first_token_ms = base_ms + 20
    direct.output_start_ms = base_ms + 20
    direct.output_end_ms = base_ms + 60
    direct.output_chars = 50
    direct.ended_at_ms = base_ms + 100
    direct.finish()

    async_base_ms = base_ms + 1_000
    async_turn = store.begin_turn(
        session_id="async",
        platform="telegram",
        attributes={
            "request_id": "req_async",
            "request_class": "async_kanban",
            "kanban_task_id": "t_abc12345",
            "notification_mode": "synthesize",
        },
        started_at_ms=async_base_ms,
    )
    async_turn.first_ack_ms = async_base_ms + 50
    async_turn.first_token_ms = async_base_ms + 50
    async_turn.output_start_ms = async_base_ms + 50
    async_turn.output_end_ms = async_base_ms + 70
    async_turn.output_chars = 48
    async_turn.ended_at_ms = async_base_ms + 80
    async_turn.finish()

    attrs = {
        "request_id": "req_async",
        "task_id": "t_abc12345",
        "notification_mode": "synthesize",
    }
    store.record_span_event("kanban.task_created", platform="kanban", attributes=attrs, started_at_ms=async_base_ms + 30, ended_at_ms=async_base_ms + 30)
    store.record_span_event("kanban.dispatch_ack_sent", platform="kanban", attributes=attrs, started_at_ms=async_base_ms + 50, ended_at_ms=async_base_ms + 50)
    store.record_span_event("queue.wait", platform="kanban", attributes=attrs, duration_ms=300, ended_at_ms=async_base_ms + 400)
    store.record_span_event("worker.run", platform="kanban", attributes=attrs, duration_ms=500, ended_at_ms=async_base_ms + 1_000)
    store.record_span_event("kanban.synthesis", platform="gateway", attributes=attrs, duration_ms=120, ended_at_ms=async_base_ms + 1_150)
    store.record_span_event("kanban.final_notification_sent", platform="gateway", attributes=attrs, started_at_ms=async_base_ms + 1_200, ended_at_ms=async_base_ms + 1_200)

    summary = summarize_metrics(store=store)
    mix = summary["request_mix"]
    assert mix["total_foreground"] == 2
    assert mix["direct_no_kanban"] == 1
    assert mix["kanban_dispatched"] == 1
    assert mix["notification_modes"]["synthesize"] >= 1

    async_summary = summary["async_kanban"]
    assert async_summary["requests"] == 1
    assert async_summary["completed_notifications"] == 1
    assert async_summary["ttfa_async_p50_ms"] == 50
    assert async_summary["ttlt_async_p50_ms"] == 1200
    stage_names = {row["name"] for row in async_summary["stages"]}
    assert {"queue.wait", "worker.run", "kanban.synthesis"} <= stage_names

    text = format_metrics_summary(store=store)
    assert "request mix" in text
    assert "async kanban" in text
    assert "TTLT_async" in text
