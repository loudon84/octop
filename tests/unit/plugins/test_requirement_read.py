from __future__ import annotations

import json

import pytest

from octop.config import OctopConfig
from octop.infra.agents.plugins.execution import issue_execution_ticket
from octop.infra.agents.plugins.host import PluginHost
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
    PluginHostError,
    PluginRequirementReadiness,
    RequiredExtensionWrite,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def plugin_services(tmp_path):
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


async def _persist_requirement(services, host, actor, agent_id, required, thread_id=None):
    declarer = host.bind("declarer", "1.0.0")
    for plugin_id, version in required.items():
        if plugin_id != "declarer":
            host.bind(plugin_id, version)
    await declarer.commit(
        [],
        requirements=(RequiredExtensionWrite(actor, agent_id, required, 0, thread_id),),
    )


async def test_requirement_read_merges_agent_and_own_thread_and_is_immutable(plugin_services):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    employee = UserRepo(services.db).create(username="employee", role="user")
    services.agent_repo.create(
        agent_id="shared-agent",
        user_id=owner,
        name="Shared",
        config_json='{"plugins":{"alpha":{"enabled":false}}}',
    )
    services.agent_repo.set_shared("shared-agent", True)
    services.thread_repo.insert(
        thread_id="employee-thread",
        agent_id="shared-agent",
        user_id=employee,
        channel_type="dashboard",
        session_key="employee-session",
    )
    services.thread_repo.insert(
        thread_id="another-thread",
        agent_id="shared-agent",
        user_id=employee,
        channel_type="dashboard",
        session_key="another-session",
    )
    host = PluginHost(lambda: services)
    owner_actor = PluginActor(owner, True, agent_id="shared-agent")
    await _persist_requirement(
        services,
        host,
        owner_actor,
        "shared-agent",
        {"alpha": "1.0", "declarer": "1.0.0", "wrong-version": "1.0"},
    )
    host.bind("wrong-version", "2.0")

    # Thread declarations are normally owner-managed. Seed the canonical reserved record
    # for this employee-owned thread to exercise its read scope without changing that policy.
    thread_key = json.dumps(["shared-agent", "employee-thread", "declarer"], separators=(",", ":"))
    services.plugin_record_repo.cas(
        "@host",
        "required_extensions",
        thread_key,
        owner_user_id=owner,
        payload_json=json.dumps(
            {
                "actor_user_id": owner,
                "agent_id": "shared-agent",
                "thread_id": "employee-thread",
                "declaring_plugin_id": "declarer",
                "required_plugins": {"beta": "2.0"},
                "enabled": True,
            },
            separators=(",", ":"),
        ),
        expected_revision=0,
    )
    other_thread_key = json.dumps(
        ["shared-agent", "another-thread", "declarer"], separators=(",", ":")
    )
    services.plugin_record_repo.cas(
        "@host",
        "required_extensions",
        other_thread_key,
        owner_user_id=owner,
        payload_json=json.dumps(
            {
                "actor_user_id": owner,
                "agent_id": "shared-agent",
                "thread_id": "another-thread",
                "declaring_plugin_id": "declarer",
                "required_plugins": {"gamma": "3.0"},
                "enabled": True,
            },
            separators=(",", ":"),
        ),
        expected_revision=0,
    )

    actor = PluginActor(employee, False, agent_id="shared-agent", thread_id="employee-thread")
    record_count = len(services.plugin_record_repo.list_prefix("@host", "required_extensions", ""))
    readiness = await host.read_requirements(actor)
    assert dict(readiness.required_plugins) == {
        "beta": "2.0",
        "declarer": "1.0.0",
        "alpha": "1.0",
        "wrong-version": "1.0",
    }
    assert readiness.unavailable_plugins == ("alpha", "beta", "wrong-version")
    with pytest.raises(TypeError):
        readiness.required_plugins["changed"] = "3.0"

    agent_only = await host.read_requirements(PluginActor(employee, False, agent_id="shared-agent"))
    assert dict(agent_only.required_plugins) == {
        "alpha": "1.0",
        "declarer": "1.0.0",
        "wrong-version": "1.0",
    }
    another_thread = await host.read_requirements(
        PluginActor(employee, False, agent_id="shared-agent", thread_id="another-thread")
    )
    assert dict(another_thread.required_plugins) == {
        "alpha": "1.0",
        "declarer": "1.0.0",
        "gamma": "3.0",
        "wrong-version": "1.0",
    }
    assert (
        len(services.plugin_record_repo.list_prefix("@host", "required_extensions", ""))
        == record_count
    )


