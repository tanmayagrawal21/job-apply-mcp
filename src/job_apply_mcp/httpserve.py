"""Streamable-HTTP transport with an optional bearer token (ADR-0020).

Defaults are safe: bind 127.0.0.1. Binding a non-loopback address requires a token in
``JOB_APPLY_MCP_TOKEN`` (never a CLI flag, so it cannot leak into shell history or ``ps``). When a token is
set, every request except ``GET /healthz`` must carry ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
from typing import Any

TOKEN_ENV = "JOB_APPLY_MCP_TOKEN"
ALLOWED_HOSTS_ENV = "JOB_APPLY_MCP_ALLOWED_HOSTS"   # comma-separated Host header values for non-local use


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class BearerAuth:
    """Minimal ASGI middleware. Constant-time token comparison; /healthz is the only open path."""

    def __init__(self, app: Any, token: str | None):
        self.app = app
        self.token = token.encode() if token else None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/healthz" and scope.get("method") == "GET":
            return await _respond(send, 200, {"status": "ok"})
        if self.token is not None:
            sent = b""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    sent = v[7:] if v[:7].lower() == b"bearer " else b""
                    break
            if not hmac.compare_digest(sent, self.token):
                return await _respond(send, 401, {"error": "unauthorized"},
                                      [(b"www-authenticate", b"Bearer")])
        return await self.app(scope, receive, send)


async def _respond(send: Any, status: int, body: dict, extra: list | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": payload})


def check_bind(host: str, token: str | None) -> None:
    if not is_loopback(host) and not token:
        raise SystemExit(
            f"refusing to bind {host!r} without authentication: set {TOKEN_ENV} to a long random value "
            f"(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`) or bind 127.0.0.1.")


def build_app(mcp: Any, host: str, port: int, token: str | None, allowed_hosts: list[str] | None = None) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    check_bind(host, token)
    mcp.settings.host, mcp.settings.port = host, port
    if not is_loopback(host):
        # The default allow-list only knows localhost. With a token required we either honour an explicit
        # list or turn DNS-rebinding checks off (a rebinding attack cannot supply the bearer token).
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed_hosts),
            allowed_hosts=list(allowed_hosts or []), allowed_origins=[])
    return BearerAuth(mcp.streamable_http_app(), token)


def run_http(mcp: Any, host: str = "127.0.0.1", port: int = 8765, log_level: str = "info") -> None:
    import uvicorn

    token = os.environ.get(TOKEN_ENV) or None
    extra = [h.strip() for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if h.strip()]
    app = build_app(mcp, host, port, token, extra or None)
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"job-apply-mcp listening on http://{shown}:{port}/mcp "
          f"({'bearer token required' if token else 'no auth, loopback only'})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
