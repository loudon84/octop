from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory
from octop_harness.manager import HarnessAgentManager
from octop_harness.plugins import (
    LoadedPlugin,
    PluginManifest,
    PluginRegistry,
    build_plugin_tools,
)
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.registry import MiddlewareRegistration, ToolRegistration
from octop_harness.registry import AgentEntry

from octop.infra.agents.manager import AgentManager
from octop.infra.agents.plugins.host import PluginHost, register_host_setup
from octop.infra.agents.plugins.host_contracts import PluginHostError
from octop.infra.agents.plugins.manager import registry_state_guard
from octop.infra.agents.plugins.plugin_tool_names import sanitize_plugin_tool_names


class RequiredHook(AgentMiddleware):
    pass


def _compiled_agent_entry(
    agent_id: str,
    config: HarnessAgentConfig,
    workspace: Path,
    *,
    model: FakeListChatModel | FakeMessagesListChatModel | None = None,
) -> AgentEntry:
    providers = [
        ProviderConfig(
            id="test-provider",
            base_url="http://localhost",
            api_key="unused",
            models=[ModelConfig(id="test-model")],
        )
    ]
    config = replace(
        config,
        workspace_dir=workspace,
        backend={"type": "local_shell", "root_dir": str(workspace.resolve()), "virtual_mode": True},
        providers=providers,
        default_model="test-provider/test-model",
        memory=(),
        todos_enabled=False,
        model_retry_enabled=False,
        memory_enabled=False,
        session_log_enabled=False,
        checkpointer=InMemorySaver(),
    )

    factory = ChatModelFactory(providers, agent_config=config)

    class NoNetworkModel(FakeListChatModel):
        def bind_tools(self, _tools: object, **_kwargs: object) -> NoNetworkModel:
            return self

    factory.get_chat_model = lambda _ref: model or NoNetworkModel(responses=[])
    agent = HarnessAgent(config, model_factory=factory, agent_id=agent_id)
    agent._init_graph()
    assert agent._graph is not None
    return AgentEntry(
        agent_id=agent_id,
        agent=agent,
        config=config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )


def _real_sdk_manager(workspace: Path) -> tuple[HarnessAgentManager, HarnessAgentConfig]:
    providers = [
        ProviderConfig(
            id="test-provider",
            base_url="http://localhost",
            api_key="unused",
            models=[ModelConfig(id="test-model")],
        )
    ]
    manager = HarnessAgentManager(providers=providers, file_logging=False)
    factory = manager._shared_factory
    assert factory is not None

    class NoNetworkModel(FakeListChatModel):
        def bind_tools(self, _tools: object, **_kwargs: object) -> NoNetworkModel:
            return self

    factory.get_chat_model = lambda _ref: NoNetworkModel(responses=[])
    config = replace(
        HarnessAgentConfig(),
        workspace_dir=workspace,
        providers=providers,
        default_model="test-provider/test-model",
        memory=(),
        todos_enabled=False,
        model_retry_enabled=False,
        memory_enabled=False,
        session_log_enabled=False,
        checkpointer=InMemorySaver(),
    )
    return manager, config


def _loaded_plugin(*, context: object, hook: object) -> LoadedPlugin:
    return LoadedPlugin(
        manifest=PluginManifest(
            id="required-plugin",
            version="1.2.3",
            name="Required plugin",
            kind="hook",
            entry="plugin.py",
        ),
        source_path=Path("plugin"),
        middleware=[MiddlewareRegistration("required-plugin", hook)],
        context=context,
    )


@pytest.fixture(autouse=True)
def _clean_plugin_registry():
    registry = PluginRegistry()
    registry.clear()
    yield
    registry.clear()


