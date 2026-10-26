from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest
from octop_harness.plugins import PluginRegistry

from octop.infra.agents.plugins.host import PluginHostFacade
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
    PluginHostError,
    RecordWrite,
    RequiredExtensionWrite,
)
from tests.support.auth import create_agent, create_user, resolve_user_id
from tests.support.enterprise import (
    install_enterprise_knowledge,
    install_synthetic_enterprise_policy,
)


def _session_source_revisions(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_id": snapshot["connection_id"],
        "config_revision": snapshot["config_revision"],
        "binding_revision": snapshot["binding_revision"],
        "grant_revision": snapshot["grant_revision"],
    }


async def test_http_connection_url_validation_precedes_commit_and_refresh_is_stable(env) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    rejected = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Encoded path",
                "base_url": "https://knowledge.example/a%23b",
                "api_key": "ENCODED_PATH_KEY",
            },
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "PLUGIN_ACTION_INVALID"
    assert await server.plugin_host.facade("enterprise-knowledge").list_records("connections") == []

    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Canonical URL",
                "base_url": "https://knowledge.example/api/v1/api/v1/",
                "api_key": "CANONICAL_URL_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    assert connection["base_url"] == "https://knowledge.example/api/v1"
    settings = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=admin_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["result"]["connections"][0]["base_url"] == connection["base_url"]


async def test_invalid_edit_key_is_rejected_before_recovery_or_credential_mutation(env) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Invalid key preflight",
                "base_url": "https://knowledge.example",
                "api_key": "PREFLIGHT_OLD_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    previous_intents = await facade.list_records("connection_intents")
    previous_recovery = await facade.list_records("connection_recovery")
    previous = await facade.get_record("connections", connection["connection_id"])
    assert previous is not None

    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection["connection_id"],
                "expected_revision": connection["revision"],
                "name": "Must not persist",
                "base_url": "https://knowledge.example/api/v1",
                "api_key": "",
            },
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert await facade.list_records("connection_intents") == previous_intents
    assert await facade.list_records("connection_recovery") == previous_recovery
    current = await facade.get_record("connections", connection["connection_id"])
    assert current is not None and current.revision == previous.revision
    assert current.payload == previous.payload


async def test_real_sdk_loaded_actions_use_current_authenticated_user(env, caplog) -> None:
    client, server, admin_auth = env
    plugin = await install_enterprise_knowledge(server)
    assert plugin.manifest.id == "enterprise-knowledge"
    assert PluginRegistry().get("enterprise-knowledge") is plugin

    actions = await client.get("/api/plugins/enterprise-knowledge/actions", headers=admin_auth)
    assert actions.status_code == 200
    names = {item["name"] for item in actions.json()["actions"]}
    assert names == {
        "settings.get",
        "connection.catalog",
        "connection.probe",
        "connection.save",
        "grant.save",
        "binding.get",
        "binding.save",
        "session.get",
        "session.save",
        "operation.prepare",
        "evidence.list",
        "evidence.read",
        "evidence.original",
        "provisioning.config.get",
        "provisioning.config.save",
        "provisioning.retained.list",
        "provisioning.status",
        "provisioning.retry",
        "provisioning.tasks.list",
        "provisioning.task.retry",
    }

    no_auth = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        json={"api_version": 1, "payload": {}},
    )
    assert no_auth.status_code == 401

    first = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Test knowledge",
                "base_url": "https://knowledge.example",
                "enabled": True,
                "api_key": "KNOWLEDGE_KEY_CANARY",
            },
        },
    )
    assert first.status_code == 200, first.text
    result = first.json()["result"]
    connection_id = result["connection"]["connection_id"]
    assert result["connection"]["probe_status"] == "CONFIGURED"
    assert result["connection"]["credential_configured"] is True
    assert "credential_ref" not in str(result)
    assert "KNOWLEDGE_KEY_CANARY" not in first.text
    assert "KNOWLEDGE_KEY_CANARY" not in caplog.text

    second = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "expected_revision": result["connection"]["revision"],
                "name": "Test knowledge updated",
                "base_url": "https://knowledge.example/api/v1",
                "enabled": True,
            },
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["result"]["connection"]["credential_configured"] is True

    user_auth = await create_user(client, admin_auth, username="knowledge-employee")
    user_id = await resolve_user_id(client, admin_auth, "knowledge-employee")
    denied_connection_manage = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=user_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Blocked",
                "base_url": "https://knowledge.example",
                "api_key": "EMPLOYEE_KEY_CANARY",
            },
        },
    )
    assert denied_connection_manage.status_code == 403
    assert "EMPLOYEE_KEY_CANARY" not in denied_connection_manage.text
    assert "EMPLOYEE_KEY_CANARY" not in caplog.text
    grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {"connection_id": connection_id, "user_id": user_id},
        },
    )
    assert grant.status_code == 200, grant.text
    assert grant.json()["result"]["grant"]["user_allowed_kb_ids"] == []

    binding_context = {"agent_id": "main"}
    disabled_binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": binding_context,
            "payload": {"expected_revision": 0},
        },
    )
    assert disabled_binding.status_code == 200, disabled_binding.text
    assert disabled_binding.json()["result"]["binding"]["revision"] == 1
    admin = server.user_manager.get_by_id(1)
    assert admin is not None
    assert server.plugin_host is not None
    requirement = await server.plugin_host.facade("enterprise-knowledge").get_requirement(
        PluginActor(admin.id, True, frozenset(admin.permissions), agent_id="main"), "main"
    )
    assert requirement.required_plugins == {}

    cannot_enable = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": binding_context,
            "payload": {
                "expected_revision": 1,
                "connection_ref": connection_id,
                "enabled": True,
            },
        },
    )
    assert cannot_enable.status_code == 409
    current_binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.get",
        headers=admin_auth,
        json={"api_version": 1, "context": binding_context, "payload": {}},
    )
    assert current_binding.status_code == 200
    assert current_binding.json()["result"]["binding"]["revision"] == 1
    assert current_binding.json()["result"]["binding"]["enabled"] is False

    denied = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=user_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert denied.status_code == 403

    settings = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=admin_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert settings.status_code == 200
    assert settings.json()["result"]["connections"][0]["connection_id"] == connection_id
    assert "KNOWLEDGE_KEY_CANARY" not in settings.text


