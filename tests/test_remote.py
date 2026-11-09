"""Tests for the secret-path remote entry point."""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from intervals_icu_mcp.remote import (
    ALLOWED_TOOLS,
    SecretPathMiddleware,
    restrict_tools,
    validate_secret,
)

SECRET = "a" * 40


def _client() -> TestClient:
    async def mcp_endpoint(request):
        return PlainTextResponse("mcp:" + request.url.path)

    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["GET", "POST"]), Route("/health", health)])
    app.add_middleware(SecretPathMiddleware, secret=SECRET)
    return TestClient(app)


def test_health_open():
    assert _client().get("/health").text == "ok"


@pytest.mark.parametrize("path", ["/", "/mcp", "/mcp/", "/mcp/wrong", f"/mcp/{SECRET}x", f"/mcp/{SECRET}/extra", "/sse"])
def test_other_paths_404(path):
    assert _client().post(path).status_code == 404


def test_correct_secret_reaches_mcp():
    r = _client().post(f"/mcp/{SECRET}")
    assert r.status_code == 200 and r.text == "mcp:/mcp"


@pytest.mark.parametrize("bad", [None, "", "short", "x" * 31, "a" * 31 + "/", "a" * 31 + " "])
def test_secret_validation_rejects(bad):
    with pytest.raises(ValueError):
        validate_secret(bad)


def test_secret_validation_accepts():
    assert validate_secret(SECRET) == SECRET


@pytest.mark.asyncio
async def test_allowlist_has_no_destructive_tools(monkeypatch):
    monkeypatch.setenv("INTERVALS_ICU_DELETE_MODE", "none")
    from intervals_icu_mcp.server import mcp

    restrict_tools(mcp)
    names = {t.name for t in await mcp.list_tools()}
    assert names <= ALLOWED_TOOLS
    assert not [n for n in names if "delete" in n]
    assert {"icu_create_event", "icu_update_event", "icu_get_wellness_for_date"} <= names
