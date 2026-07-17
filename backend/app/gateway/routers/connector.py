"""Proxy router for AnyConnct admin API.

Forwards browser requests to an AnyConnct runtime so the wasp frontend
can manage connector providers, connections, and OAuth flows without
talking to AnyConnct directly.  Wasp stores nothing — AnyConnct
remains the sole source of truth for credentials, connections, and run
logs.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/connector", tags=["connector"])

logger = logging.getLogger(__name__)

_ANYCONNCT_BASE_URL = os.getenv(
    "ANYCONNCT_BASE_URL", "http://localhost:5173"
).rstrip("/")

_ALIAS_HEADER = "x-oo-connector-alias"

_CONTENT_HEADERS = {"content-type", "content-length", "content-encoding"}
_FORWARD_HEADER_BLOCKLIST = {"host", "transfer-encoding", "connection"}


def _get_connector_auth_header() -> str | None:
    """Return the Authorization header configured for the connectors MCP server."""
    try:
        from deerflow.config.extensions_config import ExtensionsConfig

        config = ExtensionsConfig.from_file()
        server_cfg = config.mcp_servers.get("connectors")
        if server_cfg and server_cfg.enabled:
            headers = server_cfg.headers or {}
            return headers.get("Authorization") or headers.get("authorization")
    except Exception:
        logger.debug("Cannot read connector auth header", exc_info=True)
    return None


def _oc_url(path: str) -> str:
    """Build a full AnyConnct URL for *path* (must start with ``/``)."""
    return f"{_ANYCONNCT_BASE_URL}{path}"


def _resolve_connector_alias(request: Request) -> str | None:
    """Return the current user's connector alias, or ``None`` if unavailable.

    Uses DeerFlow's ``get_effective_user_id`` so the same account isolation
    that applies to threads/memory also applies to connector connections.
    """
    from deerflow.runtime.user_context import get_effective_user_id

    try:
        user_id = get_effective_user_id()
        logger.info("Connector alias resolved: %s", user_id)
        if user_id and user_id != "default":
            return user_id
    except Exception:
        logger.debug("Cannot resolve connector alias", exc_info=True)
    return None


def _inject_alias(headers: dict[str, str], alias: str | None) -> None:
    """Inject the ``x-oo-connector-alias`` header into *headers*.

    Only sets the header when *alias* is non-empty, so the default
    connection is used when there is no per-user alias.
    """
    if alias:
        headers[_ALIAS_HEADER] = alias


async def _proxy(
    request: Request,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
) -> StreamingResponse:
    """Forward *request* to AnyConnct and stream the response back."""
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _FORWARD_HEADER_BLOCKLIST
    }
    _inject_alias(headers, _resolve_connector_alias(request))
    # Inject the app API key so anyconnct scopes results to this app
    auth = _get_connector_auth_header()
    if auth and "authorization" not in {k.lower() for k in headers}:
        headers["Authorization"] = auth
    url = _oc_url(path)
    logger.debug("connector proxy: %s %s", method, url)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        upstream = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body or request.stream(),
            follow_redirects=False,
        )

    # Only forward content-related headers, not transport ones.
    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() in _CONTENT_HEADERS
    }

    # For OAuth redirects, rewrite the Location header so the browser
    # comes back to wasp instead of landing on AnyConnct directly.
    if 300 <= upstream.status_code < 400:
        loc = upstream.headers.get("location")
        if loc and loc.startswith(_ANYCONNCT_BASE_URL):
            # Relative redirects are fine; rewrite absolute ones that
            # point back to the OC origin.
            rewritten = loc.replace(
                _ANYCONNCT_BASE_URL, "", 1
            )
            response_headers["location"] = rewritten
        elif loc and loc.startswith("/oauth/callback"):
            # Already relative — pass through as-is.
            response_headers["location"] = loc

    return StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


async def _proxy_raw(
    request: Request,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
) -> httpx.Response:
    """Forward *request* to AnyConnct and return the full httpx Response."""
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _FORWARD_HEADER_BLOCKLIST
    }
    _inject_alias(headers, _resolve_connector_alias(request))
    # Inject the app API key so anyconnct scopes results to this app
    auth = _get_connector_auth_header()
    if auth and "authorization" not in {k.lower() for k in headers}:
        headers["Authorization"] = auth
    url = _oc_url(path)
    logger.debug("connector proxy (raw): %s %s", method, url)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        return await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body or request.stream(),
            follow_redirects=False,
        )


async def _read_upstream_body(upstream: httpx.Response) -> bytes:
    """Drain the upstream response body into bytes."""
    return await upstream.aread()


def _json_response(data: object, status_code: int = 200) -> StreamingResponse:
    """Return a StreamingResponse with JSON-encoded *data*."""
    import json as _json

    content = _json.dumps(data, ensure_ascii=False, default=str)
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        status_code=status_code,
        media_type="application/json",
    )


# ── Provider catalogue (read-only, public) ───────────────────────────


@router.get("/providers")
async def list_providers(request: Request) -> StreamingResponse:
    """List available provider apps scoped to the configured app API key."""
    upstream = await _proxy_raw(request, "GET", "/v1/providers")
    body = await _read_upstream_body(upstream)
    envelope = json.loads(body) if body else {}
    data = envelope.get("data", []) if isinstance(envelope, dict) else []
    return _json_response({"providers": data})


@router.get("/providers/{service:path}")
async def get_provider(request: Request, service: str) -> StreamingResponse:
    """Get a single provider's detail including actions and auth config."""
    return await _proxy(request, "GET", f"/api/providers/{service}")


