"""Tests for MCP connection-test proxy handling."""

import aiohttp
import pytest

import astrbot.core
from astrbot.core.agent.mcp_client import _quick_test_mcp_connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("core_config", "expected_trust_env"),
    [
        ({}, False),
        ({"respect_env_proxy": True}, True),
        ({"http_proxy": "http://proxy.example.com:8080"}, True),
    ],
)
async def test_mcp_connection_test_proxy_behavior(
    monkeypatch: pytest.MonkeyPatch,
    core_config: dict,
    expected_trust_env: bool,
):
    """Use environment proxies only when AstrBot proxy settings allow them."""
    captured = {}

    class FakeResponse:
        status = 200
        reason = "OK"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeSession:
        def __init__(self, *, trust_env):
            captured["trust_env"] = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(astrbot.core, "astrbot_config", core_config)
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    success, error = await _quick_test_mcp_connection(
        {"url": "https://mcp.example.com", "transport": "sse"}
    )

    assert success is True
    assert error == ""
    assert captured["trust_env"] is expected_trust_env
