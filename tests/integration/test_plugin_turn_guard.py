from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory
from octop_harness.manager import HarnessAgentManager
from octop_harness.plugins import LoadedPlugin, PluginManifest, PluginRegistry
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.registry import MiddlewareRegistration
from octop_harness.registry import AgentEntry
from starlette.websockets import WebSocketDisconnect, WebSocketState

from octop.api.routers.chat.models import ChatTurnBody, HitlResumeBody
from octop.api.routers.chat.turn import build_dashboard_inbound, prepare_dashboard_turn
from octop.config import OctopConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.plugins.execution import (
    activate_ticket,
    issue_execution_ticket,
    reset_ticket,
)
from octop.infra.agents.plugins.host import PluginHost, register_host_setup
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
    PluginHostError,
    RequiredExtensionWrite,
)
from octop.infra.agents.plugins.manager import (
    current_state,
    mutate,
    register_host,
    unregister_host,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.gateway.process.message_keys import session_metadata_from_message
from octop.infra.gateway.process.processor import GlobalProcessor
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


class _RequiredHook(AgentMiddleware):
    pass


def _compile_entry(agent_id: str, config: HarnessAgentConfig, workspace: Path) -> AgentEntry:
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

    factory.get_chat_model = lambda _ref: NoNetworkModel(responses=[])
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


def _offline_sdk_manager() -> tuple[HarnessAgentManager, list[ProviderConfig]]:
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
    return manager, providers


@pytest.fixture(autouse=True)
def _plugin_registry_isolation():
    registry = PluginRegistry()
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
async def execution_fixture(tmp_path: Path) -> Any:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    owner = UserRepo(db).create(
        username="owner",
        role="user",
        permissions=["agent_plugins"],
    )
    services.agent_repo.create(agent_id="guarded", user_id=owner, name="Guarded")
    registry = ThreadRegistry(session_repo=services.session_repo, thread_repo=services.thread_repo)
    thread_id = await registry.get_or_create_by_key(
        session_key="guarded:dashboard:owner",
        agent_id="guarded",
        user_id=owner,
        channel_type="dashboard",
    )
    host = PluginHost(lambda: services)
    register_host(host)
    try:
        facade = host.bind("required-plugin", "1.2.3")
    except BaseException:
        unregister_host(host)
        raise
    context = object()
    hook = _RequiredHook()
    PluginRegistry().register(
        LoadedPlugin(
            manifest=PluginManifest(
                id="required-plugin",
                version="1.2.3",
                name="Required plugin",
                kind="hook",
                entry="plugin.py",
            ),
            source_path=tmp_path,
            middleware=[MiddlewareRegistration("required-plugin", hook)],
            context=context,
        )
    )
    entry = AgentEntry(
        agent_id="guarded",
        agent=SimpleNamespace(_init_graph=lambda: None),
        config=HarnessAgentConfig(middleware=PluginRegistry().build_middleware_chain()),
        metadata={},
        tags=[],
        created_at=datetime.now(UTC),
    )
    host._test_entry = entry
    actor = PluginActor(
        owner,
        False,
        frozenset({"agent_plugins"}),
        agent_id="guarded",
        thread_id=thread_id,
    )
    try:
        await facade.commit(
            [],
            requirements=(
                RequiredExtensionWrite(
                    actor=actor,
                    agent_id="guarded",
                    thread_id=thread_id,
                    required_plugins={"required-plugin": "1.2.3"},
                    expected_revision=0,
                ),
            ),
        )
        yield SimpleNamespace(
            host=host,
            facade=facade,
            actor=actor,
            thread_id=thread_id,
            thread_registry=registry,
            entry=entry,
        )
    finally:
        await host.close_and_wait()
        unregister_host(host)


async def test_processor_rejects_bound_team_before_dispatch_without_ticket(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    processor = object.__new__(GlobalProcessor)
    processor._agent_manager = SimpleNamespace(_plugin_host=fixture.host)
    called = False

    async def body(_msg: Any):
        nonlocal called
        called = True
        yield {"type": "token", "text": "should not run"}

    processor._iter_turn_chunks_body = body
    message = SimpleNamespace(
        tenant_id="guarded",
        metadata={"thread_id": fixture.thread_id},
    )
    chunks = [chunk async for chunk in processor.iter_turn_chunks(message)]
    assert [chunk["type"] for chunk in chunks] == ["error", "done"]
    assert called is False


async def test_processor_scope_uses_ticket_and_clears_it_after_turn(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    processor = object.__new__(GlobalProcessor)
    processor._agent_manager = SimpleNamespace(_plugin_host=fixture.host)
    ticket = issue_execution_ticket(
        fixture.host,
        fixture.actor,
        plugin_context={"required-plugin": {"query": "current"}},
    )
    message = SimpleNamespace(
        tenant_id="guarded",
        metadata={
            "thread_id": fixture.thread_id,
            "_octop_plugin_execution_ticket": ticket,
        },
    )

    async def body(_msg: Any):
        execution = await fixture.facade.current_execution()
        assert execution.context == {"query": "current"}
        yield {"type": "done"}

    processor._iter_turn_chunks_body = body
    assert [chunk async for chunk in processor.iter_turn_chunks(message)] == [{"type": "done"}]
    with pytest.raises(PluginHostError):
        await fixture.facade.current_execution()


def _manager_with_fake_harness(host: PluginHost) -> tuple[AgentManager, Any]:
    calls = SimpleNamespace(stream=0, call=0, resume=0, rebuild=0)

    class FakeHarness:
        def get_agent(self, _agent_id: str) -> Any:
            return host._test_entry

        def list_agents(self) -> list[AgentEntry]:
            return [host._test_entry]

        def rebuild_all_agents(self) -> None:
            calls.rebuild += 1

        async def stream(self, _agent_id: str, _request: Any):
            calls.stream += 1
            yield {"type": "token"}

        async def call(self, _agent_id: str, _request: Any):
            calls.call += 1
            return {"ok": True}

        async def resume_hitl(self, _agent_id: str, _thread_id: str, _decisions: Any):
            calls.resume += 1
            yield {"type": "token"}

    manager = object.__new__(AgentManager)
    manager._harness_manager = FakeHarness()
    manager._plugin_host = host
    manager._thread_execution_locks = {}
    manager._active_invocations = {}
    manager._invocation_waiters = {}
    manager._history_backfills = {}
    manager._reclaim_holds = {}
    manager._reclaim_pending = set()
    manager._bootstrap_graph_refresh_pending = set()
    manager._plugin_mount_generations = {}
    manager._plugin_mount_snapshots = {}
    manager._plugin_config_tokens = {"guarded": 0}
    manager._plugin_config_writes = {}
    manager._plugin_entry_config_tokens = {"guarded": (host._test_entry, 0)}
    manager._reload_dirty = set()
    manager._reload_worker_running = {"guarded": True}
    manager._apply_pending_bootstrap_graph_refresh = lambda _agent_id: None
    manager._prepare_stream_request = lambda _agent_id, request: request
    manager._stream_model_label = lambda _agent_id, _request, _thread_id: "test-model"
    generation, epoch = manager._mark_plugin_mount_stale("guarded")
    candidate = manager._capture_plugin_mount_candidate(
        "guarded", host._test_entry.config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("guarded", host._test_entry, candidate)
    return manager, calls


async def test_agent_manager_stream_call_and_resume_reject_before_harness(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    request = {"thread_id": fixture.thread_id, "messages": []}
    with pytest.raises(PluginHostError):
        _ = [chunk async for chunk in manager.stream("guarded", request)]
    with pytest.raises(PluginHostError):
        await manager.call("guarded", request)
    with pytest.raises(PluginHostError):
        _ = [chunk async for chunk in manager.resume_hitl("guarded", fixture.thread_id, [])]
    assert calls.stream == calls.call == calls.resume == 0


async def test_agent_manager_keeps_unbound_turn_compatibility(execution_fixture: Any) -> None:
    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)

    assert await manager.call("ordinary", {"thread_id": fixture.thread_id, "messages": []}) == {
        "ok": True
    }
    assert calls.call == 1


async def test_persisted_plugin_config_keeps_old_graph_stale_until_rebuild(
    execution_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    monkeypatch.setattr(manager, "get_config", lambda _agent_id: {})
    monkeypatch.setattr(manager, "sync_effective_tools_disabled", lambda *_args: None)
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    persist_entered = threading.Event()
    release_persist = threading.Event()
    probe_done = threading.Event()
    preflight_results: list[str] = []
    publication_results: list[bool] = []

    def gated_persist(*_args: Any, **_kwargs: Any) -> None:
        persist_entered.set()
        if not release_persist.wait(timeout=5):
            raise TimeoutError("test did not release persistence gate")

    monkeypatch.setattr(manager, "persist_harness_config", gated_persist)

    def preflight_while_persist_is_gated() -> None:
        async def check() -> None:
            token = activate_ticket(fixture.host, ticket)
            try:
                generation, epoch = manager._mark_plugin_mount_stale("guarded")
                candidate = manager._capture_plugin_mount_candidate(
                    "guarded",
                    fixture.entry.config,
                    generation=generation,
                    registry_epoch=epoch,
                    config_token=manager._plugin_config_tokens["guarded"],
                )
                publication_results.append(
                    manager._publish_plugin_mount_snapshot(
                        "guarded", fixture.entry, candidate, bind_config_token=True
                    )
                )
                lease = await manager._begin_agent_execution(
                    ticket,
                    agent_id="guarded",
                    thread_id=fixture.thread_id,
                    execution_kind=None,
                )
            except PluginHostError:
                preflight_results.append("rejected")
            else:
                preflight_results.append("admitted")
                fixture.host.end_execution(lease)
            finally:
                reset_ticket(token)

        try:
            assert persist_entered.wait(timeout=3)
            asyncio.run(check())
        finally:
            release_persist.set()
            probe_done.set()

    probe = threading.Thread(target=preflight_while_persist_is_gated, name="p07b-persist-gate")
    probe.start()
    try:
        await manager.persist_plugin_tools_config(
            "guarded", {"required-plugin": {"enabled": False}}
        )
        assert probe_done.is_set()
        assert preflight_results == ["rejected"]
        assert publication_results == [False]
        assert calls.call == 0
    finally:
        release_persist.set()
        await asyncio.to_thread(probe.join, 3)


async def test_pending_async_reload_does_not_admit_old_graph(execution_fixture: Any) -> None:
    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    manager._schedule_reload("guarded")

    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    try:
        with pytest.raises(PluginHostError):
            await manager.call("guarded", {"thread_id": fixture.thread_id, "messages": []})
        assert calls.call == 0
        assert manager._reload_dirty == {"guarded"}
    finally:
        reset_ticket(token)


async def test_old_pending_bootstrap_refresh_cannot_restore_persisted_disabled_graph(
    execution_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    manager._bootstrap_graph_refresh_pending.add("guarded")
    monkeypatch.setattr(manager, "get_config", lambda _agent_id: {})
    monkeypatch.setattr(manager, "persist_harness_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "sync_effective_tools_disabled", lambda *_args: None)
    await manager.persist_plugin_tools_config("guarded", {"required-plugin": {"enabled": False}})

    del manager._apply_pending_bootstrap_graph_refresh
    manager._bootstrap_graph_refresh_pending.add("guarded")
    manager._apply_pending_bootstrap_graph_refresh("guarded")

    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    try:
        with pytest.raises(PluginHostError):
            await manager.call("guarded", {"thread_id": fixture.thread_id, "messages": []})
        assert calls.call == 0
        assert not manager._plugin_mount_is_current("guarded")
    finally:
        reset_ticket(token)


async def test_real_agent_config_compiled_graph_gates_shared_actors_and_recovers(
    execution_fixture: Any,
    tmp_path: Path,
) -> None:
    fixture = execution_fixture
    services = fixture.host._services_provider()
    workspace = tmp_path / "guarded-workspace"
    services.agent_repo.update_config(
        "guarded",
        config_json=json.dumps(
            {"workspace_dir": str(workspace), "memory": {"memory_enabled": False}}
        ),
    )
    services.agent_repo.create(
        agent_id="ordinary",
        user_id=fixture.actor.user_id,
        name="Ordinary",
        config_json=json.dumps(
            {
                "workspace_dir": str(tmp_path / "ordinary-workspace"),
                "memory": {"memory_enabled": False},
            }
        ),
    )
    second_user = UserRepo(services.db).create(
        username="second-user-real-builder",
        role="user",
        permissions=["agent_plugins"],
    )
    services.agent_repo.set_shared("guarded", True)
    second_thread = await fixture.thread_registry.get_or_create_by_key(
        session_key="guarded:dashboard:second-user-real-builder",
        agent_id="guarded",
        user_id=second_user,
        channel_type="dashboard",
    )
    second_actor = PluginActor(
        second_user,
        False,
        frozenset({"agent_plugins"}),
        agent_id="guarded",
        thread_id=second_thread,
    )
    await fixture.facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                PluginActor(
                    fixture.actor.user_id,
                    fixture.actor.is_admin,
                    fixture.actor.permissions,
                    agent_id="guarded",
                ),
                "guarded",
                {"required-plugin": "1.2.3"},
                0,
            ),
        ),
    )
    manager = AgentManager(
        repos=services.repos,
        paths=services.paths,
        config=services.config,
        plugin_host=fixture.host,
    )
    manager._plugin_manager = SimpleNamespace(
        global_enabled_map=lambda: {"required-plugin": True}, list_installed=lambda: []
    )

    async def no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def collect(iterator: Any) -> list[Any]:
        return [item async for item in iterator]

    manager._post_start_agent = no_op
    calls = SimpleNamespace(stream=0, call=0, resume=0)
    entries: dict[str, AgentEntry] = {}

    class Harness:
        shared_factory = object()

        def get_agent(self, agent_id: str) -> AgentEntry:
            return entries[agent_id]

        def list_agents(self) -> list[AgentEntry]:
            return list(entries.values())

        async def arebuild_agent(
            self, agent_id: str, config: HarnessAgentConfig, **_kwargs: Any
        ) -> AgentEntry:
            entry = _compile_entry(agent_id, config, Path(config.workspace_dir))
            entries[agent_id] = entry
            return entry

        async def call(self, _agent_id: str, _request: Any) -> dict[str, bool]:
            calls.call += 1
            return {"ok": True}

        async def stream(self, _agent_id: str, _request: Any):
            calls.stream += 1
            yield {"type": "token"}

        async def resume_hitl(self, _agent_id: str, _thread_id: str, _decisions: Any):
            calls.resume += 1
            yield {"type": "token"}

    harness = Harness()
    manager._harness_manager = harness
    for agent_id in ("guarded", "ordinary"):
        row = manager.get_row(agent_id)
        assert row is not None
        config = manager._build_harness_config(row)
        entry = _compile_entry(agent_id, config, Path(config.workspace_dir))
        entries[agent_id] = entry
        generation, epoch = manager._mark_plugin_mount_stale(agent_id)
        candidate = manager._capture_plugin_mount_candidate(
            agent_id, entry.config, generation=generation, registry_epoch=epoch
        )
        assert manager._publish_plugin_mount_snapshot(
            agent_id, entry, candidate, bind_config_token=True
        )
    guarded_hook = fixture.entry.config.middleware[0]
    assert guarded_hook in entries["guarded"].config.middleware
    assert manager._plugin_mount_is_current("ordinary")

    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    second_ticket = issue_execution_ticket(fixture.host, second_actor)
    token = activate_ticket(fixture.host, ticket)
    try:
        await manager.persist_plugin_tools_config(
            "guarded", {"required-plugin": {"enabled": False}}
        )
        manager._bootstrap_graph_refresh_pending.add("guarded")
        manager._apply_pending_bootstrap_graph_refresh("guarded")
        assert not manager._plugin_mount_is_current("guarded")
        assert manager._plugin_mount_is_current("ordinary")

        await manager._reload_agent("guarded")
        assert all(hook is not guarded_hook for hook in entries["guarded"].config.middleware or [])
        for invoke in (
            manager.call("guarded", {"thread_id": fixture.thread_id, "messages": []}),
            collect(manager.stream("guarded", {"thread_id": fixture.thread_id, "messages": []})),
            collect(manager.resume_hitl("guarded", fixture.thread_id, [])),
        ):
            with pytest.raises(PluginHostError):
                await invoke
        assert calls.call == calls.stream == calls.resume == 0
        assert manager._plugin_mount_is_current("ordinary")

        await manager.persist_plugin_tools_config("guarded", {"required-plugin": {"enabled": True}})
        await manager._reload_agent("guarded")
        assert guarded_hook in entries["guarded"].config.middleware
        assert manager._plugin_mount_is_current("guarded")
        reset_ticket(token)
        token = activate_ticket(fixture.host, second_ticket)
        assert await manager.call("guarded", {"thread_id": second_thread, "messages": []}) == {
            "ok": True
        }
        assert await collect(
            manager.stream("guarded", {"thread_id": second_thread, "messages": []})
        )
        assert await collect(manager.resume_hitl("guarded", second_thread, []))
        assert calls.call == calls.stream == calls.resume == 1
    finally:
        reset_ticket(token)
        for entry in entries.values():
            entry.agent.close()


async def test_persisted_config_blocks_actual_sdk_rebuild_from_old_bound_entry(
    execution_fixture: Any,
    tmp_path: Path,
) -> None:
    fixture = execution_fixture
    services = fixture.host._services_provider()
    services.agent_repo.update_config(
        "guarded",
        config_json=json.dumps(
            {
                "workspace_dir": str(tmp_path / "actual-sdk-workspace"),
                "memory": {"memory_enabled": False},
            }
        ),
    )
    manager = AgentManager(
        repos=services.repos,
        paths=services.paths,
        config=services.config,
        plugin_host=fixture.host,
    )
    manager._plugin_manager = SimpleNamespace(
        global_enabled_map=lambda: {"required-plugin": True}, list_installed=lambda: []
    )
    row = manager.get_row("guarded")
    assert row is not None
    config = manager._build_harness_config(row)
    sdk, providers = _offline_sdk_manager()
    config = replace(
        config,
        providers=providers,
        default_model="test-provider/test-model",
        memory=(),
        todos_enabled=False,
        model_retry_enabled=False,
        memory_enabled=False,
        session_log_enabled=False,
        checkpointer=InMemorySaver(),
    )
    old_entry = sdk.create_agent(config, agent_id="guarded", init_workspace=False)
    manager._harness_manager = sdk
    manager.sync_effective_tools_disabled = lambda *_args: None
    generation, epoch = manager._mark_plugin_mount_stale("guarded")
    candidate = manager._capture_plugin_mount_candidate(
        "guarded", old_entry.config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot(
        "guarded", old_entry, candidate, bind_config_token=True
    )
    try:
        await manager.persist_plugin_tools_config(
            "guarded", {"required-plugin": {"enabled": False}}
        )
        assert manager.get_config("guarded")["plugins"]["required-plugin"]["enabled"] is False

        manager.reload_octop_harnesss()

        rebuilt_entry = sdk.get_agent("guarded")
        assert rebuilt_entry is not old_entry
        assert rebuilt_entry.config is not old_entry.config
        assert old_entry.config.middleware[0] in rebuilt_entry.config.middleware
        assert manager._plugin_entry_config_tokens["guarded"] == (old_entry, 0)
        assert manager._plugin_mount_snapshots == {}
        assert not manager._plugin_mount_is_current("guarded")
    finally:
        sdk.close()


async def test_mount_validation_and_task_tracking_are_atomic_with_registry_mutation(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    started = asyncio.Event()
    release = asyncio.Event()
    invalidated = asyncio.Event()

    async def tracked_turn() -> None:
        lease = await manager._begin_agent_execution(
            ticket,
            agent_id="guarded",
            thread_id=fixture.thread_id,
            execution_kind=None,
        )
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            invalidated.set()
            await release.wait()
        finally:
            fixture.host.end_execution(lease)

    turn = asyncio.create_task(tracked_turn())
    mutation: asyncio.Task[Any] | None = None
    try:
        await started.wait()
        mutation = asyncio.create_task(mutate(lambda: None))
        for _ in range(100):
            if current_state().in_progress and invalidated.is_set():
                break
            await asyncio.sleep(0.001)
        assert current_state().in_progress and invalidated.is_set()
        with pytest.raises(PluginHostError):
            await manager._begin_agent_execution(
                ticket,
                agent_id="guarded",
                thread_id=fixture.thread_id,
                execution_kind=None,
            )
        assert calls.stream == calls.call == calls.resume == 0
    finally:
        release.set()
        await turn
        if mutation is not None:
            await mutation
        reset_ticket(token)


async def test_registry_mutation_cannot_cross_validated_untracked_admission_window(
    execution_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import octop.infra.agents.plugins.manager as coordinator

    fixture = execution_fixture
    manager, _calls = _manager_with_fake_harness(fixture.host)
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    tracking_entered = threading.Event()
    allow_tracking = threading.Event()
    task_tracked = threading.Event()
    mutation_attempted = threading.Event()
    mutation_done = threading.Event()
    release_execution = asyncio.Event()
    execution_cancelled = asyncio.Event()
    mutation_errors: list[BaseException] = []
    mutation_was_blocked: list[bool] = []
    controller_done = threading.Event()

    original_track = fixture.host._track_authorized_execution

    def pause_before_tracking(plugin_ids: Any, task: Any = None) -> Any:
        tracking_entered.set()
        if not allow_tracking.wait(timeout=5):
            raise TimeoutError("tracking gate was not released")
        lease = original_track(plugin_ids, task)
        task_tracked.set()
        return lease

    monkeypatch.setattr(fixture.host, "_track_authorized_execution", pause_before_tracking)
    original_is_plugin_task = coordinator._is_plugin_task

    def observe_mutation_attempt(task: Any) -> bool:
        mutation_attempted.set()
        return original_is_plugin_task(task)

    monkeypatch.setattr(coordinator, "_is_plugin_task", observe_mutation_attempt)

    async def admitted_execution() -> None:
        lease = await manager._begin_agent_execution(
            ticket,
            agent_id="guarded",
            thread_id=fixture.thread_id,
            execution_kind=None,
        )
        try:
            try:
                await release_execution.wait()
            except asyncio.CancelledError:
                execution_cancelled.set()
                await release_execution.wait()
        finally:
            fixture.host.end_execution(lease)

    execution = asyncio.create_task(admitted_execution())

    def run_mutation() -> None:
        try:
            asyncio.run(mutate(lambda: None))
        except BaseException as exc:
            mutation_errors.append(exc)
        finally:
            mutation_done.set()

    mutation_thread = threading.Thread(target=run_mutation, name="p07b-mutation-race")

    def coordinate_race() -> None:
        try:
            if not tracking_entered.wait(timeout=3):
                raise TimeoutError("admission did not reach task tracking")
            mutation_thread.start()
            if not mutation_attempted.wait(timeout=3):
                raise TimeoutError("mutation did not reach coordinator")
            mutation_was_blocked.append(not mutation_done.wait(timeout=0.05))
        except BaseException as exc:
            mutation_errors.append(exc)
        finally:
            allow_tracking.set()
            controller_done.set()

    controller = threading.Thread(target=coordinate_race, name="p07b-race-controller")
    controller.start()
    try:
        assert await asyncio.to_thread(tracking_entered.wait, 3)
        assert await asyncio.to_thread(task_tracked.wait, 3)
        assert await asyncio.to_thread(controller_done.wait, 3)
        assert mutation_was_blocked == [True]
        assert await asyncio.wait_for(execution_cancelled.wait(), timeout=3)
        assert not mutation_done.is_set()
        release_execution.set()
        await execution
        assert await asyncio.to_thread(mutation_done.wait, 3)
        assert mutation_errors == []
    finally:
        allow_tracking.set()
        release_execution.set()
        if not execution.done():
            with pytest.raises(asyncio.CancelledError):
                await execution
        if mutation_thread.is_alive():
            await asyncio.to_thread(mutation_thread.join, 3)
        if controller.is_alive():
            await asyncio.to_thread(controller.join, 3)
        reset_ticket(token)


async def test_registry_coordinator_drains_host_worker_before_mutation_and_rebind(
    execution_fixture: Any,
) -> None:
    import octop.infra.agents.plugins.manager as coordinator

    fixture = execution_fixture
    old_finalizing = asyncio.Event()
    release_old_finally = asyncio.Event()
    old_started = asyncio.Event()
    new_started = asyncio.Event()
    old_worker_calls = 0

    async def old_worker(_wake: asyncio.Event) -> None:
        nonlocal old_worker_calls
        old_worker_calls += 1
        old_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            old_finalizing.set()
            await release_old_finally.wait()

    old_context = PluginContext(
        manifest=PluginManifest("required-plugin", "1.2.4", "Required", "skill", "main.py"),
        source_path=Path("required-plugin"),
    )
    register_host_setup(
        old_context,
        lambda facade: facade.register_background_worker("reconcile", old_worker),
    )
    fixture.host.bind_context(old_context, "1.2.4")
    await fixture.host.start_background_workers()
    await asyncio.wait_for(old_started.wait(), timeout=2)

    mutated = asyncio.Event()
    mutation = asyncio.create_task(coordinator.mutate(mutated.set))
    await asyncio.wait_for(old_finalizing.wait(), timeout=2)
    assert not mutation.done()
    release_old_finally.set()
    await asyncio.wait_for(mutation, timeout=2)
    assert mutated.is_set()
    assert fixture.host.active_plugins == {}

    async def new_worker(_wake: asyncio.Event) -> None:
        new_started.set()
        await asyncio.Event().wait()

    new_context = PluginContext(
        manifest=PluginManifest("required-plugin", "1.2.5", "Required", "skill", "main.py"),
        source_path=Path("required-plugin"),
    )
    register_host_setup(
        new_context,
        lambda facade: facade.register_background_worker("reconcile", new_worker),
    )
    fixture.host.bind_context(new_context, "1.2.5")
    await asyncio.wait_for(new_started.wait(), timeout=2)
    assert old_started.is_set() and old_worker_calls == 1


async def test_rebuilt_graph_without_required_hook_rejects_all_public_harness_paths(
    execution_fixture: Any,
) -> None:
    from dataclasses import replace

    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    fixture.entry.config = replace(fixture.entry.config, middleware=[])
    generation, epoch = manager._mark_plugin_mount_stale("guarded")
    candidate = manager._capture_plugin_mount_candidate(
        "guarded", fixture.entry.config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("guarded", fixture.entry, candidate)

    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    pending_consumed = False

    def consume_pending() -> None:
        nonlocal pending_consumed
        pending_consumed = True

    try:
        with pytest.raises(PluginHostError):
            _ = [
                chunk
                async for chunk in manager.stream(
                    "guarded", {"thread_id": fixture.thread_id, "messages": []}
                )
            ]
        with pytest.raises(PluginHostError):
            await manager.call("guarded", {"thread_id": fixture.thread_id, "messages": []})
        with pytest.raises(PluginHostError):
            _ = [
                chunk
                async for chunk in manager.resume_hitl(
                    "guarded", fixture.thread_id, [], on_admitted=consume_pending
                )
            ]
    finally:
        reset_ticket(token)
    assert calls.stream == calls.call == calls.resume == 0
    assert not pending_consumed


async def test_successful_current_graph_rebuild_restores_admission(
    execution_fixture: Any,
) -> None:
    from dataclasses import replace

    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    fixture.entry.config = replace(fixture.entry.config, middleware=[])
    generation, epoch = manager._mark_plugin_mount_stale("guarded")
    disabled = manager._capture_plugin_mount_candidate(
        "guarded", fixture.entry.config, generation=generation, registry_epoch=epoch
    )
    assert manager._publish_plugin_mount_snapshot("guarded", fixture.entry, disabled)

    active_config = replace(
        fixture.entry.config,
        middleware=PluginRegistry().build_middleware_chain(),
    )
    fixture.entry.config = active_config
    manager.reload_octop_harnesss()

    assert calls.rebuild == 1
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    try:
        assert await manager.call("guarded", {"thread_id": fixture.thread_id, "messages": []}) == {
            "ok": True
        }
    finally:
        reset_ticket(token)


async def test_public_stream_and_call_lease_cancel_direct_harness_work(
    execution_fixture: Any,
) -> None:
    import asyncio

    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    started = asyncio.Event()
    never = asyncio.Event()

    class BlockingHarness:
        def get_agent(self, _agent_id: str) -> Any:
            return fixture.entry

        async def stream(self, _agent_id: str, _request: Any):
            calls.stream += 1
            started.set()
            await never.wait()
            yield {"type": "token"}

        async def call(self, _agent_id: str, _request: Any):
            calls.call += 1
            started.set()
            await never.wait()
            return {"ok": True}

    manager._harness_manager = BlockingHarness()
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    try:
        call_task = asyncio.create_task(
            manager.call("guarded", {"thread_id": fixture.thread_id, "messages": []})
        )
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        reset_ticket(token)
    await fixture.host.deactivate_and_wait("required-plugin")
    assert call_task.cancelled()
    assert calls.call == 1


async def test_public_stream_lease_cancel_direct_harness_work(execution_fixture: Any) -> None:
    import asyncio

    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    started = asyncio.Event()

    class BlockingHarness:
        def get_agent(self, _agent_id: str) -> Any:
            return fixture.entry

        async def stream(self, _agent_id: str, _request: Any):
            calls.stream += 1
            started.set()
            await asyncio.Event().wait()
            yield {"type": "token"}

    manager._harness_manager = BlockingHarness()
    token = activate_ticket(fixture.host, issue_execution_ticket(fixture.host, fixture.actor))
    try:
        task = asyncio.create_task(_consume_stream(manager, "guarded", fixture.thread_id))
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        reset_ticket(token)
    await fixture.host.deactivate_and_wait("required-plugin")
    assert task.cancelled()
    assert calls.stream == 1


async def _consume_stream(manager: AgentManager, agent_id: str, thread_id: str) -> None:
    async for _ in manager.stream(agent_id, {"thread_id": thread_id, "messages": []}):
        pass


async def test_resume_rechecks_after_thread_lock_and_does_not_admit_pending(
    execution_fixture: Any,
) -> None:
    import asyncio

    fixture = execution_fixture
    manager, calls = _manager_with_fake_harness(fixture.host)
    pending_consumed = False

    def consume_pending() -> None:
        nonlocal pending_consumed
        pending_consumed = True

    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)
    try:
        async with manager._thread_execution_lock("guarded", fixture.thread_id):
            task = asyncio.create_task(
                _consume_resume(
                    manager,
                    "guarded",
                    fixture.thread_id,
                    consume_pending,
                )
            )
            await asyncio.sleep(0.05)
            assert not task.done()
            from octop.infra.db.repos.users import UserRepo

            UserRepo(fixture.host._services_provider().db).set_disabled(fixture.actor.user_id, True)
        with pytest.raises(PluginHostError):
            await task
    finally:
        reset_ticket(token)
    assert not pending_consumed
    assert calls.resume == 0


async def _consume_resume(
    manager: AgentManager,
    agent_id: str,
    thread_id: str,
    on_admitted: Any,
) -> None:
    async for _ in manager.resume_hitl(agent_id, thread_id, [], on_admitted=on_admitted):
        pass


async def test_commit_rechecks_required_versions_inside_operation_lease(
    execution_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = execution_fixture
    host = fixture.host
    host.bind("binding-plugin", "2.0")
    facade = host.facade("binding-plugin")
    original_run = facade._run

    async def deactivate_between_check_and_transaction(operation: Any, **kwargs: Any):
        await host.deactivate_and_wait("required-plugin")
        return await original_run(operation, **kwargs)

    monkeypatch.setattr(facade, "_run", deactivate_between_check_and_transaction)
    actor = PluginActor(
        fixture.actor.user_id,
        False,
        frozenset({"agent_plugins"}),
        agent_id="guarded",
    )
    with pytest.raises(PluginHostError):
        await facade.commit(
            [],
            requirements=(
                RequiredExtensionWrite(
                    actor,
                    "guarded",
                    {"binding-plugin": "2.0", "required-plugin": "1.2.3"},
                    0,
                ),
            ),
        )
    assert (await facade.get_requirement(actor, "guarded")).revision == 0


async def test_message_event_processor_rejects_bound_agent_before_team_routing(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    processor = object.__new__(GlobalProcessor)
    processor._agent_manager = SimpleNamespace(_plugin_host=fixture.host)
    called = False

    async def body(_msg: Any):
        nonlocal called
        called = True
        yield SimpleNamespace(type="text")

    processor._call_body = body
    message = SimpleNamespace(tenant_id="guarded", metadata={"thread_id": fixture.thread_id})
    events = [event async for event in processor(message)]
    assert len(events) == 2
    assert called is False


async def test_im_numeric_subject_cannot_issue_ticket_for_bound_agent(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    processor = object.__new__(GlobalProcessor)
    processor._agent_manager = SimpleNamespace(_plugin_host=fixture.host)
    routed = False

    async def would_route_im(_msg: Any):
        nonlocal routed
        routed = True
        yield SimpleNamespace(type="text")

    processor._call_body = would_route_im
    message = SimpleNamespace(
        tenant_id="guarded",
        channel_type="mcp",
        channel_subject=SimpleNamespace(subject_id=str(fixture.actor.user_id)),
        metadata={"thread_id": fixture.thread_id},
    )
    events = [event async for event in processor(message)]
    assert len(events) == 2
    assert routed is False


async def test_bound_parent_ticket_cannot_reach_unbound_child_dispatch(
    execution_fixture: Any,
) -> None:
    fixture = execution_fixture
    processor = object.__new__(GlobalProcessor)
    processor._agent_manager = SimpleNamespace(_plugin_host=fixture.host)
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    dispatched = False

    async def would_dispatch_child(_msg: Any):
        nonlocal dispatched
        dispatched = True
        yield {"type": "token"}

    processor._iter_turn_chunks_body = would_dispatch_child
    message = SimpleNamespace(
        tenant_id="unbound-child",
        metadata={
            "thread_id": fixture.thread_id,
            "_octop_plugin_execution_ticket": ticket,
        },
    )
    chunks = [chunk async for chunk in processor.iter_turn_chunks(message)]
    assert [chunk["type"] for chunk in chunks] == ["error", "done"]
    assert dispatched is False


async def test_session_key_cannot_reuse_another_users_thread(execution_fixture: Any) -> None:
    fixture = execution_fixture
    # The fixture's thread belongs to its owner. A second user's session-key
    # collision must not inherit that thread.
    from octop.api.routers.chat.turn import resolve_thread_id
    from octop.infra.errors import OctopError

    owner_id = fixture.actor.user_id
    registry = fixture.thread_registry
    session_key = registry.get_thread(fixture.thread_id).session_key
    with pytest.raises(OctopError):
        await resolve_thread_id(
            agent_id="guarded",
            user_id=owner_id + 100,
            thread_registry=registry,
            thread_id=None,
            session_key=session_key,
        )


async def test_dashboard_prepare_signs_authenticated_actor_before_steer_admission(
    execution_fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.api.routers.chat import turn as turn_module

    fixture = execution_fixture
    monkeypatch.setattr(
        turn_module,
        "require_agent_row",
        lambda *_args, **_kwargs: SimpleNamespace(default_model=None),
    )

    async def no_mcp(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_skills(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(turn_module, "validate_chat_mcp_servers", no_mcp)
    monkeypatch.setattr(turn_module, "validate_chat_skills", no_skills)
    runtime = SimpleNamespace(
        gateway=SimpleNamespace(thread_registry=fixture.thread_registry),
        agent_registry=SimpleNamespace(
            providers=SimpleNamespace(is_model_ref_usable=lambda _ref: True)
        ),
    )
    server = SimpleNamespace(app_runtime=runtime, plugin_host=fixture.host)
    user = SimpleNamespace(
        id=fixture.actor.user_id,
        is_admin=False,
        permissions=["agent_plugins"],
        locale="en",
    )
    turn = ChatTurnBody(
        thread_id=fixture.thread_id,
        text="hello",
        plugin_context={"required-plugin": {"term": "private"}},
    )

    prepared = await prepare_dashboard_turn(server, agent_id="guarded", user=user, turn=turn)
    inbound = build_dashboard_inbound(
        agent_id="guarded",
        user_id=user.id,
        prepared=prepared,
        turn=turn,
        ws_connection_id="ws-test",
    )
    ticket = inbound.metadata["_octop_plugin_execution_ticket"]
    assert ticket.actor.user_id == user.id
    assert ticket.agent_id == "guarded"
    assert ticket.thread_id == fixture.thread_id
    assert ticket.plugin_context == {"required-plugin": {"term": "private"}}
    assert "_octop_plugin_execution_ticket" not in session_metadata_from_message(inbound)

    from octop.infra.gateway.ws.ws_channel import _continuation_inbound

    steer_inbound = _continuation_inbound(inbound, text="follow up")
    steer_ticket = steer_inbound.metadata["_octop_plugin_execution_ticket"]
    assert steer_ticket.execution_kind == "continuation"
    assert steer_ticket.actor == ticket.actor
    assert steer_ticket.plugin_context == ticket.plugin_context

    from octop.infra.gateway.steer.coordinator import SteerCoordinator

    steer = SteerCoordinator()
    turn_id, _seq, _discarded = steer.begin_turn(fixture.thread_id)
    steer.remember_context(fixture.thread_id, inbound.metadata)
    from octop.api.routers.chat.ws import _maybe_admit_steer

    frames: list[dict[str, Any]] = []

    async def send_frame(frame: dict[str, Any]) -> None:
        frames.append(frame)

    class Hub:
        async def push_to_thread(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    admitted = await _maybe_admit_steer(
        text="steer this turn",
        thread_id=fixture.thread_id,
        delivery="steer",
        client_pending_id="pending-1",
        expected_turn_id=turn_id,
        steer=steer,
        hub=Hub(),
        send_frame=send_frame,
        hitl_paused=False,
    )
    assert admitted is True
    assert frames[0]["type"] == "steer.ack"
    continuation = steer.continuation_metadata(fixture.thread_id)
    assert continuation["plugin_context"] == {"required-plugin": {"term": "private"}}
    assert continuation["_octop_plugin_execution_ticket"].execution_kind == "continuation"


async def test_dashboard_websocket_enqueues_server_signed_ticket(
    execution_fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.api.routers.chat import turn as turn_module
    from octop.api.routers.chat import ws as ws_module
    from octop.infra.gateway.steer.coordinator import SteerCoordinator

    fixture = execution_fixture
    user = SimpleNamespace(
        id=fixture.actor.user_id,
        is_admin=False,
        permissions=["agent_plugins"],
        locale="en",
    )
    monkeypatch.setattr(ws_module, "resolve_user_from_token", lambda *_args: user)
    monkeypatch.setattr(ws_module, "assert_agent_access", lambda *_args: None)
    monkeypatch.setattr(
        turn_module,
        "require_agent_row",
        lambda *_args, **_kwargs: SimpleNamespace(default_model=None),
    )

    async def no_mcp(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_skills(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(turn_module, "validate_chat_mcp_servers", no_mcp)
    monkeypatch.setattr(turn_module, "validate_chat_skills", no_skills)
    queued: list[Any] = []

    class ChannelManager:
        def enqueue(self, _channel_id: str, inbound: Any) -> None:
            queued.append(inbound)

    class Hub:
        def register(self, _connection_id: str, _callback: Any) -> None:
            return None

        def unregister(self, _connection_id: str) -> None:
            return None

        def subscribe(self, _thread_id: str, _connection_id: str) -> None:
            return None

        async def push_to_thread(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    runtime = SimpleNamespace(
        gateway=SimpleNamespace(
            ws_hub=Hub(),
            channel_manager=ChannelManager(),
            steer=SteerCoordinator(),
            thread_registry=fixture.thread_registry,
        ),
        agent_registry=SimpleNamespace(
            providers=SimpleNamespace(is_model_ref_usable=lambda _ref: True)
        ),
    )
    server = SimpleNamespace(app_runtime=runtime, plugin_host=fixture.host)

    class Socket:
        app = SimpleNamespace(state=SimpleNamespace(octop_server=server))
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self._frames = [
                json.dumps(
                    {
                        "type": "user_turn",
                        "thread_id": fixture.thread_id,
                        "text": "hello",
                        "plugin_context": {"required-plugin": {"term": "websocket"}},
                    }
                )
            ]

        async def accept(self) -> None:
            return None

        async def receive_text(self) -> str:
            if self._frames:
                return self._frames.pop(0)
            raise WebSocketDisconnect(code=1000)

        async def send_text(self, _text: str) -> None:
            return None

        async def close(self, **_kwargs: Any) -> None:
            self.application_state = WebSocketState.DISCONNECTED

    await ws_module.dashboard_chat_ws(Socket(), "guarded", token="test-token")
    assert len(queued) == 1
    ticket = queued[0].metadata["_octop_plugin_execution_ticket"]
    assert ticket.actor.user_id == user.id
    assert ticket.thread_id == fixture.thread_id
    assert ticket.plugin_context == {"required-plugin": {"term": "websocket"}}


async def test_rest_hitl_resume_rebuilds_fresh_resume_ticket(
    execution_fixture: Any,
) -> None:
    from octop.api.routers.chat.routes import resume_hitl
    from octop.infra.gateway.hitl.coordinator import HitlChannelCoordinator
    from octop.infra.gateway.steer.coordinator import SteerCoordinator

    fixture = execution_fixture
    hitl = HitlChannelCoordinator()
    steer = SteerCoordinator()
    resume_checked = False

    class ResumeProcessor:
        hitl_coordinator = hitl

        async def iter_hitl_resume_chunks(self, **_kwargs: Any):
            nonlocal resume_checked
            execution = await fixture.facade.current_execution()
            assert execution.execution_kind == "resume"
            resume_checked = True
            yield {"type": "done"}

    row = SimpleNamespace(user_id=fixture.actor.user_id, is_shared=0)
    runtime = SimpleNamespace(
        agent_registry=SimpleNamespace(get_row=lambda _agent_id: row),
        gateway=SimpleNamespace(
            processor=ResumeProcessor(),
            thread_registry=fixture.thread_registry,
            steer=steer,
        ),
    )
    server = SimpleNamespace(app_runtime=runtime, plugin_host=fixture.host)
    user = SimpleNamespace(
        id=fixture.actor.user_id,
        is_admin=False,
        permissions=["agent_plugins"],
        locale="en",
    )

    async def connected() -> bool:
        return False

    request = SimpleNamespace(headers={}, is_disconnected=connected)
    response = await resume_hitl(
        "guarded",
        HitlResumeBody(thread_id=fixture.thread_id, decisions=[{"type": "approve"}]),
        request,
        user=user,
        server=server,
    )
    frames = [frame async for frame in response.body_iterator]
    assert frames
    assert resume_checked is True


async def test_shared_agent_concurrent_users_keep_ticket_identity_isolated(
    execution_fixture: Any,
) -> None:
    import asyncio

    fixture = execution_fixture
    services = fixture.host._services_provider()
    second_user = UserRepo(services.db).create(
        username="second-user",
        role="user",
        permissions=["agent_plugins"],
        locale="en",
    )
    services.agent_repo.set_shared("guarded", True)
    second_thread = await fixture.thread_registry.get_or_create_by_key(
        session_key="guarded:dashboard:second-user",
        agent_id="guarded",
        user_id=second_user,
        channel_type="dashboard",
    )
    second_actor = PluginActor(
        second_user,
        False,
        frozenset({"agent_plugins"}),
        agent_id="guarded",
        thread_id=second_thread,
    )
    agent_owner_actor = PluginActor(
        fixture.actor.user_id,
        fixture.actor.is_admin,
        fixture.actor.permissions,
        agent_id="guarded",
    )
    await fixture.facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                agent_owner_actor,
                "guarded",
                {"required-plugin": "1.2.3"},
                0,
            ),
        ),
    )
    tickets = (
        issue_execution_ticket(
            fixture.host,
            fixture.actor,
            plugin_context={"required-plugin": {"private": "owner"}},
        ),
        issue_execution_ticket(
            fixture.host,
            second_actor,
            plugin_context={"required-plugin": {"private": "second"}},
        ),
    )

    async def inspect(ticket: Any) -> tuple[int, str, dict[str, Any]]:
        lease = await fixture.host.begin_execution(
            ticket,
            agent_id="guarded",
            thread_id=ticket.thread_id,
        )
        token = activate_ticket(fixture.host, ticket)
        try:
            execution = await fixture.facade.current_execution()
            return execution.actor.user_id, ticket.thread_id, execution.context
        finally:
            reset_ticket(token)
            fixture.host.end_execution(lease)

    owner_result, second_result = await asyncio.gather(*(inspect(ticket) for ticket in tickets))
    assert owner_result == (
        fixture.actor.user_id,
        fixture.thread_id,
        {"private": "owner"},
    )
    assert second_result == (
        second_user,
        second_thread,
        {"private": "second"},
    )
