from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.agents.plugins.execution import (
    activate_ticket,
    current_ticket,
    issue_execution_ticket,
    reset_ticket,
)
from octop.infra.agents.plugins.host import PluginHost
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
    PluginHostError,
    RequiredExtensionWrite,
)
from octop.infra.agents.teams.team_manager import wire_host_dispatch
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


@pytest.fixture
async def delegation_fixture(tmp_path: Any) -> Any:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    user_id = UserRepo(db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="parent", user_id=user_id, name="Parent")
    services.agent_repo.create(agent_id="child", user_id=user_id, name="Child")
    registry = ThreadRegistry(session_repo=services.session_repo, thread_repo=services.thread_repo)
    thread_id = await registry.get_or_create_by_key(
        session_key="parent:dashboard:owner",
        agent_id="parent",
        user_id=user_id,
        channel_type="dashboard",
    )
    host = PluginHost(lambda: services)
    host.bind("parent-plugin", "1.0")
    actor = PluginActor(user_id, True, agent_id="parent", thread_id=thread_id)
    return SimpleNamespace(
        host=host,
        services=services,
        actor=actor,
        thread_id=thread_id,
        registry=registry,
    )


def _team(*, submit: Any, original: Any) -> Any:
    return SimpleNamespace(
        enabled=True,
        submit_peer=submit,
        call_peer=original,
        _call_agent=None,
        _invoke_peer=None,
        _enrich_request=None,
        inbox=None,
    )


async def _bind_parent(fixture: Any) -> None:
    facade = fixture.host.facade("parent-plugin")
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                fixture.actor,
                "parent",
                {"parent-plugin": "1.0"},
                0,
                fixture.thread_id,
            ),
        ),
    )


async def test_real_sync_peer_callback_is_not_called_for_bound_parent(
    delegation_fixture: Any,
) -> None:
    fixture = delegation_fixture
    await _bind_parent(fixture)
    calls: list[str] = []

    async def original(**_kwargs: Any) -> str:
        calls.append("called")
        return "child result"

    team = _team(submit=lambda **_kwargs: "queued", original=original)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    token = activate_ticket(fixture.host, issue_execution_ticket(fixture.host, fixture.actor))
    try:
        with pytest.raises(PluginHostError):
            await team.call_peer(
                from_agent_id="parent",
                to_agent_id="child",
                message="delegate",
                user_id=fixture.actor.user_id,
                source_thread_id=fixture.thread_id,
            )
    finally:
        reset_ticket(token)
    assert calls == []


async def test_bound_parent_cannot_enqueue_into_inbox_started_without_scope(
    delegation_fixture: Any,
) -> None:
    fixture = delegation_fixture
    await _bind_parent(fixture)
    from octop_harness.teams.inbox import HarnessAgentInboxManager

    child_calls: list[Any] = []
    inbox = HarnessAgentInboxManager(
        call_agent=lambda _agent, _request: _async_result({"ok": True}),
        processor=object(),
        invoke_target=lambda _message: _record_ticket(child_calls),
    )

    async def original(**_kwargs: Any) -> str:
        return "direct"

    def submit(**kwargs: Any) -> str:
        return inbox.enqueue(
            target_agent_id=kwargs["to_agent_id"],
            source_agent_id=kwargs["from_agent_id"],
            source_thread_id=kwargs.get("source_thread_id"),
            message=kwargs["message"],
            user_id=kwargs["user_id"],
            metadata=kwargs.get("metadata"),
        )

    team = _team(submit=submit, original=original)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: True,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    # A real long-lived Harness inbox worker starts outside request scope.
    inbox.start()
    assert current_ticket() is None
    token = activate_ticket(fixture.host, issue_execution_ticket(fixture.host, fixture.actor))
    try:
        with pytest.raises(PluginHostError):
            await team.call_peer(
                from_agent_id="parent",
                to_agent_id="child",
                message="delegate",
                user_id=fixture.actor.user_id,
                source_thread_id=fixture.thread_id,
            )
    finally:
        reset_ticket(token)
        await inbox.shutdown()
    assert inbox.list() == []
    assert child_calls == []