async def test_requirement_read_refreshes_auth_and_rejects_bad_scope(plugin_services):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    employee = UserRepo(services.db).create(username="employee", role="user")
    services.agent_repo.create(
        agent_id="shared-agent",
        user_id=owner,
        name="Shared",
        config_json='{"plugins":{"alpha":{"enabled":false}}}',
    )
    services.agent_repo.set_shared("shared-agent", True)
    services.thread_repo.insert(
        thread_id="employee-thread",
        agent_id="shared-agent",
        user_id=employee,
        channel_type="dashboard",
        session_key="employee-session",
    )
    services.agent_repo.create(agent_id="private-agent", user_id=owner, name="Private")
    services.thread_repo.insert(
        thread_id="mismatched-thread",
        agent_id="private-agent",
        user_id=employee,
        channel_type="dashboard",
        session_key="mismatched-session",
    )
    host = PluginHost(lambda: services)
    actor = PluginActor(employee, False, agent_id="shared-agent", thread_id="employee-thread")
    assert dict((await host.read_requirements(actor)).required_plugins) == {}

    services.user_repo.set_disabled(employee, True)
    with pytest.raises(PluginHostError):
        await host.read_requirements(actor)
    services.user_repo.set_disabled(employee, False)

    for invalid_actor in (
        PluginActor(employee, False, agent_id="shared-agent", thread_id="foreign-thread"),
        PluginActor(employee, False, agent_id="shared-agent", thread_id="mismatched-thread"),
        PluginActor(employee, False, agent_id="shared-agent", thread_id=""),
        PluginActor(employee, False, agent_id=""),
        PluginActor(employee, False, agent_id="private-agent"),
        PluginActor(employee, True, agent_id="private-agent"),
    ):
        with pytest.raises(PluginHostError):
            await host.read_requirements(invalid_actor)
    with pytest.raises(PluginHostError) as pending_error:
        await host.read_requirements(
            PluginActor(employee, False, agent_id="shared-agent", thread_id="pending")
        )
    assert pending_error.value.code == "not_found"


async def test_requirement_read_fails_closed_on_malformed_record(plugin_services):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    services.plugin_record_repo.cas(
        "@host",
        "required_extensions",
        json.dumps(["agent-1", None, "broken"], separators=(",", ":")),
        owner_user_id=owner,
        payload_json='{"agent_id":"agent-1","thread_id":null,"declaring_plugin_id":"broken","required_plugins":{"bad id":"1"}}',
        expected_revision=0,
    )
    host = PluginHost(lambda: services)
    with pytest.raises(PluginHostError) as invalid_plugin_id:
        await host.read_requirements(PluginActor(owner, True, agent_id="agent-1"))
    assert invalid_plugin_id.value.code == "unavailable"


def test_requirement_readiness_copies_mutable_inputs():
    required = {"plugin-a": "1.0"}
    unavailable = ["plugin-a"]
    result = PluginRequirementReadiness(required, unavailable)

    required["plugin-b"] = "2.0"
    unavailable.append("plugin-b")

    assert dict(result.required_plugins) == {"plugin-a": "1.0"}
    assert result.unavailable_plugins == ("plugin-a",)
    with pytest.raises(TypeError):
        result.required_plugins["plugin-c"] = "3.0"


