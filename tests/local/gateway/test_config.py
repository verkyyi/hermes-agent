"""Local tests extracted from tests/gateway/test_config.py.

Kept in the tests/local/ tree so upstream merges don't conflict on local
test additions. Upstream helpers/fixtures are imported from the original
module rather than duplicated.
"""
from __future__ import annotations

import os
from unittest.mock import patch
from gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
    StreamingConfig,
    _apply_env_overrides,
    load_gateway_config,
)

class TestSystemMessageLocale:
    def test_defaults_to_english(self):
        config = GatewayConfig()
        assert config.get_system_message_locale() == "en"

    def test_platform_extra_overrides_global_locale(self):
        config = GatewayConfig(
            system_message_locale="en",
            platforms={
                Platform.WEIXIN: PlatformConfig(
                    enabled=True,
                    extra={"system_message_locale": "zh-CN"},
                )
            },
        )
        assert config.get_system_message_locale(Platform.WEIXIN) == "zh-CN"
        assert config.get_system_message_locale(Platform.TELEGRAM) == "en"