async def test_unbound_dashboard_parent_dispatch_clears_ticket_from_child(
    delegation_fixture: Any,
) -> None:
    fixture = delegation_fixture
    observed: list[Any] = []

    async def original(**_kwargs: Any) -> str:
        observed.append(current_ticket())
        return "child result"

    team = _team(submit=lambda **_kwargs: "queued", original=original)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    token = activate_ticket(fixture.host, issue_execution_ticket(fixture.host, fixture.actor))
    try:
        result = await team.call_peer(
            from_agent_id="parent",
            to_agent_id="child",
            message="ordinary team",
            user_id=fixture.actor.user_id,
            source_thread_id=fixture.thread_id,
        )
    finally:
        reset_ticket(token)
    assert result == "child result"
    assert observed == [None]


async def test_ticketed_first_inbox_worker_does_not_taint_later_unbound_turn(
    delegation_fixture: Any,
) -> None:
    import asyncio

    from octop_harness.teams.inbox import HarnessAgentInboxManager

    fixture = delegation_fixture
    observed: list[Any] = []
    finished = asyncio.Event()

    async def invoke_target(_message: Any) -> dict[str, Any]:
        observed.append(current_ticket())
        finished.set()
        return {"ok": True}

    inbox = HarnessAgentInboxManager(
        call_agent=lambda _agent, _request: _async_result({"ok": True}),
        processor=object(),
        invoke_target=invoke_target,
    )

    def submit(**kwargs: Any) -> str:
        return inbox.enqueue(
            target_agent_id=kwargs["to_agent_id"],
            source_agent_id=kwargs["from_agent_id"],
            source_thread_id=kwargs.get("source_thread_id"),
            message=kwargs["message"],
            user_id=kwargs["user_id"],
            metadata=kwargs.get("metadata"),
        )

    async def original(**_kwargs: Any) -> str:
        return "direct"

    team = _team(submit=submit, original=original)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: True,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    token = activate_ticket(fixture.host, issue_execution_ticket(fixture.host, fixture.actor))
    try:
        await team.call_peer(
            from_agent_id="parent",
            to_agent_id="child",
            message="ordinary first job",
            user_id=fixture.actor.user_id,
            source_thread_id=fixture.thread_id,
        )
    finally:
        reset_ticket(token)
    await asyncio.wait_for(finished.wait(), timeout=2)
    # A later ordinary dispatch shares the same inbox worker/context.
    await team.call_peer(
        from_agent_id="parent",
        to_agent_id="child",
        message="ordinary later job",
        user_id=fixture.actor.user_id,
        source_thread_id=fixture.thread_id,
    )
    await asyncio.sleep(0.02)
    await inbox.shutdown()
    assert observed == [None, None]


async def _async_result(value: dict[str, Any]) -> dict[str, Any]:
    return value


async def _record_ticket(observed: list[Any]) -> dict[str, Any]:
    observed.append(current_ticket())
    return {"ok": True}


async def test_child_bound_peer_fails_closed_without_parent_ticket(
    delegation_fixture: Any,
) -> None:
    fixture = delegation_fixture
    child_facade = fixture.host.bind("child-plugin", "1.0")
    child_actor = PluginActor(fixture.actor.user_id, True, agent_id="child")
    await child_facade.commit(
        [],
        requirements=(RequiredExtensionWrite(child_actor, "child", {"child-plugin": "1.0"}, 0),),
    )
    calls: list[str] = []

    async def original(**_kwargs: Any) -> str:
        calls.append("called")
        return "child result"

    team = _team(submit=lambda **_kwargs: "queued", original=original)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    with pytest.raises(PluginHostError):
        await team.call_peer(
            from_agent_id="parent",
            to_agent_id="child",
            message="delegate",
            user_id=fixture.actor.user_id,
        )
    assert calls == []