def _manager(entry: AgentEntry) -> AgentManager:
    manager = object.__new__(AgentManager)
    manager._harness_manager = SimpleNamespace(get_agent=lambda _agent_id: entry)
    manager._plugin_mount_generations = {}
    manager._plugin_mount_snapshots = {}
    manager._plugin_tool_origins = {}
    manager._plugin_config_tokens = {entry.agent_id: 0}
    manager._plugin_config_writes = {}
    manager._plugin_entry_config_tokens = {entry.agent_id: (entry, 0)}
    return manager


def test_snapshot_is_derived_from_actual_compiled_sdk_agent_entry_and_middleware(
    tmp_path: Path,
) -> None:
    context = object()
    hook = RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    config = HarnessAgentConfig(middleware=PluginRegistry().build_middleware_chain())
    entry = _compiled_agent_entry("agent", config, tmp_path)
    try:
        manager = _manager(entry)

        candidate = manager._capture_plugin_mount_candidate("agent", entry.config)
        manager._publish_plugin_mount_snapshot("agent", entry, candidate)

        snapshot = manager._plugin_mount_snapshot("agent")
        assert snapshot.entry is entry
        assert snapshot.mounted_hooks["required-plugin"] == (hook,)
        assert snapshot.contexts["required-plugin"] is context
        with registry_state_guard() as state:
            assert snapshot.registry_epoch == state.epoch
    finally:
        entry.agent.close()


def test_read_only_alias_maps_to_its_actual_sanitized_sdk_tool_name(tmp_path: Path) -> None:
    manager, entry, _host = _mounted_reader(tmp_path)
    try:
        snapshot = manager._plugin_mount_snapshot("agent")
        mounted = next(item for item in snapshot.registrations if item.plugin_id == "reader")
        assert mounted.read_only_tools == ("read_tool_2",)
        assert mounted.tools[0][0].name == "read_tool"
        assert mounted.tools[1][0].name == "read_tool_2"
    finally:
        entry.agent.close()


def _mounted_reader(
    tmp_path: Path,
    *,
    disabled_plugin: bool = False,
    disabled_tool: bool = False,
) -> tuple[AgentManager, AgentEntry, PluginHost]:
    ctx = PluginContext(
        manifest=PluginManifest(
            id="reader",
            version="1.0.0",
            name="Reader",
            kind="tool",
            entry="plugin.py",
        ),
        source_path=Path("plugin"),
    )

    def read() -> str:
        return "read"

    ctx.tool("read_tool", read)
    ctx.tool("read tool", read)
    register_host_setup(ctx, lambda facade: facade.register_read_only_tools(["read tool"]))
    host = PluginHost(lambda: None)
    host.bind_context(ctx, "1.0.0")
    loaded = ctx.to_loaded()
    loaded.context = ctx
    PluginRegistry().register(loaded)

    agent_plugins = (
        {"reader": {"tools": {"read tool": {"enabled": False}}}} if disabled_tool else {}
    )
    global_plugins = {"reader": not disabled_plugin}
    plugin_tools = build_plugin_tools(
        agent_plugins=agent_plugins,
        global_plugins=global_plugins,
    )
    entry = _compiled_agent_entry("agent", HarnessAgentConfig(tools=plugin_tools), tmp_path)
    manager = _manager(entry)
    manager._plugin_host = host
    manager._record_plugin_tool_origins(
        "agent",
        plugin_tools,
        agent_plugins=agent_plugins,
        global_plugins=global_plugins,
    )
    sanitize_plugin_tool_names(plugin_tools)
    expected_names = (
        []
        if disabled_plugin
        else ["read_tool"]
        if disabled_tool
        else [
            "read_tool",
            "read_tool_2",
        ]
    )
    assert [tool.name for tool in plugin_tools] == expected_names

    candidate = manager._capture_plugin_mount_candidate("agent", entry.config)
    assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)
    return manager, entry, host


def test_disabled_plugin_tool_cannot_contribute_a_read_only_name(tmp_path: Path) -> None:
    manager, entry, _host = _mounted_reader(tmp_path, disabled_tool=True)
    try:
        mounted = next(
            item
            for item in manager._plugin_mount_snapshot("agent").registrations
            if item.plugin_id == "reader"
        )
        assert mounted.read_only_tools == ()
        assert {tool.name for tool, _function in mounted.tools} == {"read_tool"}
    finally:
        entry.agent.close()