async def test_authenticated_session_actions_preserve_empty_and_reject_unready_mode(env) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    thread_response = await client.post("/api/agents/main/threads", headers=admin_auth)
    assert thread_response.status_code == 201, thread_response.text
    thread_id = thread_response.json()["thread_id"]
    context = {"agent_id": "main", "thread_id": thread_id}

    first = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    assert first.status_code == 200, first.text
    snapshot = first.json()["result"]["session"]
    assert snapshot["revision"] == 0
    assert snapshot["selection_present"] is False
    assert snapshot["selected_kb_ids"] == []
    assert snapshot["mode"] == "KB_ONLY"
    assert snapshot["capabilities"] == {
        "can_save_selection": True,
        "can_select_knowledge": False,
        "knowledge_ready": False,
        "web_ready": False,
    }

    missing_snapshot = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_revision": 0, "selected_kb_ids": []},
        },
    )
    assert missing_snapshot.status_code == 400
    assert await server.plugin_host.facade("enterprise-knowledge").list_records("sessions") == []

    saved = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "mode": "KB_ONLY",
                "source_revisions": _session_source_revisions(snapshot),
            },
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["result"]["session"]["revision"] == 1
    assert saved.json()["result"]["session"]["selection_present"] is True

    refreshed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["result"]["session"]["selected_kb_ids"] == []
    assert refreshed.json()["result"]["session"]["selection_present"] is True

    web_only = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 1,
                "selected_kb_ids": [],
                "mode": "WEB_ONLY",
                "source_revisions": _session_source_revisions(
                    refreshed.json()["result"]["session"]
                ),
            },
        },
    )
    assert web_only.status_code == 409
    assert web_only.json()["error"]["code"] == "MODE_NOT_ALLOWED"

    session_key = json.dumps([1, "main", thread_id], separators=(",", ":"))
    facade = server.plugin_host.facade("enterprise-knowledge")
    await facade.commit(
        (
            RecordWrite(
                "sessions",
                session_key,
                1,
                {
                    "schema_id": "octop.enterprise.knowledge.v1",
                    "version": 1,
                    "user_id": 1,
                    "agent_id": "main",
                    "thread_id": thread_id,
                    "selected_kb_ids": [],
                    "mode": "WEB_ONLY",
                    "binding_revision": 0,
                    "config_revision": 0,
                    "grant_revision": 0,
                },
                1,
            ),
        )
    )
    legacy_web_only = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    assert legacy_web_only.status_code == 200, legacy_web_only.text
    web_snapshot = legacy_web_only.json()["result"]["session"]
    assert web_snapshot["mode"] == "WEB_ONLY"
    assert web_snapshot["invalid"] is True
    assert web_snapshot["search_ready"] is False
    assert web_snapshot["weknora_query_count"] == 0


async def test_session_initialization_cas_and_shared_agent_user_isolation(env, monkeypatch) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    assert server.services.agent_repo is not None
    server.services.agent_repo.set_shared("main", True)
    second_auth = await create_user(client, admin_auth, username="session-isolation-user")

    first_thread = (await client.post("/api/agents/main/threads", headers=admin_auth)).json()[
        "thread_id"
    ]
    second_thread_response = await client.post("/api/agents/main/threads", headers=second_auth)
    assert second_thread_response.status_code == 201, second_thread_response.text
    second_thread = second_thread_response.json()["thread_id"]
    first_context = {"agent_id": "main", "thread_id": first_thread}
    second_context = {"agent_id": "main", "thread_id": second_thread}

    facade = server.plugin_host.facade("enterprise-knowledge")
    source_snapshot_response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": first_context, "payload": {}},
    )
    source_snapshot = source_snapshot_response.json()["result"]["session"]
    original_commit = PluginHostFacade.commit

    async def bump_binding_before_session_commit(
        self, writes, activations=(), *, requirements=(), checks=()
    ):
        if any(write.namespace == "sessions" for write in writes):
            await original_commit(
                self, (RecordWrite("bindings", "main", 1, {"agent_id": "main"}, 0),)
            )
            return await original_commit(
                self, writes, activations, requirements=requirements, checks=checks
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", bump_binding_before_session_commit)
    stale = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": first_context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "source_revisions": _session_source_revisions(source_snapshot),
            },
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await facade.list_records("sessions") == []
    monkeypatch.setattr(PluginHostFacade, "commit", original_commit)

    async def initialize(headers, context):
        snapshot_response = await client.post(
            "/api/plugins/enterprise-knowledge/actions/session.get",
            headers=headers,
            json={"api_version": 1, "context": context, "payload": {}},
        )
        return await client.post(
            "/api/plugins/enterprise-knowledge/actions/session.save",
            headers=headers,
            json={
                "api_version": 1,
                "context": context,
                "payload": {
                    "expected_revision": 0,
                    "selected_kb_ids": [],
                    "source_revisions": _session_source_revisions(
                        snapshot_response.json()["result"]["session"]
                    ),
                },
            },
        )

    first_results = await asyncio.gather(
        initialize(admin_auth, first_context), initialize(admin_auth, first_context)
    )
    assert sorted(result.status_code for result in first_results) == [200, 409]
    second_user_session = await initialize(second_auth, second_context)
    assert second_user_session.status_code == 200, second_user_session.text
    first_snapshot = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": first_context, "payload": {}},
    )
    assert first_snapshot.status_code == 200, first_snapshot.text
    assert first_snapshot.json()["result"]["session"]["selection_present"] is True
    other_snapshot = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=second_auth,
        json={"api_version": 1, "context": second_context, "payload": {}},
    )
    assert other_snapshot.status_code == 200, other_snapshot.text
    assert other_snapshot.json()["result"]["session"]["selection_present"] is True
    assert len(await facade.list_records("sessions")) == 2


