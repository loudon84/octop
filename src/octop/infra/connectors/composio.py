"""Personal Composio Connect connector (kind ``composio-connect``).

Owns Composio-specific lifecycle rules: single connection per user, no sharing,
server-side credential save, layered status, and meta-tool based app discovery.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from octop.infra.connectors.builder import mcp_server_name, validate_create_credentials
from octop.infra.connectors.catalog import get_catalog_entry
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

COMPOSIO_KIND = "composio-connect"
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"
COMPOSIO_DISPLAY_NAME = "My Composio"

# Official Connect meta-tools (docs.composio.dev/docs/composio-connect).
META_SEARCH = "COMPOSIO_SEARCH_TOOLS"
META_SCHEMAS = "COMPOSIO_GET_TOOL_SCHEMAS"
META_EXECUTE = "COMPOSIO_MULTI_EXECUTE_TOOL"
META_MANAGE = "COMPOSIO_MANAGE_CONNECTIONS"
META_WAIT = "COMPOSIO_WAIT_FOR_CONNECTIONS"
META_WORKBENCH = "COMPOSIO_REMOTE_WORKBENCH"
META_BASH = "COMPOSIO_REMOTE_BASH_TOOL"

# Page APIs may call these; POST-only for manage (may start OAuth).
READONLY_META_TOOLS = frozenset({META_SEARCH, META_SCHEMAS})
AUTH_SIDE_EFFECT_META_TOOLS = frozenset({META_MANAGE, META_WAIT})


def is_composio_kind(kind: str) -> bool:
    return kind == COMPOSIO_KIND


def reject_composio_share(*, kind: str, shared: bool | None) -> None:
    if is_composio_kind(kind) and shared is True:
        raise OctopError(
            ErrorCode.COMPOSIO_SHARE_FORBIDDEN,
            "personal Composio connections cannot be shared",
        )


def reject_composio_default_open(*, kind: str, default_open: bool | None) -> None:
    if is_composio_kind(kind) and default_open is True:
        raise OctopError(
            ErrorCode.COMPOSIO_DEFAULT_OPEN_FORBIDDEN,
            "Composio cannot be enabled by default for every chat",
        )


def list_user_composio(repo: ConnectorRepo, user_id: int) -> list[Any]:
    return [row for row in repo.list_by_user(user_id) if is_composio_kind(row.kind)]


def get_user_composio(repo: ConnectorRepo, user_id: int) -> Any | None:
    rows = list_user_composio(repo, user_id)
    active = [r for r in rows if r.status == "active"]
    if active:
        return active[0]
    return rows[0] if rows else None


def read_credential_gen(config: dict[str, Any]) -> int:
    raw = config.get("credential_gen")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def bump_credential_gen(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    out["credential_gen"] = read_credential_gen(out) + 1
    return out


def decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Unverified JWT claims are deliberately never accepted as identity."""
    del id_token
    return {}


def account_email_from_tokens(tokens: dict[str, Any]) -> str | None:
    del tokens
    return None


def layered_status(
    *,
    row: Any,
    config: dict[str, Any],
    health: str = "unknown",
) -> dict[str, str]:
    auth = str(config.get("auth_status") or "")
    if not auth:
        if row.status == "active" and row.has_credentials or row.status == "disabled":
            auth = "authenticated"
        else:
            auth = "disconnected"
    usage = "disabled" if row.status == "disabled" else "enabled"
    return {
        "auth": auth,
        "health": health,
        "usage": usage,
    }


