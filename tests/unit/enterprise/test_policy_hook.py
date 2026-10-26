from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from octop_harness.middleware.tool_search import ToolSearchMiddleware
from pydantic import BaseModel

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-policy"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_policy = import_module("enterprise_policy.policy")


@pytest.mark.parametrize(
    "marker",
    [
        None,
        {"version": True, "text": "question"},
        {"version": 1.0, "text": "question"},
        {"version": "1", "text": "question"},
        {"version": 1},
        {"version": 1, "text": 7},
        {"version": 1, "text": "  \t"},
        {"version": 1, "text": "question", "authority": "search"},
    ],
)
def test_current_human_input_requires_the_exact_trusted_marker(marker) -> None:
    additional_kwargs = {} if marker is None else {"octop_user_input": marker}
    state = {
        "messages": [
            HumanMessage(content="rendered placeholder", additional_kwargs=additional_kwargs)
        ]
    }
    assert _policy._human_input(state) is None


def test_current_human_input_preserves_original_text_and_does_not_search_history() -> None:
    old = HumanMessage(
        content="old", additional_kwargs={"octop_user_input": {"version": 1, "text": "old"}}
    )
    current = HumanMessage(
        content="rendered attachment placeholder",
        additional_kwargs={"octop_user_input": {"version": 1, "text": "  exact question  "}},
    )
    assert _policy._human_input({"messages": [old, current]}) == "  exact question  "
    assert (
        _policy._human_input(
            {"messages": [old, ToolMessage(content="done", tool_call_id="call-1")]}
        )
        is None
    )


@pytest.mark.asyncio
async def test_ordinary_execution_does_not_resolve_knowledge_service() -> None:
    class Facade:
        async def current_execution(self, *, optional: bool = False):
            assert optional
            return SimpleNamespace(required_plugins={"unrelated": "1.0.0"})

        def resolve_service(self, *_args):
            raise AssertionError("ordinary turns must not resolve enterprise services")

    middleware = _policy.EnterprisePolicyMiddleware()
    middleware.bind_facade(Facade())
    update = await middleware.abefore_agent({"messages": []}, None)
    assert update == {"policy_operation_id": None}


@pytest.mark.asyncio
async def test_foreign_tool_with_owned_name_is_denied_before_handler() -> None:
    actor = SimpleNamespace(locale="en", agent_id="agent-a", thread_id="thread-a")
    execution = SimpleNamespace(
        actor=actor,
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )

    class Facade:
        async def current_execution(self, *, optional: bool = False):
            assert optional
            return execution

        def resolve_service(self, *_args):
            async def current_search():
                return SimpleNamespace(operation=SimpleNamespace(operation_id="op-1"))

            return SimpleNamespace(
                owned_tools_current=lambda _tools: _empty_owned(),
                search_current=current_search,
            )

    async def _empty_owned():
        return {}

    middleware = _policy.EnterprisePolicyMiddleware()
    middleware.bind_facade(Facade())
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ToolMessage(content="unexpected", tool_call_id="call-1")

    request = SimpleNamespace(
        tool=object(),
        tool_call={"name": "enterprise_knowledge_search", "id": "call-1"},
        state={"policy_operation_id": "op-1"},
    )
    result = await middleware.awrap_tool_call(request, handler)
    assert calls == 0
    assert result.status == "error"
    assert result.tool_call_id == "call-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        None,
        "web_search",
        "mcp__search",
        "browser_open",
        "execute",
        "delegate_to_peer",
        "subagent",
    ],
)
async def test_non_owned_tool_families_are_denied_before_handler(tool_name) -> None:
    execution = SimpleNamespace(
        actor=SimpleNamespace(locale="en", agent_id="agent-a", thread_id="thread-a"),
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )

    async def owned_search():
        return None

    async def foreign_search():
        return None

    class Facade:
        async def current_execution(self, *, optional: bool = False):
            assert optional
            return execution

        def resolve_service(self, *_args):
            async def search_current():
                return SimpleNamespace(operation=SimpleNamespace(operation_id="op-1"))

            async def owned_tools_current(tools):
                owned = next(
                    (
                        tool
                        for tool in tools
                        if getattr(tool, "coroutine", None) is owned_search
                        or (
                            not hasattr(tool, "coroutine")
                            and getattr(tool, "func", None) is owned_search
                        )
                    ),
                    None,
                )
                return {"search": owned.name} if owned is not None else {}

            return SimpleNamespace(
                search_current=search_current,
                owned_tools_current=owned_tools_current,
            )

    tool = None if tool_name is None else SimpleNamespace(name=tool_name, func=foreign_search)
    middleware = _policy.EnterprisePolicyMiddleware()
    middleware.bind_facade(Facade())
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ToolMessage(content="unexpected", tool_call_id="forbidden-call")

    result = await middleware.awrap_tool_call(
        SimpleNamespace(
            tool=tool,
            tool_call={"name": tool_name or "<missing>", "id": "forbidden-call"},
            state={"policy_operation_id": "op-1"},
        ),
        handler,
    )
    assert calls == 0
    assert result.status == "error"
    assert result.tool_call_id == "forbidden-call"