async def test_session_uses_authorized_snapshot_and_marks_revoked_selection_invalid(
    env, tmp_path, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)
    connection = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Session scope",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-a", "kb-b"],
                "enabled": True,
                "api_key": "SESSION_SCOPE_KEY",
            },
        },
    )
    assert connection.status_code == 200, connection.text
    connection_id = connection.json()["result"]["connection"]["connection_id"]
    grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "user_id": 1,
                "user_allowed_kb_ids": ["kb-a"],
            },
        },
    )
    assert grant.status_code == 200, grant.text
    binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a", "kb-b"],
                "default_kb_ids": ["kb-a", "kb-b"],
                "enabled": True,
            },
        },
    )
    assert binding.status_code == 200, binding.text
    thread_id = (await client.post("/api/agents/main/threads", headers=admin_auth)).json()[
        "thread_id"
    ]
    context = {"agent_id": "main", "thread_id": thread_id}
    initial = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    snapshot = initial.json()["result"]["session"]
    assert snapshot["selected_kb_ids"] == ["kb-a"]
    assert snapshot["selection_present"] is False
    assert snapshot["search_ready"] is False

    binding_update = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "expected_revision": snapshot["binding_revision"],
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a", "kb-b"],
                "default_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert binding_update.status_code == 200, binding_update.text
    stale_binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": ["kb-a"],
                "source_revisions": _session_source_revisions(snapshot),
            },
        },
    )
    assert stale_binding.status_code == 409
    assert stale_binding.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await server.plugin_host.facade("enterprise-knowledge").list_records("sessions") == []
    current_snapshot_response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    snapshot = current_snapshot_response.json()["result"]["session"]
    assert snapshot["binding_revision"] == binding_update.json()["result"]["binding"]["revision"]
    assert snapshot["selected_kb_ids"] == ["kb-a"]

    facade = server.plugin_host.facade("enterprise-knowledge")
    original_commit = PluginHostFacade.commit
    intent = await facade.get_record("connection_intents", connection_id)
    assert intent is not None
    raced = False

    async def bump_intent_before_session_commit(
        self, writes, activations=(), *, requirements=(), checks=()
    ):
        nonlocal raced
        if not raced and any(write.namespace == "sessions" for write in writes):
            raced = True
            await original_commit(
                self,
                (
                    RecordWrite(
                        "connection_intents",
                        connection_id,
                        intent.owner_user_id,
                        intent.payload,
                        intent.revision,
                    ),
                ),
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", bump_intent_before_session_commit)
    stale_source = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "source_revisions": _session_source_revisions(snapshot),
            },
        },
    )
    assert stale_source.status_code == 409
    assert stale_source.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await facade.list_records("sessions") == []
    monkeypatch.setattr(PluginHostFacade, "commit", original_commit)

    cleared = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "mode": "KB_ONLY",
                "source_revisions": _session_source_revisions(snapshot),
            },
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["result"]["session"]["selection_present"] is True

    again = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    assert again.json()["result"]["session"]["selected_kb_ids"] == []

    selected = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 1,
                "selected_kb_ids": ["kb-a"],
                "source_revisions": _session_source_revisions(again.json()["result"]["session"]),
            },
        },
    )
    assert selected.status_code == 200, selected.text

    changed_grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "expected_revision": 1,
                "user_id": 1,
                "user_allowed_kb_ids": [],
            },
        },
    )
    assert changed_grant.status_code == 200, changed_grant.text
    stale_scope = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    assert stale_scope.status_code == 200, stale_scope.text
    assert stale_scope.json()["result"]["session"]["invalid"] is True
    assert stale_scope.json()["result"]["session"]["selected_kb_ids"] == ["kb-a"]