def test_agent_disabled_plugin_is_absent_from_read_only_snapshot(tmp_path: Path) -> None:
    manager, entry, _host = _mounted_reader(tmp_path, disabled_plugin=True)
    try:
        assert manager._plugin_mount_snapshot("agent").registrations == ()
    finally:
        entry.agent.close()


@pytest.mark.parametrize(
    "disable_a_globally", [False, True], ids=["agent-tool-disabled", "effective-global-disabled"]
)
@pytest.mark.parametrize("b_declares", [False, True], ids=["b-not-declared", "b-declared"])
async def test_disabled_same_name_shared_function_registration_cannot_donate_readonly_or_lease(
    tmp_path: Path,
    disable_a_globally: bool,
    b_declares: bool,
) -> None:
    shared = lambda: "shared"  # noqa: E731
    plugin_a = PluginContext(
        manifest=PluginManifest(
            id="plugin-a",
            version="1.0.0",
            name="Plugin A",
            kind="tool",
            entry="plugin.py",
        ),
        source_path=Path("plugin-a"),
    )
    plugin_b = PluginContext(
        manifest=PluginManifest(
            id="plugin-b",
            version="1.0.0",
            name="Plugin B",
            kind="tool",
            entry="plugin.py",
        ),
        source_path=Path("plugin-b"),
    )
    plugin_a.tool("shared", shared)
    plugin_b.tool("shared", shared)
    register_host_setup(plugin_a, lambda facade: facade.register_read_only_tools(["shared"]))
    if b_declares:
        register_host_setup(plugin_b, lambda facade: facade.register_read_only_tools(["shared"]))
    host = PluginHost(lambda: None)
    for context in (plugin_a, plugin_b):
        host.bind_context(context, "1.0.0")
        loaded = context.to_loaded()
        loaded.context = context
        PluginRegistry().register(loaded)

    agent_plugins = (
        {} if disable_a_globally else {"plugin-a": {"tools": {"shared": {"enabled": False}}}}
    )
    global_plugins = {"plugin-a": not disable_a_globally, "plugin-b": True}
    tools = build_plugin_tools(agent_plugins=agent_plugins, global_plugins=global_plugins)
    assert len(tools) == 1
    manager = object.__new__(AgentManager)
    manager._harness_manager = SimpleNamespace()
    manager._plugin_mount_generations = {}
    manager._plugin_mount_snapshots = {}
    manager._plugin_tool_origins = {}
    manager._plugin_config_tokens = {"agent": 0}
    manager._plugin_config_writes = {}
    manager._plugin_entry_config_tokens = {}
    manager._plugin_host = host
    manager._record_plugin_tool_origins(
        "agent",
        tools,
        agent_plugins=agent_plugins,
        global_plugins=global_plugins,
    )
    sanitize_plugin_tool_names(tools)
    entry = _compiled_agent_entry("agent", HarnessAgentConfig(tools=tools), tmp_path)
    manager._harness_manager.get_agent = lambda _agent_id: entry
    manager._plugin_entry_config_tokens["agent"] = (entry, 0)
    try:
        candidate = manager._capture_plugin_mount_candidate("agent", entry.config)
        assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)
        snapshot = manager._plugin_mount_snapshot("agent")
        readonly = {
            item.plugin_id: item.read_only_tools
            for item in snapshot.registrations
            if item.read_only_tools
        }
        if b_declares:
            assert readonly == {"plugin-b": ("shared",)}
        else:
            assert readonly == {}

        host.authorize_execution = _empty_authorization  # type: ignore[method-assign]
        lease = await manager._begin_agent_execution(
            None,
            agent_id="agent",
            thread_id="thread",
            execution_kind=None,
            request={"configurable": {}},
        )
        if b_declares:
            assert lease[1] == (host._plugins["plugin-b"],)
            assert lease[0] in host._plugins["plugin-b"].tasks
            assert lease[0] not in host._plugins["plugin-a"].tasks
        else:
            assert lease[1] == ()
        host.end_execution(lease)
    finally:
        entry.agent.close()


