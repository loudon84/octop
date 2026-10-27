"""Composio Connect MCP protocol and capability boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from octop.infra.connectors import composio
from octop.infra.connectors.composio import (
    META_SEARCH,
    ComposioService,
    _manage_arguments,
    _search_arguments,
    _tool_schema,
)
from octop.infra.errors import OctopError


def _tool(name: str, schema: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(name=name, inputSchema=schema)


def test_search_arguments_follow_realistic_tools_list_schema() -> None:
    schema = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"use_case": {"type": "string"}},
                    "required": ["use_case"],
                },
            },
            "session": {
                "type": "object",
                "properties": {"generate_id": {"type": "boolean"}},
                "required": ["generate_id"],
            },
        },
        "required": ["queries", "session"],
    }
    assert _search_arguments(schema, "find gmail tools") == {
        "queries": [{"use_case": "find gmail tools"}],
        "session": {"generate_id": True},
    }


def test_manage_schema_uses_toolkits_not_guessed_action() -> None:
    schema = {
        "type": "object",
        "properties": {
            "toolkits": {"type": "array", "items": {"type": "string"}},
            "reinitiate_all": {"type": "boolean"},
        },
        "required": ["toolkits"],
    }
    assert _manage_arguments(schema, "gmail") == {
        "toolkits": ["gmail"],
        "reinitiate_all": False,
    }


def test_unknown_tools_and_unsupported_schema_fail_clearly() -> None:
    with pytest.raises(OctopError, match="capability"):
        _tool_schema([], META_SEARCH)
    with pytest.raises(OctopError, match="Unsupported"):
        _search_arguments({"type": "object", "properties": {"term": {"type": "string"}}}, "mail")


def test_search_capability_requires_adapter_supported_schema() -> None:
    unsupported = _tool(
        META_SEARCH,
        {
            "type": "object",
            "properties": {"query": {"type": "string"}, "required_flag": {"type": "boolean"}},
            "required": ["query", "required_flag"],
        },
    )
    assert composio._capabilities([unsupported])["search_apps"] is False


def test_search_results_use_structured_content_allowlist() -> None:
    result = composio._normalize_search_results(
        {
            "structuredContent": {
                "data": {
                    "results": [
                        {
                            "toolkits": [
                                {
                                    "toolkit_slug": "gmail",
                                    "name": "Gmail",
                                    "description": "Mail",
                                    "access_token": "must-not-escape",
                                },
                                {"name": "friendly name without a slug", "description": "ignored"},
                            ]
                        }
                    ],
                    "toolkit_connection_statuses": [
                        {"toolkit": "gmail", "has_active_connection": False}
                    ],
                }
            },
            "content": [],
        }
    )
    assert result == [
        {
            "toolkit": "gmail",
            "name": "Gmail",
            "description": "Mail",
            "source": "composio",
        }
    ]
    assert "must-not-escape" not in str(result)


def test_live_connect_text_result_normalizes_string_toolkits_with_safe_description() -> None:
    result = composio._normalize_search_results(
        {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"successful":true,"data":{"results":[{"toolkits":["github"]}],'
                        '"toolkit_connection_statuses":[{"toolkit":"github",'
                        '"description":"GitHub integration","has_active_connection":false,'
                        '"status_message":"not connected","connection_details":{"token":"secret"}}]}}'
                    ),
                }
            ]
        }
    )
    assert result == [
        {
            "toolkit": "github",
            "name": "github",
            "description": "GitHub integration",
            "source": "composio",
        }
    ]
    assert "secret" not in str(result)


def test_authorization_url_requires_https_and_only_returns_url() -> None:
    assert (
        composio._authorization_url(
            {
                "structuredContent": {
                    "data": {
                        "results": {
                            "gmail": {"redirect_url": "https://auth.example/connect?state=abc"}
                        }
                    },
                    "token": "secret",
                }
            }
        )
        == "https://auth.example/connect?state=abc"
    )
    assert (
        composio._authorization_url({"structuredContent": {"redirect_url": "javascript:alert(1)"}})
        is None
    )
    assert (
        composio._authorization_url(
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"successful":true,"data":{"results":{"github":{"redirect_url":"https://auth.example/github?state=abc","access_token":"secret"}}}}',
                    }
                ]
            }
        )
        == "https://auth.example/github?state=abc"
    )


def test_composio_sdk_client_disables_redirects() -> None:
    client = composio._composio_httpx_client_factory(headers={"Authorization": "Bearer secret"})
    try:
        assert client.follow_redirects is False
        assert client.headers["Authorization"] == "Bearer secret"
    finally:
        import asyncio

        asyncio.run(client.aclose())


@pytest.mark.asyncio
async def test_call_tool_uses_sdk_initialized_session_and_validates_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    search_schema = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"use_case": {"type": "string"}},
                },
            },
            "session": {
                "type": "object",
                "properties": {"generate_id": {"type": "boolean"}},
            },
        },
        "required": ["queries", "session"],
    }

    class Session:
        async def initialize(self) -> None:
            calls.append("initialize")

        async def list_tools(self) -> SimpleNamespace:
            calls.append("tools/list")
            return SimpleNamespace(tools=[_tool(META_SEARCH, search_schema)])

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
            calls.append(name)
            assert arguments == {
                "queries": [{"use_case": "mail"}],
                "session": {"generate_id": True},
            }
            return SimpleNamespace(
                isError=False,
                model_dump=lambda **_: {"content": [{"type": "text", "text": "[]"}]},
            )

    @asynccontextmanager
    async def open_session(_: str) -> Any:
        yield Session()

    monkeypatch.setattr(composio, "_open_session", open_session)
    result = await ComposioService.__new__(ComposioService)._mcp_call_tool(
        "token", META_SEARCH, _search_arguments(search_schema, "mail")
    )
    assert result == {"content": [{"type": "text", "text": "[]"}]}
    assert calls == ["initialize", "tools/list", META_SEARCH]


@pytest.mark.asyncio
async def test_upstream_http_error_does_not_expose_body_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        502,
        request=httpx.Request("POST", composio.COMPOSIO_MCP_URL),
        text="Bearer highly-secret upstream diagnostic",
    )

    class Session:
        async def initialize(self) -> None:
            raise httpx.HTTPStatusError(
                "upstream failed", request=response.request, response=response
            )

    @asynccontextmanager
    async def open_session(_: str) -> Any:
        yield Session()

    monkeypatch.setattr(composio, "_open_session", open_session)
    with pytest.raises(OctopError) as exc:
        await ComposioService.__new__(ComposioService)._mcp_call_tool(
            "highly-secret", META_SEARCH, {}
        )
    assert "highly-secret" not in str(exc.value)
    assert "diagnostic" not in str(exc.value)


@pytest.mark.asyncio
async def test_app_status_never_calls_manage_connections() -> None:
    service = ComposioService.__new__(ComposioService)

    async def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("status query must not call upstream auth-capable tool")

    service._require_access_token = forbidden  # type: ignore[method-assign]
    status = await service.check_app_status(1, "gmail")
    assert status["status"] == "unavailable"
    assert status["result"] is None