async def test_session_snapshot_rejects_new_grant_and_connection_identity_mismatch(
    env, tmp_path
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)

    async def create_connection(name: str) -> str:
        response = await client.post(
            "/api/plugins/enterprise-knowledge/actions/connection.save",
            headers=admin_auth,
            json={
                "api_version": 1,
                "payload": {
                    "name": name,
                    "base_url": "https://knowledge.example",
                    "allowed_kb_ids": ["kb-a"],
                    "enabled": True,
                    "api_key": f"{name.upper().replace(' ', '_')}_KEY",
                },
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]["connection"]["connection_id"]

    first_connection = await create_connection("First identity")
    second_connection = await create_connection("Second identity")
    for connection_id in (first_connection,):
        response = await client.post(
            "/api/plugins/enterprise-knowledge/actions/grant.save",
            headers=admin_auth,
            json={
                "api_version": 1,
                "payload": {
                    "connection_id": connection_id,
                    "user_id": 1,
                    "user_allowed_kb_ids": ["kb-a"],
                },
            },
        )
        assert response.status_code == 200, response.text

    binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "connection_ref": first_connection,
                "expert_allowed_kb_ids": ["kb-a"],
                "default_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert binding.status_code == 200, binding.text
    thread_id = (await client.post("/api/agents/main/threads", headers=admin_auth)).json()[
        "thread_id"
    ]
    context = {"agent_id": "main", "thread_id": thread_id}

    switched = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "expected_revision": binding.json()["result"]["binding"]["revision"],
                "connection_ref": second_connection,
                "expert_allowed_kb_ids": ["kb-a"],
                "default_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert switched.status_code == 200, switched.text
    before_grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    old_source = before_grant.json()["result"]["session"]
    assert old_source["connection_id"] == second_connection
    assert old_source["grant_revision"] == 0

    config_changed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": second_connection,
                "expected_revision": old_source["config_revision"],
                "name": "Second identity updated",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert config_changed.status_code == 200, config_changed.text
    stale_config = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "source_revisions": _session_source_revisions(old_source),
            },
        },
    )
    assert stale_config.status_code == 409
    assert stale_config.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await server.plugin_host.facade("enterprise-knowledge").list_records("sessions") == []
    before_grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    old_source = before_grant.json()["result"]["session"]

    grant_changed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": second_connection,
                "expected_revision": old_source["grant_revision"],
                "user_id": 1,
                "user_allowed_kb_ids": [],
            },
        },
    )
    assert grant_changed.status_code == 200, grant_changed.text
    stale_grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "source_revisions": _session_source_revisions(old_source),
            },
        },
    )
    assert stale_grant.status_code == 409
    assert stale_grant.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await server.plugin_host.facade("enterprise-knowledge").list_records("sessions") == []

    current_response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=admin_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    current = current_response.json()["result"]["session"]
    assert current["grant_revision"] == grant_changed.json()["result"]["grant"]["revision"]
    other_identity = _session_source_revisions(current)
    other_identity["connection_id"] = first_connection
    identity_mismatch = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "expected_revision": 0,
                "selected_kb_ids": [],
                "source_revisions": other_identity,
            },
        },
    )
    assert identity_mismatch.status_code == 409
    assert identity_mismatch.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await server.plugin_host.facade("enterprise-knowledge").list_records("sessions") == []


async def test_connection_cas_allows_only_one_same_revision_update(env) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Race",
                "base_url": "https://knowledge.example",
                "api_key": "RACE_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]

    async def save(name: str) -> Any:
        return await client.post(
            "/api/plugins/enterprise-knowledge/actions/connection.save",
            headers=admin_auth,
            json={
                "api_version": 1,
                "payload": {
                    "connection_id": connection["connection_id"],
                    "expected_revision": connection["revision"],
                    "name": name,
                    "base_url": "https://knowledge.example/api/v1",
                },
            },
        )

    results = await asyncio.gather(save("Race A"), save("Race B"))
    assert sorted(result.status_code for result in results) == [200, 409]

    current = await server.plugin_host.facade("enterprise-knowledge").get_record(
        "connections", connection["connection_id"]
    )
    assert current is not None
    assert current.revision == 2
    assert current.payload["name"] in {"Race A", "Race B"}


async def test_another_admin_cannot_change_record_or_credential_owner(env) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    second_admin_auth = await create_user(
        client,
        admin_auth,
        username="knowledge-admin-two",
        role="admin",
    )
    first = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Owner stable",
                "base_url": "https://knowledge.example",
                "api_key": "OWNER_KEY_CANARY",
            },
        },
    )
    assert first.status_code == 200, first.text
    connection_id = first.json()["result"]["connection"]["connection_id"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    original = await facade.get_record("connections", connection_id)
    assert original is not None

    second = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=second_admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "expected_revision": original.revision,
                "name": "Owner stable edited by admin two",
                "base_url": "https://knowledge.example/api/v1",
            },
        },
    )
    assert second.status_code == 200, second.text
    changed = await facade.get_record("connections", connection_id)
    assert changed is not None
    assert changed.owner_user_id == original.owner_user_id
    assert (
        changed.payload["credential_owner_user_id"] == original.payload["credential_owner_user_id"]
    )
    assert changed.payload["credential_ref"] == original.payload["credential_ref"]
    assert "OWNER_KEY_CANARY" not in second.text