async def test_final_admission_replaces_forged_names_and_tracks_capability_only_provider(
    tmp_path: Path,
) -> None:
    manager, entry, host = _mounted_reader(tmp_path)
    host.authorize_execution = _empty_authorization  # type: ignore[method-assign]
    request: dict[str, object] = {
        "configurable": {"conversation_mode_extra_read_tools": ["forged_write_tool"]}
    }
    try:
        lease = await manager._begin_agent_execution(
            None,
            agent_id="agent",
            thread_id="thread",
            execution_kind=None,
            request=request,
        )
        assert request["configurable"]["conversation_mode_extra_read_tools"] == [  # type: ignore[index]
            "read_tool_2",
            "search_knowledge",
            "tool_search",
        ]
        state = host._plugins["reader"]
        assert lease[1] == (state,)
        assert lease[0] in state.tasks
        host.end_execution(lease)
    finally:
        entry.agent.close()


async def test_capability_lease_is_cancelled_when_provider_generation_is_replaced(
    tmp_path: Path,
) -> None:
    manager, entry, host = _mounted_reader(tmp_path)
    host.authorize_execution = _empty_authorization  # type: ignore[method-assign]
    admitted = asyncio.Event()

    async def invoke() -> None:
        lease = await manager._begin_agent_execution(
            None,
            agent_id="agent",
            thread_id="thread",
            execution_kind=None,
            request={"configurable": {}},
        )
        try:
            admitted.set()
            await asyncio.Event().wait()
        finally:
            host.end_execution(lease)

    task = asyncio.create_task(invoke())
    try:
        await admitted.wait()
        host.bind("reader", "1.0.0")
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        entry.agent.close()


