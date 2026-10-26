from __future__ import annotations

import pytest

from octop.config import OctopConfig
from octop.infra.agents.plugins.execution import issue_execution_ticket
from octop.infra.agents.plugins.host import PluginHost
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
    PluginHostError,
    RecordWrite,
    RequiredExtensionWrite,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


async def test_requirement_write_is_atomic_and_kept_out_of_plugin_records(services) -> None:
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    host = PluginHost(lambda: services)
    facade = host.bind("binding-plugin", "2.0")
    actor = PluginActor(owner, True, frozenset(), agent_id="agent-1")
    requirement = RequiredExtensionWrite(
        actor=actor,
        agent_id="agent-1",
        required_plugins={"binding-plugin": "2.0"},
        expected_revision=0,
    )

    services.plugin_record_repo.cas(
        "binding-plugin",
        "binding",
        "main",
        owner_user_id=owner,
        payload_json='{"bound":false}',
        expected_revision=0,
    )
    with pytest.raises(PluginHostError):
        await facade.commit(
            [RecordWrite("binding", "main", owner, {"bound": True}, 0)],
            requirements=(requirement,),
        )

    assert len(services.plugin_record_repo.list_prefix("binding-plugin", "binding", "")) == 1
    assert (await facade.get_requirement(actor, "agent-1")).revision == 0
    with pytest.raises(PluginHostError):
        await facade.get_record("required_extensions", "x")


async def test_bound_execution_requires_ticket_and_reloads_permissions(services) -> None:
    owner = UserRepo(services.db).create(
        username="owner",
        role="user",
        permissions=["agent_plugins", "search"],
    )
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    registry = ThreadRegistry(
        session_repo=services.session_repo,
        thread_repo=services.thread_repo,
    )
    thread_id = await registry.get_or_create_by_key(
        session_key="agent-1:dashboard:owner",
        agent_id="agent-1",
        user_id=owner,
        channel_type="dashboard",
    )
    host = PluginHost(lambda: services)
    facade = host.bind("binding-plugin", "2.0")
    actor = PluginActor(
        owner,
        False,
        frozenset({"agent_plugins", "search"}),
        agent_id="agent-1",
        thread_id=thread_id,
    )
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                actor,
                "agent-1",
                {"binding-plugin": "2.0"},
                0,
                thread_id,
            ),
        ),
    )
    with pytest.raises(PluginHostError):
        await host.authorize_execution(None, agent_id="agent-1", thread_id=thread_id)

    from octop.infra.agents.plugins.execution import activate_ticket, reset_ticket

    ticket = issue_execution_ticket(
        host,
        actor,
        plugin_context={"binding-plugin": {"query": "private prompt"}},
    )
    await host.authorize_execution(ticket, agent_id="agent-1", thread_id=thread_id)
    scope_token = activate_ticket(host, ticket)
    try:
        execution = await facade.current_execution()
        assert execution.context == {"query": "private prompt"}
        execution.context["query"] = "changed"
        assert (await facade.current_execution()).context == {"query": "private prompt"}
        services.user_repo.set_permissions(owner, [])
        refreshed = await facade.current_execution()
        assert refreshed.actor.permissions == frozenset()
        services.user_repo.set_disabled(owner, True)
        with pytest.raises(PluginHostError):
            await facade.current_execution()
    finally:
        reset_ticket(scope_token)
    with pytest.raises(PluginHostError):
        await facade.current_execution()


async def test_optional_execution_only_treats_absent_ticket_as_absence(services) -> None:
    owner = UserRepo(services.db).create(username="optional", role="admin")
    services.agent_repo.create(agent_id="agent-optional", user_id=owner, name="Agent")
    registry = ThreadRegistry(
        session_repo=services.session_repo,
        thread_repo=services.thread_repo,
    )
    thread_id = await registry.get_or_create_by_key(
        session_key="agent-optional:dashboard:optional",
        agent_id="agent-optional",
        user_id=owner,
        channel_type="dashboard",
    )
    actor = PluginActor(owner, True, agent_id="agent-optional", thread_id=thread_id)
    host = PluginHost(lambda: services)
    facade = host.bind("optional-plugin", "1.0")
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                actor,
                "agent-optional",
                {"optional-plugin": "1.0"},
                0,
                thread_id,
            ),
        ),
    )

    assert await facade.current_execution(optional=True) is None
    with pytest.raises(PluginHostError):
        await facade.current_execution()
    host.bind("optional-plugin", "1.0")
    with pytest.raises(PluginHostError):
        await facade.current_execution(optional=True)
    facade = host.facade("optional-plugin")

    from octop.infra.agents.plugins.execution import activate_ticket, reset_ticket

    ticket = issue_execution_ticket(host, actor)
    token = activate_ticket(host, ticket)
    try:
        execution = await facade.current_execution(optional=True)
        assert execution is not None
        assert dict(execution.required_plugins) == {"optional-plugin": "1.0"}
        services.user_repo.set_disabled(owner, True)
        with pytest.raises(PluginHostError):
            await facade.current_execution(optional=True)
    finally:
        reset_ticket(token)

    foreign = PluginHost(lambda: services).bind("optional-plugin", "1.0")
    token = activate_ticket(host, ticket)
    try:
        with pytest.raises(PluginHostError):
            await foreign.current_execution(optional=True)
    finally:
        reset_ticket(token)