async def test_owner_agent_plugins_http_matrix_and_payload_cannot_forge_agent(
    env_with_provider,
) -> None:
    client, server, admin_auth = env_with_provider
    await install_enterprise_knowledge(server)
    owner_auth = await create_user(
        client,
        admin_auth,
        username="knowledge-owner",
        permissions=["agent_plugins"],
    )
    owner_id = await resolve_user_id(client, admin_auth, "knowledge-owner")
    agent_id = await create_agent(client, owner_auth, name="Knowledge owner agent")
    connection = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Granted",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-1"],
                "enabled": True,
                "api_key": "OWNER_SCOPE_KEY",
            },
        },
    )
    assert connection.status_code == 200, connection.text
    connection_id = connection.json()["result"]["connection"]["connection_id"]
    grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "user_id": owner_id,
                "user_allowed_kb_ids": ["kb-1"],
            },
        },
    )
    assert grant.status_code == 200, grant.text

    owner_settings = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=owner_auth,
        json={"api_version": 1, "context": {"agent_id": agent_id}, "payload": {}},
    )
    assert owner_settings.status_code == 200, owner_settings.text
    result = owner_settings.json()["result"]
    assert [item["connection_id"] for item in result["connections"]] == [connection_id]
    assert result["connections"][0]["allowed_kb_ids"] == ["kb-1"]
    assert result["grants"] == []

    saved = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=owner_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": agent_id},
            "payload": {"expected_revision": 0},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["result"]["binding"]["revision"] == 1

    forged = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=owner_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": agent_id},
            "payload": {"expected_revision": 1, "agent_id": "main"},
        },
    )
    assert forged.status_code == 400
    assert forged.json()["error"]["code"] == "PLUGIN_ACTION_INVALID"

    other_auth = await create_user(
        client,
        admin_auth,
        username="knowledge-non-owner",
        permissions=["agent_plugins"],
    )
    non_owner = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=other_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": agent_id},
            "payload": {"expected_revision": 0},
        },
    )
    assert non_owner.status_code == 403

    no_permission_auth = await create_user(
        client,
        admin_auth,
        username="knowledge-no-plugin-permission",
        permissions=[],
    )
    no_permission_agent = await create_agent(client, no_permission_auth, name="No permission")
    denied_settings = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=no_permission_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": no_permission_agent},
            "payload": {},
        },
    )
    assert denied_settings.status_code == 403
    denied_binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=no_permission_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": no_permission_agent},
            "payload": {"expected_revision": 0},
        },
    )
    assert denied_binding.status_code == 403
    denied_grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=owner_auth,
        json={"api_version": 1, "payload": {"connection_id": connection_id, "user_id": 1}},
    )
    assert denied_grant.status_code == 403


async def test_real_host_binding_success_and_required_revision_conflict_is_atomic(
    env, tmp_path, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)
    original_commit = PluginHostFacade.commit
    observed_connection_commit: tuple[list[str], int] | None = None

    async def observe_connection_commit(
        self, writes, activations=(), *, requirements=(), checks=()
    ):
        nonlocal observed_connection_commit
        if any(write.namespace == "connections" for write in writes):
            observed_connection_commit = ([write.namespace for write in writes], len(activations))
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", observe_connection_commit)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Policy-ready",
                "base_url": "https://knowledge.example",
                "enabled": True,
                "api_key": "POLICY_READY_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    assert observed_connection_commit == (
        ["connections", "connection_intents", "connection_recovery"],
        1,
    )
    connection_id = created.json()["result"]["connection"]["connection_id"]
    binding_url = "/api/plugins/enterprise-knowledge/actions/binding.save"
    first = await client.post(
        binding_url,
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {"expected_revision": 0, "connection_ref": connection_id, "enabled": True},
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["result"]["binding"]["revision"] == 1

    admin = server.user_manager.get_by_id(1)
    assert admin is not None
    actor = PluginActor(1, True, frozenset(admin.permissions), agent_id="main")
    facade = server.plugin_host.facade("enterprise-knowledge")
    before = await facade.get_requirement(actor, "main")
    assert dict(before.required_plugins) == {
        "enterprise-knowledge": "0.1.0",
        "enterprise-policy": "0.1.0",
    }
    injected = False

    async def race_required_revision(self, writes, activations=(), *, requirements=(), checks=()):
        nonlocal injected
        if requirements and not injected:
            injected = True
            await original_commit(
                self,
                [],
                requirements=(
                    RequiredExtensionWrite(
                        actor=actor,
                        agent_id="main",
                        required_plugins=dict(before.required_plugins),
                        expected_revision=before.revision,
                    ),
                ),
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", race_required_revision)
    raced = await client.post(
        binding_url,
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "expected_revision": 1,
                "connection_ref": connection_id,
                "enabled": True,
                "web_allowed": True,
            },
        },
    )
    assert raced.status_code == 409
    current = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.get",
        headers=admin_auth,
        json={"api_version": 1, "context": {"agent_id": "main"}, "payload": {}},
    )
    assert current.status_code == 200
    assert current.json()["result"]["binding"]["revision"] == 1
    assert current.json()["result"]["binding"]["web_allowed"] is False
    after = await facade.get_requirement(actor, "main")
    assert after.revision == before.revision + 1
    assert dict(after.required_plugins) == dict(before.required_plugins)


async def test_binding_source_connection_record_check_rejects_concurrent_change_atomically(
    env, tmp_path, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Source CAS",
                "base_url": "https://knowledge.example",
                "enabled": True,
                "api_key": "SOURCE_CAS_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    original_commit = PluginHostFacade.commit
    injected = False

    async def race_connection_revision(self, writes, activations=(), *, requirements=(), checks=()):
        nonlocal injected
        if checks and not injected:
            injected = True
            source = await self.get_record("connections", connection["connection_id"])
            assert source is not None
            changed = dict(source.payload)
            changed.update(enabled=False, probe_status="DISABLED", probe_details=None)
            await original_commit(
                self,
                (
                    RecordWrite(
                        "connections",
                        connection["connection_id"],
                        source.owner_user_id,
                        changed,
                        source.revision,
                    ),
                ),
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", race_connection_revision)
    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "expected_revision": 0,
                "connection_ref": connection["connection_id"],
                "enabled": True,
            },
        },
    )
    assert response.status_code == 409
    binding = await facade.get_record("bindings", "main")
    assert binding is None
    admin = server.user_manager.get_by_id(1)
    assert admin is not None
    state = await facade.get_requirement(
        PluginActor(1, True, frozenset(admin.permissions), agent_id="main"), "main"
    )
    assert state.revision == 0
    source = await facade.get_record("connections", connection["connection_id"])
    assert source is not None and source.revision == 2 and not source.payload["enabled"]