def connection_public_dict(
    row: Any,
    *,
    config: dict[str, Any] | None = None,
    health: str | None = None,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    cfg = config if config is not None else ConnectorRepo.parse_config_json(row)
    # No validated issuer/audience/nonce verification is available here.
    account_email = None
    display = str(row.display_name or "").strip() or COMPOSIO_DISPLAY_NAME
    stored_health = cfg.get("mcp_health")
    if stored_health not in {"reachable", "unavailable", "unknown"}:
        stored_health = "unknown"
    statuses = layered_status(row=row, config=cfg, health=health or str(stored_health))
    stored_caps = cfg.get("capabilities")
    caps = capabilities
    if caps is None and stored_caps is not None:
        caps = _validated_capabilities({"capabilities": stored_caps})
    if caps is None:
        caps = {
            "full_catalog": False,
            "list_connected_apps": False,
            "account_identity": False,
            "check_app_auth": False,
            "search_apps": False,
        }
    return {
        "instance_id": row.instance_id,
        "kind": COMPOSIO_KIND,
        "display_name": display,
        "account_email": account_email,
        "source": "composio",
        "mcp_server_name": row.mcp_server_name,
        "status": row.status,
        "auth_status": statuses["auth"],
        "health": statuses["health"],
        "usage": statuses["usage"],
        "credential_gen": read_credential_gen(cfg),
        "last_checked_at": cfg.get("last_checked_at"),
        "capabilities": caps,
        "default_open": False,
        "shared": False,
        "owner_user_id": row.user_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def can_admin_force_disconnect(*, actor_role: str, owner_user_id: int, actor_id: int) -> bool:
    if owner_user_id == actor_id:
        return True
    return actor_role == "admin"


def can_view_composio(*, actor_role: str, owner_user_id: int, actor_id: int) -> bool:
    return owner_user_id == actor_id or actor_role == "admin"


def can_mutate_composio(*, owner_user_id: int, actor_id: int) -> bool:
    """Owner-only for authorize / patch / share / default_open."""
    return owner_user_id == actor_id


class ComposioService:
    """Domain helpers for personal Composio connections."""

    def __init__(
        self,
        *,
        repo: ConnectorRepo,
        connector_svc: Any,
    ) -> None:
        self._repo = repo
        self._svc = connector_svc

    def get_connection(self, user_id: int) -> dict[str, Any] | None:
        row = get_user_composio(self._repo, user_id)
        if row is None:
            return None
        return connection_public_dict(row)

    def apply_oauth_tokens(
        self,
        *,
        user_id: int,
        tokens: dict[str, Any],
        display_name: str | None = None,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or replace the user's single Composio connection (server-side)."""
        entry = get_catalog_entry(COMPOSIO_KIND)
        if entry is None:
            raise OctopError(
                ErrorCode.CONNECTOR_KIND_UNSUPPORTED, "composio-connect not in catalog"
            )
        cred_payload = validate_create_credentials(COMPOSIO_KIND, dict(tokens))
        email = None
        validated_caps = _validated_capabilities(validation)
        existing = list_user_composio(self._repo, user_id)
        now = int(time.time())
        if existing:
            row = existing[0]
            cfg = ConnectorRepo.parse_config_json(row)
            cfg = bump_credential_gen(cfg)
            # Identity from an unverified id_token is not trustworthy. Clear
            # legacy or previous-account identity on every credential change.
            cfg.pop("account_email", None)
            if email:
                cfg["account_email"] = email
            cfg["auth_status"] = "authenticated"
            cfg["last_checked_at"] = now
            cfg.pop("mcp_health", None)
            cfg.pop("capabilities", None)
            if validation and validation.get("health") == "reachable":
                cfg["mcp_health"] = "reachable"
                cfg["capabilities"] = validated_caps
            cfg.pop("default_open", None)
            name = (display_name or row.display_name or COMPOSIO_DISPLAY_NAME).strip()
            self._repo.update_metadata(row.instance_id, display_name=name, shared=False)
            self._repo.update_status(row.instance_id, "active")
            self._repo.update_config_json(row.instance_id, json.dumps(cfg))
            self._svc.encrypt_and_store(instance_id=row.instance_id, payload=cred_payload)
            refreshed = self._repo.get(row.instance_id)
            assert refreshed is not None
            return connection_public_dict(
                refreshed,
                config=cfg,
                health="reachable"
                if validation and validation.get("health") == "reachable"
                else "unknown",
            )

        candidate_instance_id = new_ulid()
        new_cfg: dict[str, Any] = bump_credential_gen({})
        new_cfg["auth_status"] = "authenticated"
        new_cfg["last_checked_at"] = now
        if validation and validation.get("health") == "reachable":
            new_cfg["mcp_health"] = "reachable"
            new_cfg["capabilities"] = validated_caps
        if email:
            new_cfg["account_email"] = email
        instance_id = self._repo.create(
            instance_id=candidate_instance_id,
            user_id=user_id,
            kind=COMPOSIO_KIND,
            display_name=(display_name or COMPOSIO_DISPLAY_NAME).strip() or COMPOSIO_DISPLAY_NAME,
            mcp_server_name=mcp_server_name(COMPOSIO_KIND, candidate_instance_id),
            shared=False,
            config_json=json.dumps(new_cfg, ensure_ascii=False),
        )
        if instance_id != candidate_instance_id:
            # A concurrent first OAuth apply won the singleton insert. Update that
            # row instead of leaving the validated candidate on a phantom ID.
            winner = self._repo.get(instance_id)
            assert winner is not None
            new_cfg = bump_credential_gen(ConnectorRepo.parse_config_json(winner))
            new_cfg.pop("account_email", None)
            new_cfg["auth_status"] = "authenticated"
            new_cfg["last_checked_at"] = now
            if validation and validation.get("health") == "reachable":
                new_cfg["mcp_health"] = "reachable"
                new_cfg["capabilities"] = validated_caps
            self._repo.update_metadata(
                instance_id, display_name=display_name or COMPOSIO_DISPLAY_NAME, shared=False
            )
            self._repo.update_status(instance_id, "active")
            self._repo.update_config_json(instance_id, json.dumps(new_cfg, ensure_ascii=False))
        self._svc.encrypt_and_store(instance_id=instance_id, payload=cred_payload)
        row = self._repo.get(instance_id)
        assert row is not None
        return connection_public_dict(
            row,
            config=new_cfg,
            health="reachable"
            if validation and validation.get("health") == "reachable"
            else "unknown",
        )

    def set_usage(self, user_id: int, *, enabled: bool) -> dict[str, Any]:
        row = get_user_composio(self._repo, user_id)
        if row is None:
            raise OctopError(ErrorCode.CONNECTOR_NOT_FOUND, "Composio is not connected")
        self._repo.update_status(row.instance_id, "active" if enabled else "disabled")
        refreshed = self._repo.get(row.instance_id)
        assert refreshed is not None
        return connection_public_dict(refreshed)

    def disconnect(self, user_id: int, *, actor_id: int, actor_role: str) -> None:
        row = get_user_composio(self._repo, user_id)
        if row is None:
            raise OctopError(ErrorCode.CONNECTOR_NOT_FOUND, "Composio is not connected")
        if not can_admin_force_disconnect(
            actor_role=actor_role, owner_user_id=row.user_id, actor_id=actor_id
        ):
            raise OctopError(ErrorCode.FORBIDDEN, "not your Composio connection")
        if row.user_id != actor_id and actor_role == "admin":
            # admin force-disconnect only
            pass
        elif row.user_id != actor_id:
            raise OctopError(ErrorCode.FORBIDDEN, "not your Composio connection")
        self._repo.delete(row.instance_id)

    async def check_connection(self, user_id: int) -> dict[str, Any]:
        row = get_user_composio(self._repo, user_id)
        if row is None:
            raise OctopError(ErrorCode.CONNECTOR_NOT_FOUND, "Composio is not connected")
        cfg = ConnectorRepo.parse_config_json(row)
        health = "unknown"
        auth = str(cfg.get("auth_status") or "authenticated")
        tools: dict[str, Any] = {}
        try:
            creds = await self._svc.ensure_fresh_credentials(row.instance_id, COMPOSIO_KIND)
            access = str(creds.get("access_token") or "").strip()
            if not access:
                health = "unavailable"
                auth = "reauth_required"
            else:
                tools = await self._mcp_tools_list(access)
                if tools.get("ok"):
                    health = "reachable"
                    auth = "authenticated"
                else:
                    health = "unavailable"
                    if tools.get("status") in {401, 403}:
                        auth = "reauth_required"
        except Exception:
            logger.info("Composio connection check failed for user %s", user_id)
            health = "unavailable"
        cfg["auth_status"] = auth
        cfg["last_checked_at"] = int(time.time())
        cfg["mcp_health"] = health
        cfg["capabilities"] = _capabilities(tools.get("tools", []))
        self._repo.update_config_json(row.instance_id, json.dumps(cfg))
        refreshed = self._repo.get(row.instance_id)
        assert refreshed is not None
        tool_list = tools.get("tools", [])
        return connection_public_dict(
            refreshed,
            config=cfg,
            health=health,
            capabilities=_capabilities(tool_list),
        )

    async def validate_oauth_tokens(self, tokens: dict[str, Any]) -> dict[str, Any]:
        """Verify candidate OAuth credentials before a callback persists them."""
        creds = validate_create_credentials(COMPOSIO_KIND, dict(tokens))
        access = str(creds.get("access_token") or "").strip()
        result = await self._mcp_tools_list(access)
        if not result.get("ok"):
            status = result.get("status")
            raise OctopError(
                ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
                f"Composio MCP validation failed ({status or 'protocol'})",
            )
        return {"health": "reachable", "capabilities": _capabilities(result["tools"])}

    async def search_apps(self, user_id: int, query: str) -> dict[str, Any]:
        access = await self._require_access_token(user_id)
        tools = await self._mcp_tools_list(access)
        schema = _tool_schema(tools.get("tools", []), META_SEARCH)
        arguments = _search_arguments(schema, query.strip() or "list available apps")
        raw = await self._mcp_call_tool(access, META_SEARCH, arguments)
        apps = _normalize_search_results(raw)
        return {
            "query": query,
            "apps": apps,
            "source": "COMPOSIO_SEARCH_TOOLS",
            "partial": True,
        }

    async def start_app_auth(self, user_id: int, toolkit: str) -> dict[str, Any]:
        access = await self._require_access_token(user_id)
        toolkit = toolkit.strip()
        if not toolkit:
            raise OctopError(ErrorCode.CONNECTOR_INVALID_CREDENTIALS, "toolkit is required")
        tools = await self._mcp_tools_list(access)
        schema = _tool_schema(tools.get("tools", []), META_MANAGE)
        arguments = _manage_arguments(schema, toolkit)
        raw = await self._mcp_call_tool(access, META_MANAGE, arguments)
        redirect_url = _authorization_url(raw)
        return {
            "toolkit": toolkit,
            "result": {"redirect_url": redirect_url} if redirect_url else {"status": "unknown"},
            "source": "COMPOSIO_MANAGE_CONNECTIONS",
        }

    async def check_app_status(self, user_id: int, toolkit: str) -> dict[str, Any]:
        # Official manage-connections can initiate OAuth for missing apps.
        # No verified read-only status API is available in this adapter.
        return {
            "toolkit": toolkit.strip() or None,
            "result": None,
            "source": "COMPOSIO_MANAGE_CONNECTIONS",
            "status": "unavailable",
        }

    async def _require_access_token(self, user_id: int) -> str:
        row = get_user_composio(self._repo, user_id)
        if row is None or row.status != "active":
            raise OctopError(ErrorCode.CONNECTOR_NOT_FOUND, "Composio is not connected")
        creds = await self._svc.ensure_fresh_credentials(row.instance_id, COMPOSIO_KIND)
        access = str(creds.get("access_token") or "").strip()
        if not access:
            raise OctopError(
                ErrorCode.CONNECTOR_INVALID_CREDENTIALS,
                "Composio credentials expired; re-authorize",
            )
        return access

    async def _mcp_tools_list(self, access_token: str) -> dict[str, Any]:
        try:
            async with _open_session(access_token) as session:
                await session.initialize()
                tools = await session.list_tools()
                return {
                    "ok": True,
                    "status": 200,
                    "tools": tools.tools,
                }
        except httpx.HTTPStatusError as exc:
            return {"ok": False, "status": exc.response.status_code}
        except Exception:
            logger.info("Composio MCP tools/list failed")
            return {"ok": False, "status": None}

    async def _mcp_call_tool(
        self,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        try:
            async with _open_session(access_token) as session:
                await session.initialize()
                listed = await session.list_tools()
                schema = _tool_schema(listed.tools, tool_name)
                _validate_arguments(schema, arguments)
                result = await session.call_tool(tool_name, arguments)
                if result.isError:
                    raise OctopError(
                        ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
                        "Composio tool call failed",
                    )
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)
        except OctopError:
            raise
        except httpx.HTTPStatusError as exc:
            raise OctopError(
                ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
                f"Composio MCP request failed ({exc.response.status_code})",
            ) from exc
        except Exception as exc:
            raise OctopError(
                ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
                "Composio MCP request failed",
            ) from exc


@asynccontextmanager
async def _open_session(access_token: str) -> Any:
    async with (
        streamablehttp_client(
            COMPOSIO_MCP_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
            sse_read_timeout=30,
            httpx_client_factory=_composio_httpx_client_factory,
        ) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        yield session


def _composio_httpx_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Build the MCP SDK client without forwarding bearer auth on redirects."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0),
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )


def _capabilities(tools: Any) -> dict[str, bool]:
    can_search = False
    try:
        _search_arguments(_tool_schema(tools, META_SEARCH), "capability probe")
        can_search = True
    except (OctopError, TypeError, AttributeError):
        pass
    return {
        "full_catalog": False,
        "list_connected_apps": False,
        "account_identity": False,
        "check_app_auth": False,
        "search_apps": can_search,
    }


def _validated_capabilities(validation: dict[str, Any] | None) -> dict[str, bool]:
    raw = validation.get("capabilities") if isinstance(validation, dict) else None
    names = (
        "full_catalog",
        "list_connected_apps",
        "account_identity",
        "check_app_auth",
        "search_apps",
    )
    if not isinstance(raw, dict):
        return dict.fromkeys(names, False)
    # Only search is currently adapter-verified; no verified identity or
    # read-only connected-app status surface exists for Connect.
    return {name: raw.get(name) is True if name == "search_apps" else False for name in names}


def _tool_schema(tools: Any, name: str) -> dict[str, Any]:
    for tool in tools:
        if getattr(tool, "name", None) == name:
            schema = getattr(tool, "inputSchema", None)
            if isinstance(schema, dict):
                return schema
    raise OctopError(
        ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
        f"Composio capability {name} is unavailable",
    )


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or any(key not in properties for key in arguments):
        raise OctopError(
            ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
            "Composio tool schema does not support this operation",
        )
    required = schema.get("required", [])
    if isinstance(required, list) and any(key not in arguments for key in required):
        raise OctopError(
            ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE,
            "Composio tool schema requires unsupported arguments",
        )


def _search_arguments(schema: dict[str, Any], query: str) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise OctopError(ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE, "Invalid Composio search schema")
    query_property = properties.get("query")
    queries_property = properties.get("queries")
    if isinstance(query_property, dict) and query_property.get("type") == "string":
        args: dict[str, Any] = {"query": query}
    elif isinstance(queries_property, dict) and queries_property.get("type") == "array":
        item_schema = queries_property.get("items", {})
        if not isinstance(item_schema, dict) or item_schema.get("type") != "object":
            raise OctopError(
                ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE, "Unsupported Composio search schema"
            )
        item_properties = item_schema.get("properties", {})
        item_properties = item_properties if isinstance(item_properties, dict) else {}
        query_field = next(
            (
                field
                for field in ("use_case", "query")
                if isinstance(item_properties.get(field), dict)
                and item_properties[field].get("type") == "string"
            ),
            None,
        )
        if query_field is None:
            raise OctopError(
                ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE, "Unsupported Composio search schema"
            )
        args = {"queries": [{query_field: query}]}
    else:
        raise OctopError(
            ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE, "Unsupported Composio search schema"
        )
    session = properties.get("session")
    if isinstance(session, dict) and session.get("type") == "object":
        session_props = session.get("properties", {})
        generate_id = session_props.get("generate_id") if isinstance(session_props, dict) else None
        if isinstance(generate_id, dict) and generate_id.get("type") == "boolean":
            args["session"] = {"generate_id": True}
    _validate_arguments(schema, args)
    return args


def _manage_arguments(schema: dict[str, Any], toolkit: str) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise OctopError(
            ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE, "Unsupported Composio authorization schema"
        )
    toolkits = properties.get("toolkits")
    if not isinstance(toolkits, dict) or toolkits.get("type") != "array":
        raise OctopError(
            ErrorCode.COMPOSIO_UPSTREAM_UNAVAILABLE, "Unsupported Composio authorization schema"
        )
    args: dict[str, Any] = {"toolkits": [toolkit]}
    reinitiate = properties.get("reinitiate_all")
    if isinstance(reinitiate, dict) and reinitiate.get("type") == "boolean":
        args["reinitiate_all"] = False
    _validate_arguments(schema, args)
    return args


def _authorization_url(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    candidates: list[Any] = []
    structured = raw.get("structuredContent")
    if isinstance(structured, dict):
        candidates.extend(_authorization_candidates(structured))
    content = raw.get("content")
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                candidates.extend(_authorization_candidates(parsed))
    candidates.append(raw.get("redirect_url"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        parsed_url = urlsplit(candidate)
        if parsed_url.scheme == "https" and parsed_url.hostname and not parsed_url.username:
            return candidate
    return None


def _authorization_candidates(data: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = [data.get("redirect_url")]
    payload = data.get("data")
    if isinstance(payload, dict):
        candidates.append(payload.get("redirect_url"))
        results = payload.get("results")
        if isinstance(results, dict):
            for result in results.values():
                if isinstance(result, dict):
                    candidates.append(result.get("redirect_url"))
                    connection = result.get("connection")
                    if isinstance(connection, dict):
                        candidates.append(connection.get("redirect_url"))
    return candidates


def _normalize_search_results(raw: Any) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    if raw is None:
        return apps
    # MCP tool results often wrap content as [{type:text, text:"..."}]
    payload: Any = raw
    if isinstance(raw, dict) and isinstance(raw.get("structuredContent"), (dict, list)):
        payload = raw["structuredContent"]
    if isinstance(raw, dict) and isinstance(raw.get("content"), list):
        texts: list[str] = []
        for item in raw["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        joined = "\n".join(texts).strip()
        if joined and payload is raw:
            try:
                payload = json.loads(joined)
            except json.JSONDecodeError:
                payload = {"raw_text": joined[:4000]}
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        payload = payload["data"]
    candidates: list[Any] = []
    descriptions: dict[str, str] = {}
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        statuses = payload.get("toolkit_connection_statuses")
        if isinstance(statuses, list):
            descriptions = {
                str(item.get("toolkit")): str(item.get("description") or "")[:600]
                for item in statuses
                if isinstance(item, dict) and isinstance(item.get("toolkit"), str)
            }
        results = payload.get("results")
        if isinstance(results, list):
            candidates = [
                toolkit
                for result in results
                if isinstance(result, dict) and isinstance(result.get("toolkits"), list)
                for toolkit in result["toolkits"]
            ]
        if not candidates:
            for key in (
                "toolkit_connection_statuses",
                "apps",
                "toolkits",
                "results",
                "tools",
                "items",
            ):
                if isinstance(payload.get(key), list):
                    candidates = payload[key]
                    break
        if not candidates and payload:
            candidates = [payload]
    expanded: list[Any] = []
    for item in candidates:
        if isinstance(item, str):
            expanded.append({"toolkit": item, "description": descriptions.get(item, "")})
        elif isinstance(item, dict) and isinstance(item.get("toolkits"), list):
            expanded.extend(
                {"toolkit": toolkit, "description": descriptions.get(toolkit, "")}
                if isinstance(toolkit, str)
                else toolkit
                for toolkit in item["toolkits"]
            )
        else:
            expanded.append(item)
    candidates = expanded
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        toolkit = str(
            item.get("toolkit") or item.get("slug") or item.get("toolkit_slug") or ""
        ).strip()
        if not toolkit:
            continue
        if len(toolkit) > 128:
            continue
        if toolkit in seen:
            continue
        seen.add(toolkit)
        apps.append(
            {
                "toolkit": toolkit,
                "name": str(item.get("name") or toolkit)[:160],
                "description": str(item.get("description") or item.get("summary") or "")[:600],
                "source": "composio",
            }
        )
    return apps
