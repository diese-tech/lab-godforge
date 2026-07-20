"""ActivityBackendClient configuration/logic (Issue #48, Phase 3c)."""

import pytest

from utils.activity_backend import ActivityBackendClient


def test_disabled_when_no_base_url():
    assert ActivityBackendClient("", "key").enabled is False


def test_enabled_and_trailing_slash_stripped():
    client = ActivityBackendClient("https://api.example.com/", "key")
    assert client.enabled is True
    assert client.base_url == "https://api.example.com"


def test_headers_include_api_key():
    client = ActivityBackendClient("https://api.example.com", "secret")
    headers = client.headers()
    assert headers["X-Api-Key"] == "secret"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.example.com", "wss://api.example.com/ws"),
        ("http://localhost:8000", "ws://localhost:8000/ws"),
    ],
)
def test_ws_url_scheme(base, expected):
    assert ActivityBackendClient(base, "k").ws_url() == expected


async def test_post_returns_none_on_failure():
    # Unreachable host -> swallowed to None (best-effort behavior preserved).
    client = ActivityBackendClient("http://127.0.0.1:1", "k")
    assert await client.post("/x") is None
    assert await client.get("/x") is None