async def test_binding_record_check_rejects_concurrent_recovery_quarantine(
    env, tmp_path, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Intent CAS",
                "base_url": "https://knowledge.example",
                "enabled": True,
                "api_key": "INTENT_CAS_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    original_commit = PluginHostFacade.commit
    injected = False

    async def race_intent_revision(self, writes, activations=(), *, requirements=(), checks=()):
        nonlocal injected
        if any(write.namespace == "bindings" for write in writes) and not injected:
            injected = True
            intent = await self.get_record("connection_intents", connection["connection_id"])
            assert intent is not None
            blocked = {**intent.payload, "state": "BLOCKED"}
            await original_commit(
                self,
                (
                    RecordWrite(
                        "connection_intents",
                        connection["connection_id"],
                        intent.owner_user_id,
                        blocked,
                        intent.revision,
                    ),
                ),
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", race_intent_revision)
    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "main"},
            "payload": {
                "expected_revision": 0,
                "connection_ref": connection["connection_id"],
                "enabled": True,
            },
        },
    )
    assert response.status_code == 409
    assert await facade.get_record("bindings", "main") is None
    admin = server.user_manager.get_by_id(1)
    assert admin is not None
    state = await facade.get_requirement(
        PluginActor(1, True, frozenset(admin.permissions), agent_id="main"), "main"
    )
    assert state.revision == 0
    quarantined = await facade.get_record("connection_intents", connection["connection_id"])
    assert quarantined is not None and quarantined.payload["state"] == "BLOCKED"


async def test_failed_connection_cas_records_staged_key_recovery(env, monkeypatch, caplog) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    original_commit = PluginHostFacade.commit

    async def conflict_connection_write(
        self, writes, activations=(), *, requirements=(), checks=()
    ):
        if writes and writes[0].namespace == "connections":
            raise PluginHostError("conflict")
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    async def leave_staged_credential(self, _credential_id, _owner_user_id):
        return False

    monkeypatch.setattr(PluginHostFacade, "commit", conflict_connection_write)
    monkeypatch.setattr(PluginHostFacade, "release_staged_credential", leave_staged_credential)
    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Recovery",
                "base_url": "https://knowledge.example",
                "api_key": "RECOVERY_KEY_CANARY",
            },
        },
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ROLLBACK_FAILED"
    assert "RECOVERY_KEY_CANARY" not in response.text
    assert "RECOVERY_KEY_CANARY" not in caplog.text
    assert server.plugin_host is not None
    facade = server.plugin_host.facade("enterprise-knowledge")
    assert await facade.list_records("connections") == []
    recovery = await facade.list_records("connection_intents")
    assert len(recovery) == 1
    assert recovery[0].payload["state"] == "BLOCKED"
    assert recovery[0].payload["connection_id"] == recovery[0].logical_key
    archived = await facade.list_records("connection_recovery")
    assert len(archived) == 1 and archived[0].payload["state"] == "BLOCKED"
    with pytest.raises(PluginHostError):
        await facade.resolve_credential(
            recovery[0].payload["staged_credential_ref"],
            recovery[0].payload["staged_credential_owner_user_id"],
        )


@pytest.mark.parametrize("fail_block_record_write", [False, True])
async def test_failed_rotation_persists_connection_quarantine_and_blocks_new_admin_writes(
    env, monkeypatch, caplog, fail_block_record_write: bool
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Protected recovery",
                "base_url": "https://knowledge.example",
                "enabled": True,
                "api_key": "OLD_ACTIVE_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    t0 = await facade.get_record("connections", connection["connection_id"])
    assert t0 is not None
    original_commit = PluginHostFacade.commit

    async def fail_connection_write(self, writes, activations=(), *, requirements=(), checks=()):
        if fail_block_record_write and any(
            write.namespace == "connection_intents" and write.payload.get("state") == "BLOCKED"
            for write in writes
        ):
            raise PluginHostError("unavailable")
        if writes and writes[0].namespace == "connections":
            raise PluginHostError("conflict")
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    async def leave_staged_credential(self, _credential_id, _owner_user_id):
        return False

    monkeypatch.setattr(PluginHostFacade, "commit", fail_connection_write)
    monkeypatch.setattr(PluginHostFacade, "release_staged_credential", leave_staged_credential)
    failed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection["connection_id"],
                "expected_revision": connection["revision"],
                "name": "Failed rotation",
                "base_url": "https://knowledge.example/api/v1",
                "api_key": "STAGED_ROTATION_KEY",
            },
        },
    )
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "ROLLBACK_FAILED"
    assert "STAGED_ROTATION_KEY" not in failed.text
    assert "STAGED_ROTATION_KEY" not in caplog.text

    intent = await facade.get_record("connection_intents", connection["connection_id"])
    assert intent is not None
    assert intent.payload["state"] == ("PREPARING" if fail_block_record_write else "BLOCKED")
    assert intent.payload["connection_id"] == connection["connection_id"]
    assert intent.payload["t0"]["connection_revision"] == t0.revision
    assert intent.payload["t0"]["credential_ref"] == t0.payload["credential_ref"]
    assert "STAGED_ROTATION_KEY" not in str(intent.payload)
    archived = await facade.get_record("connection_recovery", intent.payload["recovery_id"])
    assert archived is not None and archived.payload["state"] == intent.payload["state"]
    assert archived.payload["staged_credential_ref"] == intent.payload["staged_credential_ref"]
    planned_ref = intent.payload["staged_credential_ref"]
    assert isinstance(planned_ref, str) and UUID(planned_ref).version == 4
    assert intent.payload["staged_credential_owner_user_id"] == 1
    staged = server.services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, 1)
    assert staged is not None and staged.state == "staged"
    current = await facade.get_record("connections", connection["connection_id"])
    assert current is not None and current.payload == t0.payload and current.revision == t0.revision

    grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {"connection_id": connection["connection_id"], "user_id": 1},
        },
    )
    assert grant.status_code == 409
    settings = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=admin_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["result"]["connections"][0]["recovery_blocked"] is True


