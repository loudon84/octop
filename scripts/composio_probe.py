"""Read-only Composio Connect probe for M0 technical validation (PRD §9).

Usage:
  uv run python scripts/composio_probe.py discover
  uv run python scripts/composio_probe.py oauth --redirect-uri http://127.0.0.1:8765/callback

Tokens stay in memory (or optional temp file under %TEMP%/octop-composio-probe/).
Never write long-lived credentials into the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from octop.infra.connectors.oauth.discovery import discover_oauth_from_mcp_url
from octop.infra.connectors.oauth.mcp import (
    build_authorize_url,
    exchange_authorization_code,
    fetch_authorization_metadata,
    refresh_access_token,
    register_dynamic_client,
)
from octop.infra.connectors.oauth.pkce import new_pkce_pair
from octop.infra.utils.ssrf_guard import safe_request

MCP_URL = "https://connect.composio.dev/mcp"
ISSUER = "https://connect.composio.dev"


def _redact(value: str | None, *, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}…{value[-keep:]}"


def _oauth_summary(tokens: dict[str, Any]) -> dict[str, bool]:
    """Return token presence only; never persist or display token material."""
    return {
        "has_access_token": bool(tokens.get("access_token")),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "has_id_token": bool(tokens.get("id_token")),
    }


async def cmd_discover() -> dict[str, Any]:
    discovery = await discover_oauth_from_mcp_url(MCP_URL)
    issuer = str(discovery.get("issuer") or ISSUER)
    metadata = discovery.get("metadata")
    if not isinstance(metadata, dict):
        metadata = await fetch_authorization_metadata(issuer)
    unauth = await safe_request("POST", MCP_URL, json={}, timeout=20.0)
    result = {
        "date": time.strftime("%Y-%m-%d"),
        "mcp_url": MCP_URL,
        "discovery_available": bool(discovery.get("available")),
        "issuer": issuer,
        "resource": discovery.get("resource"),
        "authorization_endpoint": metadata.get("authorization_endpoint"),
        "token_endpoint": metadata.get("token_endpoint"),
        "registration_endpoint": metadata.get("registration_endpoint"),
        "code_challenge_methods_supported": metadata.get("code_challenge_methods_supported"),
        "token_endpoint_auth_methods_supported": metadata.get(
            "token_endpoint_auth_methods_supported"
        ),
        "scopes_supported": metadata.get("scopes_supported"),
        "unauth_mcp_status": unauth.status_code,
        "unauth_www_authenticate": unauth.headers.get("www-authenticate"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        error = (qs.get("error") or [""])[0]
        _CallbackHandler.result = {"code": code, "state": state, "error": error}
        body = b"<html><body>OK - return to the terminal.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


async def cmd_oauth(*, redirect_uri: str, wait_sec: int) -> dict[str, Any]:
    discovery = await discover_oauth_from_mcp_url(MCP_URL)
    issuer = str(discovery.get("issuer") or ISSUER)
    resource = str(discovery.get("resource") or MCP_URL).strip() or None
    metadata = discovery.get("metadata")
    if not isinstance(metadata, dict):
        metadata = await fetch_authorization_metadata(issuer)

    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    path = parsed.path or "/callback"

    reg = await register_dynamic_client(
        metadata,
        issuer=issuer,
        redirect_uri=redirect_uri,
        client_name="Octop Composio Probe",
    )
    client_id = str(reg["client_id"])
    client_secret = str(reg.get("client_secret") or "") or None
    verifier, challenge = new_pkce_pair()
    state = secrets.token_urlsafe(24)
    scopes = metadata.get("scopes_supported")
    scope = (
        " ".join(str(s) for s in scopes if s)
        if isinstance(scopes, list) and scopes
        else "openid profile email offline_access"
    )
    authorize_url = build_authorize_url(
        metadata,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        scope=scope,
        resource=resource,
    )
    print("Open this URL in a browser and complete Composio login:\n", authorize_url)

    server = HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    deadline = time.time() + wait_sec
    while time.time() < deadline and not _CallbackHandler.result:
        await asyncio.sleep(0.2)
    server.server_close()
    cb = _CallbackHandler.result
    if not cb or cb.get("error"):
        out = {
            "ok": False,
            "stage": "callback",
            "error": cb.get("error") if cb else "timeout",
            "client_id_redacted": _redact(client_id),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
    if cb.get("state") != state:
        out = {"ok": False, "stage": "state_mismatch"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    tokens = await exchange_authorization_code(
        metadata,
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        code=str(cb["code"]),
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        resource=resource,
    )
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or "")

    tool_summary = await _mcp_tools_list(access)
    refreshed: dict[str, Any] | None = None
    if refresh:
        refreshed = await refresh_access_token(
            metadata,
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh,
            resource=resource,
        )

    out = {
        "ok": True,
        "client_id_redacted": _redact(client_id),
        **_oauth_summary(tokens),
        "tool_count": tool_summary.get("count"),
        "tool_names": tool_summary.get("names"),
        "refresh_ok": bool(refreshed and refreshed.get("access_token")),
        "path_hint": "callback_path=" + path,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


async def _mcp_tools_list(access_token: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "octop-composio-probe", "version": "0.1"},
        },
    }
    init = await safe_request("POST", MCP_URL, headers=headers, json=init_body, timeout=30.0)
    if init.status_code >= 400:
        return {"count": 0, "names": [], "error": f"initialize HTTP {init.status_code}"}
    # session id if present
    session = init.headers.get("mcp-session-id")
    if session:
        headers["mcp-session-id"] = session
    # notifications/initialized
    await safe_request(
        "POST",
        MCP_URL,
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=20.0,
    )
    listed = await safe_request(
        "POST",
        MCP_URL,
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        timeout=30.0,
    )
    names: list[str] = []
    try:
        data = listed.json()
        tools = (data.get("result") or {}).get("tools") or []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("name"):
                names.append(str(tool["name"]))
    except Exception as exc:
        return {"count": 0, "names": [], "error": str(exc)[:200]}
    return {"count": len(names), "names": names}


def main() -> None:
    parser = argparse.ArgumentParser(description="Composio Connect M0 probe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover", help="Anonymous discovery + unauth MCP probe")
    oauth = sub.add_parser("oauth", help="Interactive DCR + authorize + MCP tools/list")
    oauth.add_argument(
        "--redirect-uri",
        default="http://127.0.0.1:8765/callback",
        help="Must match the local callback listener",
    )
    oauth.add_argument("--wait-sec", type=int, default=300)
    args = parser.parse_args()
    if args.cmd == "discover":
        asyncio.run(cmd_discover())
    elif args.cmd == "oauth":
        asyncio.run(cmd_oauth(redirect_uri=args.redirect_uri, wait_sec=args.wait_sec))
    else:
        parser.error(f"unknown command {args.cmd}")


if __name__ == "__main__":
    main()