async def test_paused_authorization_rejects_a_rebuilt_read_only_snapshot(
    tmp_path: Path,
) -> None:
    manager, entry, host = _mounted_reader(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def paused_authorization(*_args: object, **_kwargs: object) -> dict[str, str]:
        started.set()
        await release.wait()
        return {}

    host.authorize_execution = paused_authorization  # type: ignore[method-assign]
    request: dict[str, object] = {"configurable": {}}
    task = asyncio.create_task(
        manager._begin_agent_execution(
            None,
            agent_id="agent",
            thread_id="thread",
            execution_kind=None,
            request=request,
        )
    )
    try:
        await started.wait()
        manager._plugin_mount_generations["agent"] = 1
        release.set()
        with pytest.raises(PluginHostError):
            await task
        assert request["configurable"] == {}
    finally:
        release.set()
        if not task.done():
            task.cancel()
        entry.agent.close()


@pytest.mark.parametrize("mode", ["ask", "plan", "craft"])
async def test_native_mode_uses_host_declaration_through_final_admission_and_compiled_graph(
    mode: str,
    tmp_path: Path,
) -> None:
    calls = {"read": 0, "write": 0, "foreign_search": 0}

    async def shared_read() -> str:
        calls["read"] += 1
        return f"read-{calls['read']}"

    async def write_tool() -> str:
        calls["write"] += 1
        return "wrote"

    async def foreign_tool_search() -> str:
        calls["foreign_search"] += 1
        return "foreign search executed"

    ctx = PluginContext(
        manifest=PluginManifest(
            id="reader",
            version="1.0.0",
            name="Reader",
            kind="tool",
            entry="plugin.py",
        ),
        source_path=Path("plugin"),
    )
    ctx.tool("read_tool", shared_read)
    ctx.tool("read tool", shared_read)
    ctx.tool("tool_search", foreign_tool_search)
    ctx.tool("write_tool", write_tool)
    declared_names = ["read tool"]
    register_host_setup(ctx, lambda facade: facade.register_read_only_tools(declared_names))
    host = PluginHost(lambda: None)
    host.bind_context(ctx, "1.0.0")
    declared_names.append("write_tool")
    loaded = ctx.to_loaded()
    loaded.context = ctx
    PluginRegistry().register(loaded)

    agent_plugins: dict[str, object] = {}
    global_plugins = {"reader": True}
    tools = build_plugin_tools(agent_plugins=agent_plugins, global_plugins=global_plugins)
    manager = object.__new__(AgentManager)
    manager._plugin_tool_origins = {}
    manager._record_plugin_tool_origins(
        "agent",
        tools,
        agent_plugins=agent_plugins,
        global_plugins=global_plugins,
    )
    tool_origins = manager._plugin_tool_origins["agent"]
    sanitize_plugin_tool_names(tools, reserved={"tool_search"})
    assert [tool.name for tool in tools] == [
        "read_tool",
        "read_tool_2",
        "tool_search_2",
        "write_tool",
    ]

    bound_tool_names: list[set[str]] = []

    class CapturingFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, bound_tools: object, **_kwargs: object) -> CapturingFakeModel:
            names = set()
            for tool in bound_tools:  # type: ignore[union-attr]
                if isinstance(tool, dict):
                    function = tool.get("function")
                    name = function.get("name") if isinstance(function, dict) else tool.get("name")
                else:
                    name = getattr(tool, "name", "")
                if name:
                    names.add(str(name))
            bound_tool_names.append(names)
            return self

    def tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        )

    model = CapturingFakeModel(
        responses=[
            tool_call("tool_search", {"query": "read tool", "max_results": 1}, "search"),
            tool_call("read_tool_2", {}, "declared"),
            tool_call("read_tool", {}, "undeclared"),
            tool_call("tool_search_2", {}, "foreign-search"),
            tool_call("write_tool", {}, "forged-write"),
            AIMessage(content="done"),
        ]
    )
    config = HarnessAgentConfig(
        tools=tools,
        deferred_tools=frozenset({"read_tool_2"}),
        tool_search_mode="client",
    )
    entry = _compiled_agent_entry("agent", config, tmp_path, model=model)
    manager = _manager(entry)
    manager._plugin_tool_origins = {"agent": tool_origins}
    manager._plugin_host = host
    candidate = manager._capture_plugin_mount_candidate("agent", entry.config)
    assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)
    mounted = manager._plugin_mount_snapshot("agent").registrations
    assert len(mounted) == 1
    assert mounted[0].read_only_tools == ("read_tool_2",)

    host.authorize_execution = _empty_authorization  # type: ignore[method-assign]
    request: dict[str, object] = {
        "configurable": {
            "thread_id": f"native-{mode}",
            "conversation_mode": mode,
            "conversation_mode_extra_read_tools": ["read_tool", "write_tool"],
        }
    }
    lease = await manager._begin_agent_execution(
        None,
        agent_id="agent",
        thread_id=f"native-{mode}",
        execution_kind=None,
        request=request,
    )
    try:
        configurable = request["configurable"]
        assert configurable["conversation_mode_extra_read_tools"] == [  # type: ignore[index]
            "read_tool_2",
            "search_knowledge",
            "tool_search",
        ]
        result = await entry.agent.graph.ainvoke(
            {"messages": [HumanMessage(content="Read the requested data.")]},
            config=request,
        )
    finally:
        host.end_execution(lease)
        entry.agent.close()

    assert any("tool_search" in names and "read_tool_2" in names for names in bound_tool_names)
    assert "read_tool_2" in result["loaded_tool_names"]
    assert any(
        isinstance(message, ToolMessage) and "Loaded tools:" in str(message.content)
        for message in result["messages"]
    )
    if mode in {"ask", "plan"}:
        assert all(
            "read_tool" not in names and "tool_search_2" not in names and "write_tool" not in names
            for names in bound_tool_names
        )
        assert calls == {"read": 1, "write": 0, "foreign_search": 0}
        blocked_ids = {
            message.tool_call_id
            for message in result["messages"]
            if isinstance(message, ToolMessage) and message.status == "error"
        }
        assert {"undeclared", "foreign-search", "forged-write"} <= blocked_ids
    else:
        assert any(
            {"read_tool", "tool_search_2", "write_tool"} <= names for names in bound_tool_names
        )
        assert calls == {"read": 2, "write": 1, "foreign_search": 1}