async def test_stale_connection_snapshot_cannot_create_intent_after_concurrent_save(
    env, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Race baseline",
                "base_url": "https://knowledge.example",
                "api_key": "RACE_OLD_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    original_get_record = PluginHostFacade.get_record
    original_stage = PluginHostFacade.stage_credential
    b_paused = asyncio.Event()
    resume_b = asyncio.Event()
    b_intent_reads = 0
    staged_ids: list[str | None] = []

    async def pause_b_before_initial_intent(self, namespace: str, logical_key: str):
        nonlocal b_intent_reads
        if namespace == "connection_intents" and logical_key == connection["connection_id"]:
            b_intent_reads += 1
            if b_intent_reads == 2:
                b_paused.set()
                await resume_b.wait()
        return await original_get_record(self, namespace, logical_key)

    async def count_stages(self, owner_user_id, payload, *, credential_id=None):
        staged_ids.append(credential_id)
        return await original_stage(self, owner_user_id, payload, credential_id=credential_id)

    monkeypatch.setattr(PluginHostFacade, "get_record", pause_b_before_initial_intent)
    monkeypatch.setattr(PluginHostFacade, "stage_credential", count_stages)
    request_b = asyncio.create_task(
        client.post(
            "/api/plugins/enterprise-knowledge/actions/connection.save",
            headers=admin_auth,
            json={
                "api_version": 1,
                "payload": {
                    "connection_id": connection["connection_id"],
                    "expected_revision": 1,
                    "name": "Stale B",
                    "base_url": "https://knowledge.example",
                    "api_key": "RACE_STALE_B_KEY",
                },
            },
        )
    )
    await asyncio.wait_for(b_paused.wait(), timeout=5)
    request_a = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection["connection_id"],
                "expected_revision": 1,
                "name": "Committed A",
                "base_url": "https://knowledge.example",
                "api_key": "RACE_A_KEY",
            },
        },
    )
    assert request_a.status_code == 200, request_a.text
    resume_b.set()
    response_b = await request_b
    assert response_b.status_code == 409, response_b.text
    assert response_b.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert len(staged_ids) == 1
    latest = await facade.get_record("connections", connection["connection_id"])
    assert latest is not None and latest.payload["name"] == "Committed A"
    intent = await facade.get_record("connection_intents", connection["connection_id"])
    assert intent is not None and intent.payload["state"] == "RESOLVED"
    assert intent.payload["t0"]["connection_revision"] == 1
    recovery = await facade.list_records("connection_recovery")
    assert len(recovery) == 2
    assert sorted(record.payload["t0"]["connection_revision"] for record in recovery) == [0, 1]


async def test_stage_failure_closes_intent_without_changing_connection_t0(env, monkeypatch) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    facade = server.plugin_host.facade("enterprise-knowledge")

    async def reject_stage(_self, _owner_user_id, _payload, *, credential_id=None):
        raise PluginHostError("unavailable")

    monkeypatch.setattr(PluginHostFacade, "stage_credential", reject_stage)
    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Stage failure",
                "base_url": "https://knowledge.example",
                "api_key": "NEVER_STAGED_KEY",
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLUGIN_NOT_READY"
    assert await facade.list_records("connections") == []
    intents = await facade.list_records("connection_intents")
    assert len(intents) == 1 and intents[0].payload["state"] == "ABORTED"
    assert intents[0].payload["t0"]["connection_revision"] == 0
    assert UUID(intents[0].payload["staged_credential_ref"]).version == 4
    assert "NEVER_STAGED_KEY" not in response.text
    archived = await facade.list_records("connection_recovery")
    assert len(archived) == 1 and archived[0].payload["state"] == "ABORTED"
    assert (
        archived[0].payload["staged_credential_ref"] == intents[0].payload["staged_credential_ref"]
    )


async def test_connection_commit_failure_with_successful_release_preserves_t0(
    env, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    created = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Release path",
                "base_url": "https://knowledge.example",
                "api_key": "RELEASE_OLD_KEY",
            },
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()["result"]["connection"]
    facade = server.plugin_host.facade("enterprise-knowledge")
    t0 = await facade.get_record("connections", connection["connection_id"])
    assert t0 is not None
    original_commit = PluginHostFacade.commit

    async def conflict_connection_write(
        self, writes, activations=(), *, requirements=(), checks=()
    ):
        if writes and writes[0].namespace == "connections":
            raise PluginHostError("conflict")
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", conflict_connection_write)
    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection["connection_id"],
                "expected_revision": connection["revision"],
                "name": "Rejected update",
                "base_url": "https://knowledge.example/api/v1",
                "api_key": "RELEASE_NEW_KEY",
            },
        },
    )
    assert response.status_code == 409
    current = await facade.get_record("connections", connection["connection_id"])
    assert current is not None and current.revision == t0.revision
    assert current.payload == t0.payload
    intent = await facade.get_record("connection_intents", connection["connection_id"])
    assert intent is not None and intent.payload["state"] == "ABORTED"
    assert intent.payload["t0"]["credential_ref"] == t0.payload["credential_ref"]
    planned_ref = intent.payload["staged_credential_ref"]
    assert isinstance(planned_ref, str) and UUID(planned_ref).version == 4
    assert (
        server.services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, 1) is None
    )
    archived = await facade.get_record("connection_recovery", intent.payload["recovery_id"])
    assert archived is not None and archived.payload["state"] == "ABORTED"
    assert archived.payload["t0"]["connection_revision"] == t0.revision
    assert "RELEASE_NEW_KEY" not in response.text