async def test_real_ask_agent_middleware_permits_sequential_unbound_threads(
    delegation_fixture: Any,
) -> None:
    import asyncio

    from langchain.agents.middleware.types import ToolCallRequest
    from octop_harness.teams.inbox import HarnessAgentInboxManager
    from octop_harness.teams.team_manager import AgentEntry, AgentRegistry, TeamManager
    from octop_harness.teams.tools import build_team_tools

    from octop.infra.agents.teams.team_manager import PeerDispatchAuthorizationMiddleware

    fixture = delegation_fixture
    registry = AgentRegistry()
    for agent_id, display in (("parent", "Parent"), ("child", "Child")):
        registry.add(
            AgentEntry(
                agent_id=agent_id,
                agent=object(),
                config=SimpleNamespace(name=display, team_peers=None, peer_invoke_mode="async"),
                metadata={"user_id": str(fixture.actor.user_id), "name": display},
                tags=[],
                created_at=datetime.now(UTC),
            )
        )
    team = TeamManager(registry)
    finished: list[str] = []
    events: dict[str, asyncio.Event] = {}

    async def invoke_target(message: Any) -> dict[str, Any]:
        finished.append(str(message.source_thread_id))
        events[str(message.source_thread_id)].set()
        return {"ok": True}

    team._inbox = HarnessAgentInboxManager(
        call_agent=lambda _agent, _request: _async_result({"ok": True}),
        processor=object(),
        invoke_target=invoke_target,
    )
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    tool = next(
        tool
        for tool in build_team_tools(team, peer_invoke_mode="async")
        if tool.name == "ask_agent"
    )
    middleware = PeerDispatchAuthorizationMiddleware(
        team,
        fixture.host.authorize_peer_dispatch,
    )

    async def dispatch(thread_id: str) -> None:
        events[thread_id] = asyncio.Event()
        actor = PluginActor(fixture.actor.user_id, True, agent_id="parent", thread_id=thread_id)
        ticket = issue_execution_ticket(fixture.host, actor)
        token = activate_ticket(fixture.host, ticket)
        config = {
            "configurable": {
                "agent_id": "parent",
                "user": fixture.actor.user_id,
                "thread_id": thread_id,
                "session_key": f"parent:dashboard:{fixture.actor.user_id}:{thread_id}",
            }
        }
        request = ToolCallRequest(
            tool_call={
                "id": f"ask-{thread_id}",
                "name": "ask_agent",
                "args": {"expert": "child", "message": f"on {thread_id}", "mode": "background"},
            },
            tool=tool,
            state={},
            runtime=SimpleNamespace(config=config),
        )

        async def handler(req: Any) -> Any:
            return await tool.ainvoke(req.tool_call["args"], config=config)

        try:
            await middleware.awrap_tool_call(request, handler)
        finally:
            reset_ticket(token)
        await asyncio.wait_for(events[thread_id].wait(), timeout=2)

    await dispatch(fixture.thread_id)
    second_thread = await fixture.registry.get_or_create_by_key(
        session_key="parent:dashboard:owner:thread-b",
        agent_id="parent",
        user_id=fixture.actor.user_id,
        channel_type="dashboard",
    )
    await dispatch(second_thread)
    await team.inbox.shutdown()
    assert finished == [fixture.thread_id, second_thread]


async def test_real_bare_submit_peer_without_permit_does_not_enqueue(
    delegation_fixture: Any,
) -> None:
    from octop_harness.teams.inbox import HarnessAgentInboxManager
    from octop_harness.teams.team_manager import AgentEntry, AgentRegistry, TeamManager

    fixture = delegation_fixture
    await _bind_parent(fixture)
    registry = AgentRegistry()
    for agent_id in ("parent", "child"):
        registry.add(
            AgentEntry(
                agent_id=agent_id,
                agent=object(),
                config=SimpleNamespace(name=agent_id, team_peers=None, peer_invoke_mode="async"),
                metadata={"user_id": str(fixture.actor.user_id), "name": agent_id},
                tags=[],
                created_at=datetime.now(UTC),
            )
        )
    team = TeamManager(registry)
    team._inbox = HarnessAgentInboxManager(
        call_agent=lambda _agent, _request: _async_result({"ok": True}),
        processor=object(),
        invoke_target=lambda _message: _async_result({"ok": True}),
    )
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    with pytest.raises(PluginHostError):
        team.submit_peer(
            from_agent_id="parent",
            to_agent_id="child",
            message="bare",
            user_id=fixture.actor.user_id,
            source_thread_id=fixture.thread_id,
        )
    assert team.inbox.list() == []
    await team.inbox.shutdown()


async def test_peer_requirement_is_scoped_to_known_source_thread(
    delegation_fixture: Any,
) -> None:
    fixture = delegation_fixture
    thread_t1 = await fixture.registry.get_or_create_by_key(
        session_key="parent:dashboard:owner:t1",
        agent_id="parent",
        user_id=fixture.actor.user_id,
        channel_type="dashboard",
    )
    thread_t2 = await fixture.registry.get_or_create_by_key(
        session_key="parent:dashboard:owner:t2",
        agent_id="parent",
        user_id=fixture.actor.user_id,
        channel_type="dashboard",
    )
    facade = fixture.host.facade("parent-plugin")
    actor_t1 = PluginActor(fixture.actor.user_id, True, agent_id="parent", thread_id=thread_t1)
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(actor_t1, "parent", {"parent-plugin": "1.0"}, 0, thread_t1),
        ),
    )
    actor_t2 = PluginActor(fixture.actor.user_id, True, agent_id="parent", thread_id=thread_t2)
    ticket = issue_execution_ticket(fixture.host, actor_t2)
    await fixture.host.authorize_peer_dispatch(
        ticket,
        from_agent_id="parent",
        to_agent_id="child",
        source_thread_id=thread_t2,
    )