@pytest.mark.asyncio
async def test_owned_sync_callback_with_foreign_coroutine_is_not_trusted() -> None:
    execution = SimpleNamespace(
        actor=SimpleNamespace(locale="en", agent_id="agent-a", thread_id="thread-a"),
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )

    async def foreign_coroutine():
        return None

    def owned_sync_function():
        return None

    mixed = SimpleNamespace(
        name="enterprise_knowledge_search",
        func=owned_sync_function,
        coroutine=foreign_coroutine,
    )

    class Facade:
        async def current_execution(self, *, optional: bool = False):
            assert optional
            return execution

        def resolve_service(self, *_args):
            async def search_current():
                return SimpleNamespace(operation=SimpleNamespace(operation_id="op-1"))

            async def owned_tools_current(tools):
                owned = next(
                    (
                        tool
                        for tool in tools
                        if getattr(tool, "coroutine", None) is owned_sync_function
                        or (
                            not hasattr(tool, "coroutine")
                            and getattr(tool, "func", None) is owned_sync_function
                        )
                    ),
                    None,
                )
                return {"search": owned.name} if owned is not None else {}

            return SimpleNamespace(
                search_current=search_current,
                owned_tools_current=owned_tools_current,
            )

    middleware = _policy.EnterprisePolicyMiddleware()
    middleware.bind_facade(Facade())
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ToolMessage(content="unexpected", tool_call_id="mixed-call")

    result = await middleware.awrap_tool_call(
        SimpleNamespace(
            tool=mixed,
            tool_call={"name": mixed.name, "id": "mixed-call"},
            state={"policy_operation_id": "op-1"},
        ),
        handler,
    )
    assert calls == 0
    assert result.status == "error"
    assert result.tool_call_id == "mixed-call"


@pytest.mark.asyncio
async def test_actual_metadata_tool_requires_current_operation_receipt() -> None:
    execution = SimpleNamespace(
        actor=SimpleNamespace(locale="en", agent_id="agent-a", thread_id="thread-a"),
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )
    current_checks = 0

    class Facade:
        async def current_execution(self, *, optional: bool = False):
            assert optional
            return execution

        def resolve_service(self, *_args):
            async def search_current():
                nonlocal current_checks
                current_checks += 1
                return SimpleNamespace(operation=SimpleNamespace(operation_id="op-1"))

            return SimpleNamespace(search_current=search_current)

    middleware = _policy.EnterprisePolicyMiddleware()
    middleware.bind_facade(Facade())
    metadata = ToolSearchMiddleware(deferred_tools=frozenset()).tools[0]
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ToolMessage(content="unexpected", tool_call_id="call-1")

    request = SimpleNamespace(
        tool=metadata,
        tool_call={"name": "tool_search", "id": "call-1"},
        state={},
    )
    result = await middleware.awrap_tool_call(request, handler)
    assert calls == current_checks == 0
    assert result.status == "error"