async def test_catalog_probe_actions_and_current_revision_readiness(env, monkeypatch) -> None:
    import httpx

    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/knowledge-bases"):
            data = [{"id": "kb-a", "name": "A", "description": "safe", "knowledge_count": 1}]
        elif request.url.path.endswith("/knowledge-search"):
            data = [{"knowledge_id": "doc-a", "knowledge_base_id": "kb-a", "content": "found"}]
        elif request.url.path.endswith("/knowledge/doc-a"):
            data = {"id": "doc-a", "knowledge_base_id": "kb-a", "title": "A"}
        elif request.url.path.endswith("/chunks/doc-a"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "chunk-a",
                            "knowledge_id": "doc-a",
                            "knowledge_base_id": "kb-a",
                            "chunk_type": "text",
                            "chunk_index": 0,
                            "content": "verified text",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 10,
                },
            )
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    async def mock_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    saved = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Saved disabled",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": [],
                "enabled": False,
                "api_key": "SYNTHETIC_PROBE_KEY",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    connection = saved.json()["result"]["connection"]
    identifier = connection["connection_id"]
    revision = connection["revision"]

    sdk_state = server.plugin_host._plugins["enterprise-knowledge"]
    probe_handler = sdk_state.actions["connection.catalog"].handler
    sdk_probe_service = probe_handler.__globals__["ProbeService"]
    monkeypatch.setattr(sdk_probe_service, "_client", lambda _self: mock_client())

    directory = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.catalog",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {"connection_id": identifier, "expected_revision": revision},
        },
    )
    assert directory.status_code == 200, directory.text
    assert directory.json()["result"]["knowledge_bases"] == [
        {"kb_id": "kb-a", "name": "A", "description": "safe", "knowledge_count": 1}
    ]

    employee_auth = await create_user(client, admin_auth, username="probe-not-admin")
    employee_catalog = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.catalog",
        headers=employee_auth,
        json={
            "api_version": 1,
            "payload": {"connection_id": identifier, "expected_revision": revision},
        },
    )
    assert employee_catalog.status_code == 400
    assert len(calls) == 1

    blocked = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.probe",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": identifier,
                "expected_revision": revision,
                "component": "SEARCH",
            },
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["result"]["status"] == "BLOCKED"
    assert len(calls) == 1

    denied = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.probe",
        headers=employee_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": identifier,
                "expected_revision": revision,
                "component": "LIST",
            },
        },
    )
    assert denied.status_code == 403
    assert len(calls) == 1

    enabled = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": identifier,
                "expected_revision": revision,
                "name": "Enabled for probe",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert enabled.status_code == 200, enabled.text
    revision = enabled.json()["result"]["connection"]["revision"]
    stale_projection = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=admin_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert stale_projection.json()["result"]["connections"][0]["probe_status"] == "CONFIGURED"
    probes = (
        {"component": "LIST"},
        {"component": "SEARCH", "query": "synthetic question", "knowledge_base_ids": ["kb-a"]},
        {"component": "READ", "knowledge_base_ids": ["kb-a"], "knowledge_id": "doc-a"},
    )
    for payload in probes:
        response = await client.post(
            "/api/plugins/enterprise-knowledge/actions/connection.probe",
            headers=admin_auth,
            json={
                "api_version": 1,
                "payload": {
                    "connection_id": identifier,
                    "expected_revision": revision,
                    **payload,
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["status"] == "SUCCESS"

    settings = await client.post(
        "/api/plugins/enterprise-knowledge/actions/settings.get",
        headers=admin_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert settings.status_code == 200, settings.text
    projected = settings.json()["result"]["connections"][0]
    assert projected["probe_status"] == "READY"
    assert {key: value["status"] for key, value in projected["probe_components"].items()} == {
        "LIST": "SUCCESS",
        "SEARCH": "SUCCESS",
        "READ": "SUCCESS",
    }
    facade = server.plugin_host.facade("enterprise-knowledge")
    records = await facade.list_records("connection_probes")
    assert len(records) == 3
    assert all(record.payload["observed_revision"] == revision for record in records)
    assert "SYNTHETIC_PROBE_KEY" not in str(records)
    assert "synthetic question" not in str(records)
    assert [path for _, path, _ in calls] == [
        "/api/v1/knowledge-bases",
        "/api/v1/knowledge-bases",
        "/api/v1/knowledge-bases",
        "/api/v1/knowledge-search",
        "/api/v1/knowledge-bases",
        "/api/v1/knowledge/doc-a",
        "/api/v1/chunks/doc-a",
    ]
    assert len(calls) == 7
    assert sum(path.endswith("/knowledge-bases") for _, path, _ in calls) == 4
    assert sum(path.endswith("/knowledge-search") for _, path, _ in calls) == 1
    assert sum(path.endswith("/knowledge/doc-a") for _, path, _ in calls) == 1
    assert sum(path.endswith("/chunks/doc-a") for _, path, _ in calls) == 1