async def test_ask_agent_permit_is_target_bound_single_use_and_cleared_on_error(
    delegation_fixture: Any,
) -> None:
    from langchain.agents.middleware.types import ToolCallRequest
    from octop_harness.teams.inbox import HarnessAgentInboxManager
    from octop_harness.teams.team_manager import AgentEntry, AgentRegistry, TeamManager
    from octop_harness.teams.tools import build_team_tools

    from octop.infra.agents.teams.team_manager import PeerDispatchAuthorizationMiddleware

    fixture = delegation_fixture
    registry = AgentRegistry()
    for agent_id in ("parent", "child", "sibling"):
        registry.add(
            AgentEntry(
                agent_id=agent_id,
                agent=object(),
                config=SimpleNamespace(name=agent_id, team_peers=None, peer_invoke_mode="async"),
                metadata={"user_id": str(fixture.actor.user_id), "name": agent_id},
                tags=[],
                created_at=datetime.now(UTC),
            )
        )
    team = TeamManager(registry)
    team._inbox = HarnessAgentInboxManager(
        call_agent=lambda _agent, _request: _async_result({"ok": True}),
        processor=object(),
        invoke_target=lambda _message: _async_result({"ok": True}),
    )
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    tool = next(
        tool
        for tool in build_team_tools(team, peer_invoke_mode="async")
        if tool.name == "ask_agent"
    )
    middleware = PeerDispatchAuthorizationMiddleware(team, fixture.host.authorize_peer_dispatch)
    config = {
        "configurable": {
            "agent_id": "parent",
            "user": fixture.actor.user_id,
            "thread_id": fixture.thread_id,
        }
    }
    request = ToolCallRequest(
        tool_call={
            "id": "ask-target-bound",
            "name": "ask_agent",
            "args": {"expert": "child", "message": "task", "mode": "background"},
        },
        tool=tool,
        state={},
        runtime=SimpleNamespace(config=config),
    )
    ticket = issue_execution_ticket(fixture.host, fixture.actor)
    token = activate_ticket(fixture.host, ticket)

    async def handler(_request: Any) -> None:
        with pytest.raises(PluginHostError):
            team.submit_peer(
                from_agent_id="parent",
                to_agent_id="sibling",
                message="wrong target",
                user_id=fixture.actor.user_id,
                source_thread_id=fixture.thread_id,
            )
        team.submit_peer(
            from_agent_id="parent",
            to_agent_id="child",
            message="authorized once",
            user_id=fixture.actor.user_id,
            source_thread_id=fixture.thread_id,
        )
        team.submit_peer(
            from_agent_id="parent",
            to_agent_id="child",
            message="duplicate",
            user_id=fixture.actor.user_id,
            source_thread_id=fixture.thread_id,
        )

    try:
        with pytest.raises(PluginHostError):
            await middleware.awrap_tool_call(request, handler)
        assert current_ticket() is ticket
        with pytest.raises(PluginHostError):
            team.submit_peer(
                from_agent_id="parent",
                to_agent_id="child",
                message="outside middleware",
                user_id=fixture.actor.user_id,
                source_thread_id=fixture.thread_id,
            )
        assert len(team.inbox.list()) == 1
    finally:
        reset_ticket(token)
        await team.inbox.shutdown()


async def test_inbox_fallback_rechecks_bound_source_before_harness_call(
    delegation_fixture: Any,
) -> None:
    from octop_harness.teams.inbox import HarnessAgentInboxManager
    from octop_harness.teams.team_manager import AgentEntry, AgentRegistry, TeamManager

    fixture = delegation_fixture
    await _bind_parent(fixture)
    calls: list[Any] = []

    async def parent_call(request: Any) -> dict[str, Any]:
        calls.append(request)
        return {"ok": True}

    registry = AgentRegistry()
    for agent_id, agent in (
        ("parent", SimpleNamespace(call=parent_call)),
        ("child", SimpleNamespace(call=parent_call)),
    ):
        registry.add(
            AgentEntry(
                agent_id=agent_id,
                agent=agent,
                config=SimpleNamespace(name=agent_id, team_peers=None, peer_invoke_mode="async"),
                metadata={"user_id": str(fixture.actor.user_id), "name": agent_id},
                tags=[],
                created_at=datetime.now(UTC),
            )
        )
    team = TeamManager(registry)
    team._inbox = HarnessAgentInboxManager(
        call_agent=parent_call,
        processor=object(),
        invoke_target=lambda _message: _async_result({"ok": True}),
    )

    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    with pytest.raises(PluginHostError):
        await team._call_agent(
            "parent",
            {"source": "inbox", "thread_id": fixture.thread_id, "agent_id": "parent"},
        )
    assert calls == []
    await team.inbox.shutdown()