def test_execution_required_plugins_is_a_copied_immutable_mapping() -> None:
    from octop.infra.agents.plugins.host_contracts import PluginExecution

    source = {"provider": "1.2.3"}
    execution = PluginExecution(PluginActor(1, True), {}, "turn", source)
    legacy = PluginExecution(PluginActor(1, True), {}, "turn")
    source["provider"] = "changed"

    assert dict(execution.required_plugins) == {"provider": "1.2.3"}
    assert dict(legacy.required_plugins) == {}
    with pytest.raises(TypeError):
        execution.required_plugins["other"] = "1.0"  # type: ignore[index]


async def test_requirement_state_survives_host_rebuild_and_clear_advances_revision(
    services,
) -> None:
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    actor = PluginActor(owner, True, agent_id="agent-1")
    host = PluginHost(lambda: services)
    facade = host.bind("binding-plugin", "2.0")
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                actor,
                "agent-1",
                {"binding-plugin": "2.0"},
                0,
            ),
        ),
    )

    restarted = PluginHost(lambda: services)
    restarted_facade = restarted.bind("binding-plugin", "2.0")
    state = await restarted_facade.get_requirement(actor, "agent-1")
    assert state.revision == 1
    assert dict(state.required_plugins) == {"binding-plugin": "2.0"}
    await restarted_facade.commit(
        [],
        requirements=(RequiredExtensionWrite(actor, "agent-1", {}, state.revision),),
    )
    cleared = await restarted_facade.get_requirement(actor, "agent-1")
    assert cleared.revision == 2
    assert dict(cleared.required_plugins) == {}


async def test_requirement_change_does_not_trust_actor_admin_snapshot(services) -> None:
    owner = UserRepo(services.db).create(username="owner", role="user")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    host = PluginHost(lambda: services)
    facade = host.bind("binding-plugin", "2.0")
    forged_admin = PluginActor(owner, True, agent_id="agent-1")
    requirement = RequiredExtensionWrite(
        forged_admin,
        "agent-1",
        {"binding-plugin": "2.0"},
        0,
    )
    with pytest.raises(PluginHostError):
        await facade.commit([], requirements=(requirement,))
    with pytest.raises(PluginHostError):
        await facade.get_requirement(forged_admin, "agent-1")


async def test_deactivating_required_generation_cancels_scoped_turn(services) -> None:
    import asyncio

    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    registry = ThreadRegistry(
        session_repo=services.session_repo,
        thread_repo=services.thread_repo,
    )
    thread_id = await registry.get_or_create_by_key(
        session_key="agent-1:dashboard:owner",
        agent_id="agent-1",
        user_id=owner,
        channel_type="dashboard",
    )
    host = PluginHost(lambda: services)
    host.bind("binding-plugin", "2.0")
    actor = PluginActor(owner, True, agent_id="agent-1", thread_id=thread_id)
    facade = host.facade("binding-plugin")
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(actor, "agent-1", {"binding-plugin": "2.0"}, 0, thread_id),
        ),
    )
    ticket = issue_execution_ticket(host, actor)
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def turn() -> None:
        outer_lease = await host.begin_execution(ticket, agent_id="agent-1", thread_id=thread_id)
        inner_lease = await host.begin_execution(ticket, agent_id="agent-1", thread_id=thread_id)
        host.end_execution(inner_lease)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            host.end_execution(outer_lease)
            cancelled.set()

    task = asyncio.create_task(turn())
    await asyncio.wait_for(started.wait(), timeout=2)
    await host.deactivate_and_wait("binding-plugin")
    assert task.cancelled()
    assert cancelled.is_set()


async def test_begin_execution_does_not_partially_track_if_dependency_disappears(
    services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = PluginHost(lambda: services)
    host.bind("first-plugin", "1.0")

    async def stale_authorization(*_args, **_kwargs):
        return {"first-plugin": "1.0", "missing-plugin": "2.0"}

    monkeypatch.setattr(host, "authorize_execution", stale_authorization)
    with pytest.raises(PluginHostError):
        await host.begin_execution(None, agent_id="agent-1", thread_id="thread-1")
    first = host._plugins["first-plugin"]
    assert first.tasks == set()
    assert first.task_refs == {}
