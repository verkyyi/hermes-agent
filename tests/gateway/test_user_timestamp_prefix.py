"""Tests for ``gateway.run._format_user_timestamp_prefix``.

The gateway prepends a wall-clock prefix (``[YYYY-MM-DD HH:MM TZ]``) to
historical user messages at replay time so the LLM can perceive send-time
and inter-message gaps.  Storage is untouched — the prefix is dynamic at
replay, which prevents double-prefixing on subsequent turns.
"""
from __future__ import annotations

from datetime import datetime

from gateway.run import _format_user_timestamp_prefix


class TestFormatUserTimestampPrefix:
    def test_naive_iso_string_renders_as_pt(self):
        # Naive ISO is interpreted as gateway local time and converted to PT.
        # We don't know the test host's tz, but the format must match.
        out = _format_user_timestamp_prefix("2026-05-26T09:15:00")
        assert out.startswith("[")
        assert out.endswith("]")
        assert "2026-05-26" in out or "2026-05-2" in out
        # Two-digit time block
        assert ":" in out

    def test_empty_returns_empty(self):
        assert _format_user_timestamp_prefix(None) == ""
        assert _format_user_timestamp_prefix("") == ""

    def test_garbage_returns_empty(self):
        # Never raise — caller concatenates.
        assert _format_user_timestamp_prefix("not-a-date") == ""
        assert _format_user_timestamp_prefix("garbage 2026") == ""

    def test_epoch_float_accepted(self):
        out = _format_user_timestamp_prefix(1779818014.0)
        assert out.startswith("[") and out.endswith("]")
        # Year embedded
        assert "202" in out

    def test_tz_aware_iso_converts_to_pt(self):
        # Provide a UTC instant — should convert to PT (PST or PDT).
        out = _format_user_timestamp_prefix("2026-05-26T16:15:00+00:00")
        assert "2026-05-26" in out
        # PT abbrev present (PDT in May)
        assert "PDT" in out or "PST" in out or "PT" in out