async def _empty_authorization(*_args: object, **_kwargs: object) -> dict[str, str]:
    return {}


def test_same_id_and_version_replacement_invalidates_actual_context_identity() -> None:
    class EqualContext:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, EqualContext)

    first_context, second_context = EqualContext(), EqualContext()
    hook = RequiredHook()
    registry = PluginRegistry()
    registry.register(_loaded_plugin(context=first_context, hook=hook))
    config = HarnessAgentConfig(middleware=registry.build_middleware_chain())
    entry = AgentEntry(
        agent_id="agent",
        agent=object(),
        config=config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    manager = _manager(entry)
    candidate = manager._capture_plugin_mount_candidate("agent", config)
    manager._publish_plugin_mount_snapshot("agent", entry, candidate)

    registry.register(_loaded_plugin(context=second_context, hook=hook))

    assert not manager._plugin_mount_is_current("agent")
    assert not manager._publish_plugin_mount_snapshot("agent", entry, candidate)


def test_agent_disabled_hook_is_absent_from_snapshot_even_when_global_plugin_is_loaded() -> None:
    context, hook = object(), RequiredHook()
    registry = PluginRegistry()
    registry.register(_loaded_plugin(context=context, hook=hook))
    disabled_config = HarnessAgentConfig(middleware=[])
    entry = AgentEntry(
        agent_id="agent",
        agent=object(),
        config=disabled_config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    manager = _manager(entry)
    candidate = manager._capture_plugin_mount_candidate("agent", disabled_config)
    manager._publish_plugin_mount_snapshot("agent", entry, candidate)

    assert manager._plugin_mount_snapshot("agent").mounted_hooks == {}


def test_tool_kind_provider_is_proved_by_actual_compiled_tool_function_identity(
    tmp_path: Path,
) -> None:
    async def lookup(query: str) -> str:
        return query

    context = object()
    registry = PluginRegistry()
    registry.register(
        LoadedPlugin(
            manifest=PluginManifest(
                id="knowledge-provider",
                version="2.0",
                name="Knowledge provider",
                kind="tool",
                entry="plugin.py",
            ),
            source_path=Path("plugin"),
            tools=[
                ToolRegistration(
                    "knowledge-provider",
                    "search_knowledge",
                    lookup,
                    "Search knowledge",
                )
            ],
            context=context,
        )
    )
    tool = build_plugin_tools(global_plugins={"knowledge-provider": True})[0]
    config = HarnessAgentConfig(tools=[tool], middleware=[])
    entry = _compiled_agent_entry("agent", config, tmp_path)
    manager = _manager(entry)
    try:
        candidate = manager._capture_plugin_mount_candidate("agent", entry.config)
        assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)

        mounted_tool, registered_function = manager._plugin_mount_snapshot("agent").mounted_tools[
            "knowledge-provider"
        ][0]
        assert mounted_tool is tool
        assert tool.coroutine is registered_function is lookup
        assert manager._plugin_mount_is_current("agent")
        entry.config.tools = build_plugin_tools(global_plugins={"knowledge-provider": True})
        assert not manager._plugin_mount_is_current("agent")
    finally:
        entry.agent.close()


async def test_failed_agent_rebuild_leaves_previously_published_mount_stale() -> None:
    context, hook = object(), RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    config = HarnessAgentConfig(middleware=PluginRegistry().build_middleware_chain())
    entry = AgentEntry(
        agent_id="agent",
        agent=object(),
        config=config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    manager = _manager(entry)
    generation, epoch = manager._mark_plugin_mount_stale("agent")
    candidate = manager._capture_plugin_mount_candidate(
        "agent", config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)

    class HarnessManager:
        shared_factory = object()

        def get_agent(self, _agent_id: str) -> AgentEntry:
            return entry

        async def arebuild_agent(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("graph build failed")

    row = SimpleNamespace(enabled=True, last_state="running")
    states: list[tuple[str, str, str | None]] = []
    manager._harness_manager = HarnessManager()
    manager._agent_lifecycle_locks = {}
    manager._bootstrap_graph_refresh_pending = set()
    manager._repos = SimpleNamespace(
        agent_repo=SimpleNamespace(
            get=lambda _agent_id: row,
            set_state=lambda agent_id, state, error=None: states.append((agent_id, state, error)),
        )
    )
    manager._agent_runtime_bundle = lambda _row: (config, {}, [], "User")
    manager._octop_harness_or_none = lambda _agent_id: None

    await manager._reload_agent("agent")

    assert manager._plugin_mount_snapshots == {}
    assert not manager._plugin_mount_is_current("agent")
    assert states and states[-1][1] == "failed"


def test_failed_in_place_bootstrap_graph_rebuild_leaves_mount_stale() -> None:
    context, hook = object(), RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    config = HarnessAgentConfig(middleware=PluginRegistry().build_middleware_chain())

    class Agent:
        def _init_graph(self) -> None:
            raise RuntimeError("graph build failed")

    entry = AgentEntry(
        agent_id="agent",
        agent=Agent(),
        config=config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    manager = _manager(entry)
    generation, epoch = manager._mark_plugin_mount_stale("agent")
    candidate = manager._capture_plugin_mount_candidate(
        "agent", config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)
    manager._bootstrap_graph_refresh_pending = {"agent"}

    manager._apply_pending_bootstrap_graph_refresh("agent")

    assert "agent" in manager._bootstrap_graph_refresh_pending
    assert manager._plugin_mount_snapshots == {}
    assert not manager._plugin_mount_is_current("agent")


def test_failed_rebuild_all_leaves_in_place_mount_stale() -> None:
    context, hook = object(), RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    config = HarnessAgentConfig(middleware=PluginRegistry().build_middleware_chain())
    entry = AgentEntry(
        agent_id="agent",
        agent=object(),
        config=config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    manager = _manager(entry)
    generation, epoch = manager._mark_plugin_mount_stale("agent")
    candidate = manager._capture_plugin_mount_candidate(
        "agent", config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)

    class HarnessManager:
        def get_agent(self, _agent_id: str) -> AgentEntry:
            return entry

        def list_agents(self) -> list[AgentEntry]:
            return [entry]

        def rebuild_all_agents(self) -> None:
            raise RuntimeError("graph build failed")

    manager._harness_manager = HarnessManager()
    with pytest.raises(RuntimeError, match="graph build failed"):
        manager.reload_octop_harnesss()

    assert manager._plugin_mount_snapshots == {}
    assert not manager._plugin_mount_is_current("agent")


def test_successful_rebuild_all_retry_restores_live_agent_mount() -> None:
    context, hook = object(), RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    config = HarnessAgentConfig(middleware=PluginRegistry().build_middleware_chain())
    entry = AgentEntry(
        agent_id="agent",
        agent=object(),
        config=config,
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    manager = _manager(entry)
    generation, epoch = manager._mark_plugin_mount_stale("agent")
    candidate = manager._capture_plugin_mount_candidate(
        "agent", config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("agent", entry, candidate)

    class HarnessManager:
        rebuilds = 0

        def get_agent(self, _agent_id: str) -> AgentEntry:
            return entry

        def list_agents(self) -> list[AgentEntry]:
            return [entry]

        def rebuild_all_agents(self) -> None:
            self.rebuilds += 1
            if self.rebuilds == 1:
                raise RuntimeError("partial graph rebuild failed")

    harness_manager = HarnessManager()
    manager._harness_manager = harness_manager
    with pytest.raises(RuntimeError, match="partial graph rebuild failed"):
        manager.reload_octop_harnesss()

    assert manager._plugin_mount_snapshots == {}
    manager.reload_octop_harnesss()

    assert harness_manager.rebuilds == 2
    assert manager._plugin_mount_is_current("agent")


def test_real_sdk_rebuild_all_rebinds_actual_replaced_entry_and_config(tmp_path: Path) -> None:
    context, hook = object(), RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    sdk, config = _real_sdk_manager(tmp_path / "agent")
    config = replace(config, middleware=PluginRegistry().build_middleware_chain())
    old_entry = sdk.create_agent(config, agent_id="agent", init_workspace=False)
    manager = _manager(old_entry)
    manager._harness_manager = sdk
    try:
        generation, epoch = manager._mark_plugin_mount_stale("agent")
        candidate = manager._capture_plugin_mount_candidate(
            "agent", old_entry.config, generation=generation, registry_epoch=epoch
        )
        assert manager._publish_plugin_mount_snapshot(
            "agent", old_entry, candidate, bind_config_token=True
        )

        manager.reload_octop_harnesss()
        new_entry = sdk.get_agent("agent")
        assert new_entry is not old_entry
        assert new_entry.config is not old_entry.config
        assert new_entry.agent._graph is not None
        assert manager._plugin_mount_is_current("agent")
        snapshot = manager._plugin_mount_snapshot("agent")
        assert snapshot.entry is new_entry
        assert snapshot.config is new_entry.config
    finally:
        sdk.close()


def test_real_sdk_partial_rebuild_failure_keeps_successful_entry_eligible_for_retry(
    tmp_path: Path,
) -> None:
    context, hook = object(), RequiredHook()
    PluginRegistry().register(_loaded_plugin(context=context, hook=hook))
    sdk, base_config = _real_sdk_manager(tmp_path)
    config_a = replace(
        base_config,
        name="agent_a",
        workspace_dir=tmp_path / "agent_a",
        middleware=PluginRegistry().build_middleware_chain(),
    )
    config_b = replace(
        base_config,
        name="agent_b",
        workspace_dir=tmp_path / "agent_b",
        middleware=PluginRegistry().build_middleware_chain(),
    )
    old_a = sdk.create_agent(config_a, agent_id="agent_a", init_workspace=False)
    old_b = sdk.create_agent(config_b, agent_id="agent_b", init_workspace=False)
    manager = _manager(old_a)
    manager._harness_manager = sdk
    manager._plugin_config_tokens["agent_b"] = 0
    manager._plugin_entry_config_tokens["agent_b"] = (old_b, 0)
    try:
        for entry in (old_a, old_b):
            generation, epoch = manager._mark_plugin_mount_stale(entry.agent_id)
            candidate = manager._capture_plugin_mount_candidate(
                entry.agent_id,
                entry.config,
                generation=generation,
                registry_epoch=epoch,
            )
            assert manager._publish_plugin_mount_snapshot(
                entry.agent_id, entry, candidate, bind_config_token=True
            )

        old_b.config.middleware = [object()]
        with pytest.raises(AttributeError, match="has no attribute 'name'"):
            manager.reload_octop_harnesss()

        new_a = sdk.get_agent("agent_a")
        assert new_a is not old_a
        assert not manager._plugin_mount_is_current("agent_a")
        assert manager._plugin_entry_config_tokens["agent_a"] == (new_a, 0)
        assert "agent_a" not in manager._plugin_mount_snapshots
        assert "agent_b" not in {entry.agent_id for entry in sdk.list_agents()}

        manager.reload_octop_harnesss()

        assert manager._plugin_mount_is_current("agent_a")
    finally:
        sdk.close()
