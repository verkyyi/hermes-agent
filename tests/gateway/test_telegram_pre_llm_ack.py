import json
import sqlite3

from gateway.config import Platform
from gateway.run import (
    _is_hermes_hk_runtime,
    _pre_llm_ack_eligible_source,
    _public_progress_message,
    _public_progress_phase,
    _record_gateway_telemetry_span,
    _should_send_pre_llm_ack,
    _should_send_public_progress,
    _should_send_telegram_pre_llm_ack,
)


def test_telegram_pre_llm_ack_positive_long_ops_prompt():
    should_ack, reason = _should_send_telegram_pre_llm_ack(
        "check Hermes gateway metrics and debug why Telegram responses are slow"
    )

    assert should_ack is True
    assert reason.startswith(("verb:", "topic:"))


def test_weixin_pre_llm_ack_uses_shared_positive_long_ops_heuristic():
    should_ack, reason = _should_send_pre_llm_ack(
        "check Hermes gateway metrics and debug why Weixin responses are slow"
    )

    assert should_ack is True
    assert reason.startswith(("verb:", "topic:"))


def test_weixin_dm_is_pre_llm_ack_eligible():
    eligible, reason = _pre_llm_ack_eligible_source(Platform.WEIXIN, "dm")

    assert eligible is True
    assert reason == "eligible_dm"


def test_weixin_group_is_not_pre_llm_ack_eligible_without_explicit_addressing():
    eligible, reason = _pre_llm_ack_eligible_source(Platform.WEIXIN, "group")

    assert eligible is False
    assert reason == "not_dm"


def test_telegram_pre_llm_ack_negative_short_casual_prompt():
    should_ack, reason = _should_send_telegram_pre_llm_ack("ok")

    assert should_ack is False
    assert reason == "trivial_exact"


def test_telegram_pre_llm_ack_negative_simple_question():
    should_ack, reason = _should_send_telegram_pre_llm_ack("what time is it")

    assert should_ack is False
    assert reason == "short"


def test_telegram_pre_llm_ack_skips_slash_commands():
    should_ack, reason = _should_send_telegram_pre_llm_ack(
        "/status", is_command=True
    )

    assert should_ack is False
    assert reason == "command"


def test_public_long_progress_platform_and_cadence_gating():
    allowed, reason = _should_send_public_progress(
        platform=Platform.TELEGRAM,
        profile_name="default",
        hermes_home="/Users/verkyyi/.hermes",
        elapsed_s=89,
        now_s=1000,
        last_sent_s=None,
        last_phase=None,
        phase="verification",
    )
    assert allowed is False
    assert reason == "too_early"

    allowed, reason = _should_send_public_progress(
        platform=Platform.TELEGRAM,
        profile_name="default",
        hermes_home="/Users/verkyyi/.hermes",
        elapsed_s=90,
        now_s=1000,
        last_sent_s=None,
        last_phase=None,
        phase="verification",
    )
    assert allowed is True
    assert reason == "first_notice"

    allowed, reason = _should_send_public_progress(
        platform=Platform.TELEGRAM,
        profile_name="default",
        hermes_home="/Users/verkyyi/.hermes",
        elapsed_s=180,
        now_s=1100,
        last_sent_s=1000,
        last_phase="verification",
        phase="browser",
    )
    assert allowed is False
    assert reason == "rate_limited"

    allowed, reason = _should_send_public_progress(
        platform=Platform.TELEGRAM,
        profile_name="default",
        hermes_home="/Users/verkyyi/.hermes",
        elapsed_s=240,
        now_s=1121,
        last_sent_s=1000,
        last_phase="verification",
        phase="browser",
    )
    assert allowed is True
    assert reason == "phase_changed"

    allowed, reason = _should_send_public_progress(
        platform=Platform.DISCORD,
        profile_name="default",
        hermes_home="/Users/verkyyi/.hermes",
        elapsed_s=600,
        now_s=1600,
        last_sent_s=None,
        last_phase=None,
        phase="working",
    )
    assert allowed is False
    assert reason == "unsupported_platform"


def test_public_long_progress_excludes_hermes_hk_runtime():
    assert _is_hermes_hk_runtime(profile_name="hermes-hk") is True
    assert _is_hermes_hk_runtime(hermes_home="/Users/verkyyi/.hermes/profiles/hermes-hk") is True

    allowed, reason = _should_send_public_progress(
        platform=Platform.WEIXIN,
        profile_name="hermes-hk",
        hermes_home="/Users/verkyyi/.hermes/profiles/hermes-hk",
        elapsed_s=600,
        now_s=1600,
        last_sent_s=None,
        last_phase=None,
        phase="working",
    )
    assert allowed is False
    assert reason == "hermes_hk_excluded"


def test_public_long_progress_phase_and_message_are_sanitized_enums():
    phase = _public_progress_phase({
        "current_tool": "terminal",
        "last_activity_desc": "pytest tests/test_secret.py --token sk-test-secret task t_deadbeef pid 123",
    })
    assert phase == "verification"

    message = _public_progress_message(Platform.TELEGRAM, phase)
    assert message == "Still working — running verification now."
    assert "sk-test-secret" not in message
    assert "t_deadbeef" not in message
    assert "pid" not in message.lower()

    zh_message = _public_progress_message(Platform.WEIXIN, "verification")
    assert zh_message == "还在处理，正在验证结果。"


def test_gateway_telemetry_span_records_ack_decision(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import agent.telemetry as telemetry

    telemetry._store_singleton = None
    _record_gateway_telemetry_span(
        "gateway.pre_llm_ack.decision",
        platform="telegram",
        attributes={
            "gateway_request_id": "gw-test",
            "decision": True,
            "reason": "verb:check",
        },
        started_at_ms=1000,
        ended_at_ms=1005,
    )

    with sqlite3.connect(tmp_path / "telemetry.db") as conn:
        row = conn.execute(
            "SELECT name, attributes_json FROM spans WHERE name=?",
            ("gateway.pre_llm_ack.decision",),
        ).fetchone()

    assert row is not None
    assert row[0] == "gateway.pre_llm_ack.decision"
    attrs = json.loads(row[1])
    assert attrs["gateway_request_id"] == "gw-test"
    assert attrs["decision"] is True
    assert attrs["reason"] == "verb:check"