@pytest.mark.parametrize(
    "payload_json",
    [
        "not-json",
        '{"agent_id":"agent-1","thread_id":null,"declaring_plugin_id":"broken","required_plugins":[]}',
        '{"agent_id":"agent-1","thread_id":null,"declaring_plugin_id":"broken","required_plugins":{"valid-plugin":[]}}',
        '{"agent_id":"agent-1","thread_id":4,"declaring_plugin_id":"broken","required_plugins":{}}',
    ],
)
async def test_requirement_read_rejects_corrupt_records(plugin_services, payload_json):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    services.plugin_record_repo.cas(
        "@host",
        "required_extensions",
        '["agent-1",null,"broken"]',
        owner_user_id=owner,
        payload_json=payload_json,
        expected_revision=0,
    )
    host = PluginHost(lambda: services)
    with pytest.raises(PluginHostError):
        await host.read_requirements(PluginActor(owner, True, agent_id="agent-1"))


async def test_requirement_read_reports_storage_failure_as_unavailable(
    plugin_services, monkeypatch
):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")

    def fail_list_prefix(*_args, **_kwargs):
        raise RuntimeError("private storage failure")

    monkeypatch.setattr(services.plugin_record_repo, "list_prefix", fail_list_prefix)
    host = PluginHost(lambda: services)
    with pytest.raises(PluginHostError) as error:
        await host.read_requirements(PluginActor(owner, True, agent_id="agent-1"))
    assert error.value.code == "unavailable"


async def test_requirement_read_rejects_conflicting_exact_versions(plugin_services):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    for declaring_plugin, version in (("decl-a", "1.0"), ("decl-b", "2.0")):
        key = json.dumps(["agent-1", None, declaring_plugin], separators=(",", ":"))
        payload = {
            "agent_id": "agent-1",
            "thread_id": None,
            "declaring_plugin_id": declaring_plugin,
            "required_plugins": {"shared-plugin": version},
        }
        services.plugin_record_repo.cas(
            "@host",
            "required_extensions",
            key,
            owner_user_id=owner,
            payload_json=json.dumps(payload, separators=(",", ":")),
            expected_revision=0,
        )
    host = PluginHost(lambda: services)
    with pytest.raises(PluginHostError) as error:
        await host.read_requirements(PluginActor(owner, True, agent_id="agent-1"))
    assert error.value.code == "unavailable"


async def test_existing_requirement_write_authorization_remains_owner_only(plugin_services):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    employee = UserRepo(services.db).create(username="employee", role="user")
    services.agent_repo.create(
        agent_id="shared-agent",
        user_id=owner,
        name="Shared",
        config_json='{"plugins":{"alpha":{"enabled":false}}}',
    )
    services.agent_repo.set_shared("shared-agent", True)
    host = PluginHost(lambda: services)
    declarer = host.bind("declarer", "1.0")
    actor = PluginActor(employee, False, frozenset({"agent_plugins"}), agent_id="shared-agent")
    with pytest.raises(PluginHostError):
        await declarer.commit(
            [],
            requirements=(RequiredExtensionWrite(actor, "shared-agent", {}, 0),),
        )
    assert services.plugin_record_repo.list_prefix("@host", "required_extensions", "") == []


async def test_final_execution_gate_rechecks_after_readiness_changes(plugin_services):
    services = plugin_services
    owner = UserRepo(services.db).create(username="owner", role="admin")
    services.agent_repo.create(agent_id="agent-1", user_id=owner, name="Agent")
    services.thread_repo.insert(
        thread_id="thread-1",
        agent_id="agent-1",
        user_id=owner,
        channel_type="dashboard",
        session_key="session-1",
    )
    host = PluginHost(lambda: services)
    facade = host.bind("race-plugin", "1.0")
    actor = PluginActor(owner, True, agent_id="agent-1", thread_id="thread-1")
    await facade.commit(
        [],
        requirements=(
            RequiredExtensionWrite(actor, "agent-1", {"race-plugin": "1.0"}, 0, "thread-1"),
        ),
    )
    assert (await host.read_requirements(actor)).unavailable_plugins == ()
    host.deactivate("race-plugin")
    ticket = issue_execution_ticket(host, actor)
    with pytest.raises(PluginHostError):
        await host.authorize_execution(ticket, agent_id="agent-1", thread_id="thread-1")