async def test_real_fallback_adapter_uses_exact_target_thread_scope(
    delegation_fixture: Any,
) -> None:
    from octop_harness.teams.team_manager import AgentEntry, AgentRegistry, TeamManager

    fixture = delegation_fixture
    thread_t1 = await fixture.registry.get_or_create_by_key(
        session_key="parent:dashboard:owner:fallback-t1",
        agent_id="parent",
        user_id=fixture.actor.user_id,
        channel_type="dashboard",
    )
    thread_t2 = await fixture.registry.get_or_create_by_key(
        session_key="parent:dashboard:owner:fallback-t2",
        agent_id="parent",
        user_id=fixture.actor.user_id,
        channel_type="dashboard",
    )
    facade = fixture.host.facade("parent-plugin")
    actor_t1 = PluginActor(fixture.actor.user_id, True, agent_id="parent", thread_id=thread_t1)
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(actor_t1, "parent", {"parent-plugin": "1.0"}, 0, thread_t1),
        ),
    )

    calls: list[str | None] = []

    async def parent_call(request: Any) -> dict[str, Any]:
        calls.append(getattr(request, "thread_id", None))
        return {"ok": True}

    registry = AgentRegistry()
    registry.add(
        AgentEntry(
            agent_id="parent",
            agent=SimpleNamespace(call=parent_call),
            config=SimpleNamespace(name="parent", team_peers=None, peer_invoke_mode="async"),
            metadata={"user_id": str(fixture.actor.user_id), "name": "parent"},
            tags=[],
            created_at=datetime.now(UTC),
        )
    )
    team = TeamManager(registry)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )

    # A declaration on T1 does not authorize or deny the distinct fallback on T2.
    await team._call_agent(
        "parent",
        {
            "source": "inbox",
            "thread_id": thread_t2,
            "agent_id": "parent",
            "messages": "fallback",
        },
    )
    assert calls == [thread_t2]

    # The same real adapter must still reject its bound T1 target before Harness.
    with pytest.raises(PluginHostError):
        await team._call_agent(
            "parent",
            {
                "source": "inbox",
                "thread_id": thread_t1,
                "agent_id": "parent",
                "messages": "fallback",
            },
        )
    assert calls == [thread_t2]


async def test_real_fallback_adapter_rejects_agent_scope_and_unknown_thread(
    delegation_fixture: Any,
) -> None:
    from octop_harness.teams.team_manager import AgentEntry, AgentRegistry, TeamManager

    fixture = delegation_fixture
    facade = fixture.host.facade("parent-plugin")
    actor = PluginActor(fixture.actor.user_id, True, agent_id="parent")
    await facade.commit(
        [],
        requirements=(RequiredExtensionWrite(actor, "parent", {"parent-plugin": "1.0"}, 0),),
    )
    calls: list[Any] = []

    async def parent_call(request: Any) -> dict[str, Any]:
        calls.append(request)
        return {"ok": True}

    registry = AgentRegistry()
    registry.add(
        AgentEntry(
            agent_id="parent",
            agent=SimpleNamespace(call=parent_call),
            config=SimpleNamespace(name="parent", team_peers=None, peer_invoke_mode="async"),
            metadata={"user_id": str(fixture.actor.user_id), "name": "parent"},
            tags=[],
            created_at=datetime.now(UTC),
        )
    )
    team = TeamManager(registry)
    wire_host_dispatch(
        team,
        is_team=lambda _agent_id: False,
        authorize_peer_dispatch=fixture.host.authorize_peer_dispatch,
    )
    for request in (
        {"source": "inbox", "thread_id": fixture.thread_id, "agent_id": "parent"},
        {"source": "inbox", "agent_id": "parent"},
    ):
        with pytest.raises(PluginHostError):
            await team._call_agent("parent", request)
    assert calls == []
