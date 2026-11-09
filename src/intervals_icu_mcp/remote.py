"""Remote entry point: Streamable HTTP behind a secret URL path.

Differences from ``server.main`` (which stays untouched):

* The MCP endpoint is only reachable at ``/mcp/<MCP_PATH_SECRET>``. Every other
  path except ``/health`` returns a bare 404, so a request without the secret
  never reaches MCP or the Intervals.icu credentials.
* Delete mode is forced to ``none`` and only an allowlist of tools stays
  registered (read activities / wellness / events, create + update events).
* No access logging: uvicorn would write the secret path to the logs.

Environment variables:
    MCP_PATH_SECRET        required, at least 32 characters, URL-safe
    INTERVALS_ICU_API_KEY  required
    INTERVALS_ICU_ATHLETE_ID required
    PORT                   optional, default 8000 (Railway sets this)
"""

from __future__ import annotations

import hmac
import os
import re
import sys

MIN_SECRET_LENGTH = 32
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# The model can only ever call these. Anything else is removed at startup.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        # activities (read)
        "icu_get_recent_activities",
        "icu_get_activities_by_date",
        "icu_get_activities_around",
        "icu_get_activity_details",
        "icu_get_activity_intervals",
        "icu_get_activity_streams",
        "icu_search_activities",
        # wellness (read)
        "icu_get_wellness_data",
        "icu_get_wellness_for_date",
        # events / calendar (read, create, update; never delete)
        "icu_get_calendar_events",
        "icu_get_event",
        "icu_get_upcoming_workouts",
        "icu_create_event",
        "icu_bulk_create_events",
        "icu_update_event",
        # context needed for planning (read)
        "icu_get_athlete_profile",
        "icu_get_sport_settings",
        "icu_get_fitness_summary",
    }
)


def validate_secret(secret: str | None) -> str:
    """Return the secret if usable, otherwise raise ValueError."""
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"MCP_PATH_SECRET must be set and at least {MIN_SECRET_LENGTH} characters")
    if not _SECRET_RE.match(secret):
        raise ValueError("MCP_PATH_SECRET may only contain A-Z a-z 0-9 _ -")
    return secret


class SecretPathMiddleware:
    """Pure ASGI middleware: allow /health and /mcp/<secret>, 404 everything else.

    The secret segment is stripped, so the wrapped app only sees ``/mcp``.
    The comparison is constant-time.
    """

    def __init__(self, app, secret: str, endpoint: str = "/mcp") -> None:
        self.app = app
        self._secret = secret.encode()
        self._prefix = endpoint.rstrip("/") + "/"
        self._endpoint = endpoint.rstrip("/")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await _not_found(send)
            return

        path: str = scope["path"]
        if path == "/health":
            await self.app(scope, receive, send)
            return

        if path.startswith(self._prefix):
            rest = path[len(self._prefix) :]
            candidate, _, tail = rest.partition("/")
            if hmac.compare_digest(candidate.encode(), self._secret) and not tail:
                new_scope = dict(scope)
                new_scope["path"] = self._endpoint
                new_scope["raw_path"] = self._endpoint.encode()
                await self.app(new_scope, receive, send)
                return

        await _not_found(send)


async def _not_found(send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"9")],
        }
    )
    await send({"type": "http.response.body", "body": b"Not Found"})


def restrict_tools(mcp, allowed: frozenset[str] = ALLOWED_TOOLS) -> list[str]:
    """Remove every registered tool that is not allowlisted. Returns removed names."""
    removed: list[str] = []
    for tool in list(mcp.local_provider._components.values()):  # noqa: SLF001
        name = getattr(tool, "name", None)
        if name and getattr(tool, "key", "").startswith("tool:") and name not in allowed:
            mcp.local_provider.remove_tool(name)
            removed.append(name)
    return removed


def build_app(secret: str):
    """Build the ASGI app. Imports the server lazily so env overrides apply first."""
    # Force the safest delete mode before the server module reads its config.
    os.environ["INTERVALS_ICU_DELETE_MODE"] = "none"

    from .server import mcp  # noqa: PLC0415

    restrict_tools(mcp)
    app = mcp.http_app(path="/mcp")
    app.add_middleware(SecretPathMiddleware, secret=secret)
    return app, mcp


def main() -> None:
    import uvicorn  # noqa: PLC0415

    try:
        secret = validate_secret(os.environ.get("MCP_PATH_SECRET"))
    except ValueError as exc:
        print(f"remote: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    app, _ = build_app(secret)
    port = int(os.environ.get("PORT", "8000"))
    print(f"remote: listening on 0.0.0.0:{port} (path secret set, access log off)", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, log_level="warning")  # noqa: S104


if __name__ == "__main__":
    main()