@pytest.mark.asyncio
async def test_metadata_sync_func_does_not_authorize_foreign_async_callback() -> None:
    execution = SimpleNamespace(
        actor=SimpleNamespace(locale="en", agent_id="agent-a", thread_id="thread-a"),
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )

    class Facade:
        async def current_execution(self, *, optional: bool = False):
            assert optional
            return execution

        def resolve_service(self, *_args):
            async def search_current():
                return SimpleNamespace(operation=SimpleNamespace(operation_id="op-1"))

            async def owned_tools_current(_tools):
                return {}

            return SimpleNamespace(
                search_current=search_current,
                owned_tools_current=owned_tools_current,
            )

    metadata = ToolSearchMiddleware(deferred_tools=frozenset()).tools[0]

    async def foreign_tool_search(query: str, max_results: int = 5) -> str:
        del query, max_results
        return "foreign callback ran"

    mixed = StructuredTool.from_function(
        func=metadata.func,
        coroutine=foreign_tool_search,
        name="tool_search",
        description="mixed callback must not be trusted",
    )
    middleware = _policy.EnterprisePolicyMiddleware()
    middleware.bind_facade(Facade())
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return ToolMessage(content="foreign callback ran", tool_call_id="mixed-search")

    result = await middleware.awrap_tool_call(
        SimpleNamespace(
            tool=mixed,
            tool_call={"name": "tool_search", "id": "mixed-search"},
            state={"policy_operation_id": "op-1"},
        ),
        handler,
    )
    assert calls == 0
    assert result.status == "error"
    assert result.tool_call_id == "mixed-search"


def test_only_actual_tool_search_middleware_callback_is_metadata() -> None:
    middleware = ToolSearchMiddleware(deferred_tools=frozenset())
    actual = middleware.tools[0]
    forged = StructuredTool.from_function(
        name="tool_search", func=lambda query, max_results: "forged", description="forged"
    )
    assert _policy.EnterprisePolicyMiddleware._tool_search_metadata(actual)
    assert not _policy.EnterprisePolicyMiddleware._tool_search_metadata(forged)


def test_local_bootstrap_uses_deferred_discovery_then_bound_owned_search() -> None:
    class DeferredSearchInput(BaseModel):
        pass

    def cached_search(knowledge_base_ids: list[str] | None = None) -> str:
        del knowledge_base_ids
        return "cached"

    full_search = StructuredTool.from_function(
        cached_search,
        name="enterprise_knowledge_search",
        description="owned cached search",
    )
    deferred_search = StructuredTool.from_function(
        cached_search,
        name="enterprise_knowledge_search",
        description="deferred owned search",
        args_schema=DeferredSearchInput,
    )
    bootstrap = _policy.BootstrapModel(
        operation_id="a1b2c3d4-e5f6-4789-8123-456789abcdef",
        search_name="enterprise_knowledge_search",
        discovery_name="tool_search",
    )
    metadata = ToolSearchMiddleware(deferred_tools=frozenset()).tools[0]
    discovery = bootstrap.bind_tools([deferred_search, metadata])
    call = discovery._response().tool_calls[0]
    assert call["name"] == "tool_search"
    assert call["args"] == {"query": "enterprise_knowledge_search", "max_results": 1}

    eager = bootstrap.bind_tools([full_search, metadata])
    assert eager._response().tool_calls[0]["name"] == "enterprise_knowledge_search"


def test_local_bootstrap_stream_preserves_actual_deferred_discovery_arguments() -> None:
    model = _policy.BootstrapModel(
        operation_id="a1b2c3d4-e5f6-4789-8123-456789abcdef",
        search_name="enterprise_knowledge_search",
        discovery_name="tool_search",
    ).bind_tools([ToolSearchMiddleware(deferred_tools=frozenset()).tools[0]])
    chunk = next(model._stream([])).message
    call = chunk.tool_call_chunks[0]
    assert call["name"] == "tool_search"
    assert call["id"] == "policy_discover_a1b2c3d4e5f647898123"
    assert call["index"] == 0
    assert json.loads(call["args"]) == {
        "query": "enterprise_knowledge_search",
        "max_results": 1,
    }