# ── Connections (per-user) ───────────────────────────────────────────


@router.get("/connections")
async def list_connections(request: Request) -> StreamingResponse:
    """List connections for the current user, wrapped as ``{connections: [...]}``."""
    user_id = _resolve_connector_alias(request) or "default"
    upstream = await _proxy_raw(request, "GET", f"/v1/connections?userId={user_id}")
    body = await _read_upstream_body(upstream)
    envelope = json.loads(body) if body else {}
    data = envelope.get("data", []) if isinstance(envelope, dict) else []
    return _json_response({"connections": data})


@router.put("/connections/{service:path}")
async def upsert_connection(
    request: Request, service: str
) -> StreamingResponse:
    """Create or replace a connection for the configured app."""
    body = await request.body()
    return await _proxy(request, "PUT", f"/v1/connections/{service}", body=body)


@router.delete("/connections/{service:path}")
async def delete_connection(
    request: Request, service: str
) -> StreamingResponse:
    """Remove the configured app's connection."""
    return await _proxy(request, "DELETE", f"/v1/connections/{service}")


# ── OAuth ─────────────────────────────────────────────────────────────


@router.post("/oauth/authorize")
async def oauth_authorize(request: Request) -> StreamingResponse:
    """Start an OAuth authorization flow. Returns the redirect URL."""
    body = await request.body()
    return await _proxy(
        request, "POST", "/v1/oauth/authorizations", body=body
    )


@router.get("/oauth/callback")
@router.post("/oauth/callback")
async def oauth_callback(request: Request) -> StreamingResponse:
    """Handle OAuth callback — AnyConnct exchanges the code for tokens."""
    return await _proxy(
        request,
        request.method,
        f"/oauth/callback?{request.url.query}",
    )


# ── Actions (read-only discovery) ─────────────────────────────────────


@router.get("/actions")
async def list_actions(request: Request) -> StreamingResponse:
    """List or search actions scoped to the configured app."""
    return await _proxy(
        request, "GET", f"/v1/actions?{request.url.query}"
    )


@router.get("/actions/{action_id:path}")
async def get_action(request: Request, action_id: str) -> StreamingResponse:
    """Get a single action's detail."""
    return await _proxy(request, "GET", f"/api/actions/{action_id}")


# ── Runtime tokens ───────────────────────────────────────────────────


@router.get("/runtime-tokens")
async def list_runtime_tokens(request: Request) -> StreamingResponse:
    """List runtime tokens (admin)."""
    return await _proxy(request, "GET", "/api/runtime-tokens")


@router.post("/runtime-tokens")
async def create_runtime_token(request: Request) -> StreamingResponse:
    """Create a new runtime token (admin)."""
    body = await request.body()
    return await _proxy(request, "POST", "/api/runtime-tokens", body=body)


@router.delete("/runtime-tokens/{token_id}")
async def delete_runtime_token(
    request: Request, token_id: str
) -> StreamingResponse:
    """Delete a runtime token (admin)."""
    return await _proxy(request, "DELETE", f"/api/runtime-tokens/{token_id}")
