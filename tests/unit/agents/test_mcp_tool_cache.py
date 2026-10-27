"""Unit tests for custom MCP deferred load + user-level tool cache."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from octop.infra.connectors.mcp_tool_cache import (
    fingerprint_mcp_spec,
    wrap_tools_for_shared_use,
)
from octop.infra.errors import OctopError


def test_fingerprint_stable_and_ignores_enabled() -> None:
    a = fingerprint_mcp_spec(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "enabled": True,
        }
    )
    b = fingerprint_mcp_spec(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "enabled": False,
        }
    )
    assert a == b
    c = fingerprint_mcp_spec(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "other"],
        }
    )
    assert a != c


@pytest.mark.asyncio
async def test_wrap_tools_serialize_ainvoke() -> None:
    order: list[str] = []

    class _FakeTool:
        name = "demo_tool"
        description = "demo"
        args_schema = None

        async def ainvoke(self, *_a: Any, **_k: Any) -> str:
            order.append("start")
            await asyncio.sleep(0.05)
            order.append("end")
            return "ok"

        def invoke(self, *_a: Any, **_k: Any) -> str:
            return "ok"

    lock = asyncio.Lock()
    wrapped = wrap_tools_for_shared_use([_FakeTool()], lock)
    assert len(wrapped) == 1
    assert isinstance(wrapped[0], BaseTool)
    assert isinstance(wrapped[0], StructuredTool)

    async def _call() -> Any:
        return await wrapped[0].ainvoke({})

    await asyncio.gather(_call(), _call())
    assert order == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_sync_invoke_fails_closed_inside_running_loop() -> None:
    """Sync invoke cannot skip async caller/selection guards inside an event loop."""
    inner = MagicMock()
    inner.name = "demo_tool"
    inner.description = "demo"
    inner.args_schema = None
    inner.metadata = None
    inner.invoke.return_value = "unsafe"
    inner.ainvoke = AsyncMock(return_value="safe")

    async def allowed() -> bool:
        return False

    wrapped = wrap_tools_for_shared_use([inner], asyncio.Lock(), allow_call=allowed)[0]
    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        wrapped.invoke({})
    inner.invoke.assert_not_called()
    inner.ainvoke.assert_not_awaited()


def test_sync_invoke_without_running_loop_applies_live_guard() -> None:
    inner = MagicMock()
    inner.name = "demo_tool"
    inner.description = "demo"
    inner.args_schema = None
    inner.metadata = None
    inner.ainvoke = AsyncMock(return_value="safe")

    async def denied() -> bool:
        return False

    wrapped = wrap_tools_for_shared_use([inner], asyncio.Lock(), allow_call=denied)[0]
    with pytest.raises(PermissionError, match="no longer available"):
        wrapped.invoke({})
    inner.ainvoke.assert_not_awaited()


def test_sync_invoke_without_running_loop_uses_guarded_async_tool() -> None:
    inner = MagicMock()
    inner.name = "demo_tool"
    inner.description = "demo"
    inner.args_schema = None
    inner.metadata = None
    inner.ainvoke = AsyncMock(return_value="safe")

    async def allowed() -> bool:
        return True

    wrapped = wrap_tools_for_shared_use([inner], asyncio.Lock(), allow_call=allowed)[0]
    assert wrapped.invoke({}) == "safe"
    inner.ainvoke.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_cached_tool_reference_stops_after_connector_is_disabled() -> None:
    allowed = True
    inner = MagicMock()
    inner.name = "server_write"
    inner.description = "write"
    inner.args_schema = None
    inner.metadata = None
    inner.ainvoke = AsyncMock(return_value="written")

    async def still_enabled() -> bool:
        return allowed

    old_reference = wrap_tools_for_shared_use([inner], asyncio.Lock(), allow_call=still_enabled)[0]
    assert await old_reference.ainvoke({}) == "written"
    allowed = False
    with pytest.raises(PermissionError):
        await old_reference.ainvoke({})
    inner.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_agent_turn_scope_executes_only_selected_users_tools() -> None:
    """Exercise selected tools while two callers overlap on one shared graph."""
    from pydantic import create_model

    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_execution_locks = {}
    mgr._mcp_base_tools = {}
    mgr._thread_mcp_context = {}
    agent = MagicMock()
    agent._mcp_tools = []

    def replace(tools: list[Any]) -> None:
        agent._mcp_tools = list(tools)

    agent.replace_mcp_tools = replace
    mgr.get_agent = MagicMock(return_value=agent)
    executed: list[int] = []

    async def prepare(_agent_id: str, names: list[str], *, connector_user_id: int) -> list[str]:
        async def invoke() -> int:
            await asyncio.sleep(0.01)
            executed.append(connector_user_id)
            return connector_user_id

        agent.replace_mcp_tools(
            [
                StructuredTool(
                    name=names[0] + "_call",
                    description="caller scoped tool",
                    args_schema=create_model(f"Args{connector_user_id}"),
                    coroutine=invoke,
                    func=lambda: connector_user_id,
                )
            ]
        )
        return []

    mgr.prepare_chat_mcp = prepare

    async def turn(uid: int) -> int:
        request = {
            "thread_id": f"thread-{uid}",
            "connector_user_id": uid,
            "mcp_servers": [f"user-{uid}"],
        }
        async with (
            mgr._mcp_execution_lock("shared-expert"),
            mgr._turn_mcp_tools("shared-expert", request),
        ):
            await asyncio.sleep(0.01)
            return await agent._mcp_tools[0].ainvoke({})

    assert await asyncio.gather(turn(11), turn(22)) == [11, 22]
    assert executed == [11, 22]
    assert agent._mcp_tools == []
    assert await turn(11) == 11
    assert await turn(22) == 22
    assert await turn(11) == 11
    assert executed == [11, 22, 11, 22, 11]


@pytest.mark.asyncio
async def test_resume_without_persisted_caller_context_fails_closed() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_execution_locks = {}
    mgr._mcp_base_tools = {}
    mgr._thread_mcp_context = {}
    baseline_calls: list[str] = []
    tool = StructuredTool.from_function(
        lambda: baseline_calls.append("owner-baseline") or "ran",
        name="owner_secret_tool",
        description="must not be available to an unscoped resume",
    )
    agent = MagicMock()
    agent._mcp_tools = [tool]
    agent.replace_mcp_tools = lambda tools: setattr(agent, "_mcp_tools", list(tools))
    mgr.get_agent = MagicMock(return_value=agent)

    with pytest.raises(OctopError, match="caller and selection are unavailable"):
        async with mgr._turn_mcp_tools(
            "owner-agent", {"thread_id": "restart-thread", "_resume_mcp_context": True}
        ):
            await agent._mcp_tools[0].ainvoke({})

    assert baseline_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_agent", [True, False])
async def test_real_prepare_path_mounts_only_selected_custom_server(
    owner_agent: bool,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    from octop.infra.agents.manager import AgentManager

    user_id = 7
    specs = {
        name: {"transport": "streamable_http", "url": f"https://{name}.example/mcp"}
        for name in ("first", "second")
    }
    manager = object.__new__(AgentManager)
    manager._mcp_tool_cache = {}
    manager._mcp_tool_cache_locks = {}
    manager._mcp_tool_cache_guard = asyncio.Lock()
    manager._mcp_execution_locks = {}
    manager._mcp_base_tools = {}
    manager._thread_mcp_context = {}
    manager._paths = SimpleNamespace(root=Path("."))
    parent = SimpleNamespace(status="active", has_credentials=True, user_id=user_id, shared=False)
    repo = MagicMock()
    repo.list_visible.return_value = []
    repo.get_by_user_kind.return_value = parent
    repo.list_by_kind.return_value = [parent]
    manager._repos = SimpleNamespace(connector_repo=repo)
    manager._connector_svc = MagicMock()
    manager._connector_svc.custom_harness_configs.return_value = specs

    row = SimpleNamespace(user_id=user_id if owner_agent else None, kind="expert")
    manager.get_row = MagicMock(return_value=row)
    manager._connector_uid_for = MagicMock(return_value=user_id)
    agent = MagicMock()
    agent._mcp_tools = []
    agent._mcp_tool_name_set = frozenset()
    agent.config.mcp_server_configs = {}

    def replace(tools: list[Any]) -> None:
        agent._mcp_tools = list(tools)
        agent._mcp_tool_name_set = frozenset(tool.name for tool in tools)

    def append(tools: list[Any]) -> None:
        replace([*tools, *agent._mcp_tools])

    agent.replace_mcp_tools = replace
    agent.append_mcp_tools = append
    manager.get_agent = MagicMock(return_value=agent)

    async def load(spec_map: dict[str, Any]) -> list[Any]:
        server = next(iter(spec_map))
        tool = MagicMock()
        tool.name = f"{server}_read"
        tool.description = "read"
        tool.args_schema = None
        tool.metadata = None
        tool.ainvoke = AsyncMock(return_value=server)
        return [tool]

    with patch("octop_harness.mcp.aload_mcp_tools", side_effect=load):
        async with manager._mcp_execution_lock("shared"):
            old_reference = None
            async with manager._turn_mcp_tools(
                "shared",
                {"thread_id": "selected", "connector_user_id": user_id, "mcp_servers": ["first"]},
            ):
                assert [tool.name for tool in agent._mcp_tools] == ["first_read"]
                assert await agent._mcp_tools[0].ainvoke({}) == "first"
                old_reference = agent._mcp_tools[0]
            assert agent._mcp_tools == []

            async with manager._turn_mcp_tools(
                "shared",
                {"thread_id": "empty", "connector_user_id": user_id, "mcp_servers": []},
            ):
                assert agent._mcp_tools == []
                assert old_reference is not None
                with pytest.raises(PermissionError):
                    await old_reference.ainvoke({})

    assert old_reference is not None
    from octop.infra.agents import manager as manager_module

    caller = manager_module._connector_user_id_var.set(user_id)
    selected = manager_module._connector_selected_servers.set(frozenset({"first"}))
    try:
        manager._connector_svc.custom_harness_configs.return_value = {}
        with pytest.raises(PermissionError):
            await old_reference.ainvoke({})
        manager._connector_svc.custom_harness_configs.return_value = {
            **specs,
            "first": {**specs["first"], "url": "https://rotated.example/mcp"},
        }
        with pytest.raises(PermissionError):
            await old_reference.ainvoke({})
    finally:
        manager_module._connector_selected_servers.reset(selected)
        manager_module._connector_user_id_var.reset(caller)


@pytest.mark.asyncio
async def test_gateway_tool_reference_is_revoked_on_credential_rotation() -> None:
    from types import SimpleNamespace

    from octop.infra.agents import manager as manager_module
    from octop.infra.agents.manager import AgentManager

    credentials = {"api_key": "first-generation"}
    row = SimpleNamespace(
        instance_id="connector-1",
        user_id=7,
        shared=False,
        status="active",
        has_credentials=True,
        mcp_server_name="gateway__connector-1",
        config_json="{}",
    )
    repo = MagicMock()
    repo.list_visible.return_value = [row]
    service = MagicMock()
    service.decrypt.side_effect = lambda _instance_id: dict(credentials)
    service.ensure_fresh_credentials = AsyncMock(return_value=credentials)

    manager = object.__new__(AgentManager)
    manager._repos = SimpleNamespace(connector_repo=repo)
    manager._connector_svc = service
    manager._agently_write_interrupts = MagicMock(return_value={})
    agent = MagicMock()
    agent._mcp_tools = []
    agent.config.mcp_server_configs = {}
    agent.replace_mcp_tools = lambda tools: setattr(agent, "_mcp_tools", list(tools))

    def inject(target: Any, **_kwargs: Any) -> None:
        target._mcp_tools = [
            StructuredTool.from_function(
                lambda: "sent",
                name="gateway__connector-1_send",
                description="send",
            )
        ]

    with patch.object(manager_module, "inject_missing_gateway_tools", side_effect=inject):
        await manager._attach_gateway_tools(
            agent,
            agent_id="shared-agent",
            user_id=7,
            server_names=["gateway__connector-1"],
        )

    reference = agent._mcp_tools[0]
    caller = manager_module._connector_user_id_var.set(7)
    selected = manager_module._connector_selected_servers.set(frozenset({"gateway__connector-1"}))
    try:
        assert await reference.ainvoke({}) == "sent"
        credentials["api_key"] = "rotated-generation"
        with pytest.raises(PermissionError):
            await reference.ainvoke({})
    finally:
        manager_module._connector_selected_servers.reset(selected)
        manager_module._connector_user_id_var.reset(caller)


@pytest.mark.asyncio
async def test_get_or_load_caches_and_reuses() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())

    fake = MagicMock()
    fake.name = "s_tool1"
    fake.description = "t"
    fake.args_schema = None
    fake.metadata = None
    fake.ainvoke = AsyncMock(return_value="ok")
    aload = AsyncMock(return_value=[fake])

    spec = {"transport": "stdio", "command": "npx", "args": []}
    with patch("octop_harness.mcp.aload_mcp_tools", aload):
        first = await mgr._get_or_load_mcp_tools(1, "s", spec)
        second = await mgr._get_or_load_mcp_tools(1, "s", spec)

    assert aload.await_count == 1
    assert first is second
    assert isinstance(first[0], StructuredTool)
    assert first[0].name == "s_tool1"


@pytest.mark.asyncio
async def test_get_or_load_misses_on_fingerprint_change() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())

    def _tool(name: str) -> MagicMock:
        t = MagicMock()
        t.name = name
        t.description = "t"
        t.args_schema = None
        t.metadata = None
        t.ainvoke = AsyncMock(return_value="ok")
        return t

    aload = AsyncMock(side_effect=[[_tool("s_a")], [_tool("s_b")]])

    with patch("octop_harness.mcp.aload_mcp_tools", aload):
        a = await mgr._get_or_load_mcp_tools(
            1, "s", {"transport": "stdio", "command": "npx", "args": ["a"]}
        )
        b = await mgr._get_or_load_mcp_tools(
            1, "s", {"transport": "stdio", "command": "npx", "args": ["b"]}
        )

    assert aload.await_count == 2
    assert a[0].name == "s_a"
    assert b[0].name == "s_b"
    assert len(mgr._mcp_tool_cache) == 1


@pytest.mark.asyncio
async def test_prepare_chat_mcp_injects_custom_from_cache() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())

    tool = MagicMock()
    tool.name = "deepwiki_ask"
    tool.description = "ask"
    tool.args_schema = None
    tool.metadata = None
    tool.ainvoke = AsyncMock(return_value="ok")
    aload = AsyncMock(return_value=[tool])

    agent = MagicMock()
    agent._mcp_tool_name_set = frozenset()
    agent.config.mcp_server_configs = {"deepwiki": {}}
    agent.append_mcp_tools = MagicMock(
        side_effect=lambda tools: setattr(
            agent,
            "_mcp_tool_name_set",
            frozenset(getattr(t, "name", "") for t in tools) | frozenset(agent._mcp_tool_name_set),
        )
    )

    row = MagicMock()
    mgr.get_agent = MagicMock(return_value=agent)
    mgr.get_row = MagicMock(return_value=row)
    mgr._connector_uid_for = MagicMock(return_value=7)
    mgr._connector_svc = MagicMock()
    mgr._connector_svc.custom_harness_configs.return_value = {
        "deepwiki": {
            "transport": "streamable_http",
            "url": "https://mcp.deepwiki.com/mcp",
            "headers": {"Accept": "application/json, text/event-stream"},
        }
    }
    mgr.reload_connectors = AsyncMock()

    with patch("octop_harness.mcp.aload_mcp_tools", aload):
        failed = await mgr.prepare_chat_mcp("A1", ["deepwiki"], connector_user_id=7)
        failed2 = await mgr.prepare_chat_mcp("A1", ["deepwiki"], connector_user_id=7)

    assert failed == []
    assert failed2 == []
    assert aload.await_count == 1
    assert agent.append_mcp_tools.call_count == 1
    mgr.reload_connectors.assert_not_awaited()
    assert agent.config.mcp_server_configs["deepwiki"]["transport"] == "streamable_http"
    injected = agent.append_mcp_tools.call_args.args[0]
    assert isinstance(injected[0], StructuredTool)


@pytest.mark.asyncio
async def test_prepare_chat_mcp_attaches_gateway_tools_without_reload() -> None:
    """Gateway connectors inject in-process; rebuilding would drop the runtime.

    A rebuild ends in the very same injection, but first closes the harness
    agent (and the checkpointer pool an in-flight turn still writes to) and
    holds ``AgentManager._lock`` across workspace I/O.
    """
    from octop.infra.agents.manager import AgentManager
    from octop.infra.connectors.builder import mcp_server_name

    instance_id = "01TESTGATEWAYINSTANCE0001"
    server = mcp_server_name("qq-mail", instance_id)

    inst = MagicMock()
    inst.instance_id = instance_id
    inst.kind = "qq-mail"
    inst.status = "active"
    inst.mcp_server_name = server

    agent = MagicMock()
    agent._mcp_tools = []
    agent._mcp_tool_name_set = frozenset()
    agent.config.mcp_server_configs = {}

    def _inject(tools: list[Any]) -> None:
        agent._mcp_tools = [*tools, *agent._mcp_tools]
        agent._mcp_tool_name_set = frozenset(str(getattr(t, "name", "")) for t in agent._mcp_tools)

    agent.inject_mcp_tools = MagicMock(side_effect=_inject)

    mgr = object.__new__(AgentManager)
    mgr._repos = MagicMock()
    mgr._repos.connector_repo.list_visible.return_value = [inst]
    mgr._connector_svc = MagicMock()
    mgr._connector_svc.custom_harness_configs.return_value = {}
    mgr._connector_svc.decrypt.return_value = {"email": "a@qq.com", "password": "code"}
    mgr._connector_svc.ensure_fresh_credentials = AsyncMock(return_value={})
    mgr.get_agent = MagicMock(return_value=agent)
    mgr.get_row = MagicMock(return_value=MagicMock())
    mgr._connector_uid_for = MagicMock(return_value=7)
    mgr.reload_connectors = AsyncMock()

    failed = await mgr.prepare_chat_mcp("A1", [server], connector_user_id=7)

    # Asserted before the tool checks so a reintroduced rebuild reports itself
    # rather than surfacing as "tools missing".
    mgr.reload_connectors.assert_not_awaited()
    assert failed == []
    assert agent.inject_mcp_tools.call_count == 1
    assert all(name.startswith(f"{server}_") for name in agent._mcp_tool_name_set)
    assert server in agent.config.mcp_server_configs
    mgr._connector_svc.ensure_fresh_credentials.assert_awaited_once_with(instance_id, "qq-mail")


def test_wrap_tools_for_shared_use() -> None:
    lock = asyncio.Lock()
    inner = MagicMock()
    inner.name = "x"
    inner.description = "d"
    inner.args_schema = None
    inner.metadata = None
    wrapped = wrap_tools_for_shared_use([inner], lock)
    assert len(wrapped) == 1
    assert isinstance(wrapped[0], StructuredTool)
