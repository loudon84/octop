from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import httpx
import pytest

from octop.infra.agents.plugins.execution import (
    activate_ticket,
    issue_execution_ticket,
    reset_ticket,
)
from octop.infra.agents.plugins.host import PluginHostFacade
from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordWrite,
)
from tests.support.auth import create_agent, create_user, resolve_user_id
from tests.support.enterprise import (
    install_enterprise_knowledge,
    install_synthetic_enterprise_policy,
)


async def _query_fixture(env, tmp_path, *, username: str, install_policy: bool = True):
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    if install_policy:
        await install_synthetic_enterprise_policy(server, tmp_path)
    employee_auth = await create_user(client, admin_auth, username=username)
    user_id = await resolve_user_id(client, admin_auth, username)
    agent_id = await create_agent(client, employee_auth, name=f"{username} agent")
    saved = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Local query fixture",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-a"],
                "enabled": True,
                "api_key": "SYNTHETIC_QUERY_KEY",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    connection_id = saved.json()["result"]["connection"]["connection_id"]
    grant = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "user_id": user_id,
                "user_allowed_kb_ids": ["kb-a"],
            },
        },
    )
    assert grant.status_code == 200, grant.text
    if install_policy:
        binding = await client.post(
            "/api/plugins/enterprise-knowledge/actions/binding.save",
            headers=admin_auth,
            json={
                "api_version": 1,
                "context": {"agent_id": agent_id},
                "payload": {
                    "connection_ref": connection_id,
                    "expert_allowed_kb_ids": ["kb-a"],
                    "default_kb_ids": ["kb-a"],
                    "enabled": True,
                },
            },
        )
        assert binding.status_code == 200, binding.text
    thread_response = await client.post(f"/api/agents/{agent_id}/threads", headers=employee_auth)
    assert thread_response.status_code == 201, thread_response.text
    context = {"agent_id": agent_id, "thread_id": thread_response.json()["thread_id"]}
    facade = server.plugin_host.facade("enterprise-knowledge")
    service = facade.resolve_service("enterprise-knowledge", 1) if install_policy else None
    return (
        client,
        server,
        admin_auth,
        employee_auth,
        user_id,
        agent_id,
        context,
        connection_id,
        facade,
        service,
    )


async def test_empty_retrieval_publishes_effective_observation_and_stays_terminal(
    env, tmp_path, monkeypatch
) -> None:
    (
        client,
        server,
        _admin_auth,
        employee_auth,
        user_id,
        agent_id,
        context,
        _connection_id,
        facade,
        service,
    ) = await _query_fixture(env, tmp_path, username="empty-success-employee")
    provider = import_module("harness_plugin_enterprise_knowledge.enterprise_knowledge.weknora")
    calls: list[str] = []

    async def empty_source(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/knowledge-bases"):
            return httpx.Response(
                200,
                json={"success": True, "data": [{"id": "kb-a", "name": "Effective A"}]},
            )
        if request.url.path.endswith("/knowledge-search"):
            return httpx.Response(200, json={"success": True, "data": []})
        return httpx.Response(404)

    monkeypatch.setattr(
        provider,
        "_build_async_client",
        lambda _ca: httpx.AsyncClient(transport=httpx.MockTransport(empty_source)),
    )
    prepared = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 0},
        },
    )
    assert prepared.status_code == 200, prepared.text
    operation_id = prepared.json()["result"]["operation"]["operation_id"]
    ticket = issue_execution_ticket(
        server.plugin_host,
        PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"]),
        plugin_context={"enterprise-knowledge": {"operation_id": operation_id}},
    )
    token = activate_ticket(server.plugin_host, ticket)
    try:
        retrieved = await service.retrieve_current("synthetic question")
        assert retrieved.operation.status == "EMPTY"
        assert retrieved.evidence == []
        assert [
            (item.kb_id, item.name) for item in retrieved.operation.observed_knowledge_bases or []
        ] == [("kb-a", "Effective A")]
        persisted = await facade.get_record("operations", operation_id)
        assert persisted is not None
        assert persisted.payload["status"] == retrieved.operation.status == "EMPTY"
        assert persisted.payload["observed_knowledge_bases"] == [
            {"kb_id": "kb-a", "name": "Effective A"}
        ]
        assert await facade.list_records("evidence", f'["{operation_id}",') == []

        completed = await service.complete_current()
        assert completed.operation.status == "EMPTY"
        assert completed.operation.revision == retrieved.operation.revision
        terminal = await facade.get_record("operations", operation_id)
        assert terminal is not None
        assert terminal.revision == persisted.revision
        assert terminal.payload["status"] == "EMPTY"
    finally:
        reset_ticket(token)
    assert calls == ["/api/v1/knowledge-bases", "/api/v1/knowledge-search"]


@pytest.mark.parametrize("policy_state", ["missing", "disabled"])
async def test_employee_actions_require_policy_pair_while_admin_setup_remains_available(
    env, tmp_path, policy_state
) -> None:
    (
        client,
        server,
        admin_auth,
        employee_auth,
        _user_id,
        _agent_id,
        context,
        _connection_id,
        _facade,
        _service,
    ) = await _query_fixture(
        env,
        tmp_path,
        username=f"policy-pair-{policy_state}-employee",
        install_policy=policy_state != "missing",
    )
    manager = server.plugin_manager
    assert manager is not None
    if policy_state == "disabled":
        await manager.mutate(lambda: manager.set_enabled("enterprise-policy", False))

    base = {
        "api_version": 1,
        "context": context,
    }
    operation_id = str(uuid4())
    evidence_id = str(uuid4())
    action_payloads = {
        "operation.prepare": {"expected_session_revision": 0},
        "evidence.list": {"operation_id": operation_id},
        "evidence.read": {"operation_id": operation_id, "evidence_id": evidence_id},
        "evidence.original": {"operation_id": operation_id, "evidence_id": evidence_id},
    }
    for action_name, payload in action_payloads.items():
        response = await client.post(
            f"/api/plugins/enterprise-knowledge/actions/{action_name}",
            headers=employee_auth,
            json={**base, "payload": payload},
        )
        assert response.status_code == 503, (action_name, response.text)
        assert response.json()["error"]["code"] == "PLUGIN_HOST_UNAVAILABLE", action_name

    setup = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": f"Independent setup {policy_state}",
                "base_url": "https://knowledge.example",
                "enabled": False,
                "api_key": "SYNTHETIC_SETUP_KEY",
            },
        },
    )
    assert setup.status_code == 200, setup.text
    assert setup.json()["result"]["connection"]["name"] == f"Independent setup {policy_state}"


async def test_prepare_uses_required_flat_service_without_source_http(
    env, tmp_path, monkeypatch
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)
    employee_auth = await create_user(client, admin_auth, username="query-employee")
    user_id = await resolve_user_id(client, admin_auth, "query-employee")
    agent_id = await create_agent(client, employee_auth, name="Query agent")

    connection_response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Local producer fixture",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-a"],
                "enabled": True,
                "api_key": "SYNTHETIC_QUERY_KEY",
            },
        },
    )
    assert connection_response.status_code == 200, connection_response.text
    connection_id = connection_response.json()["result"]["connection"]["connection_id"]
    grant_response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "user_id": user_id,
                "user_allowed_kb_ids": ["kb-a"],
            },
        },
    )
    assert grant_response.status_code == 200, grant_response.text
    binding_response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": agent_id},
            "payload": {
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a"],
                "default_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert binding_response.status_code == 200, binding_response.text
    thread_response = await client.post(f"/api/agents/{agent_id}/threads", headers=employee_auth)
    assert thread_response.status_code == 201, thread_response.text
    context = {"agent_id": agent_id, "thread_id": thread_response.json()["thread_id"]}

    facade = server.plugin_host.facade("enterprise-knowledge")
    service = facade.resolve_service("enterprise-knowledge", 1)
    provider_module = import_module(
        "harness_plugin_enterprise_knowledge.enterprise_knowledge.weknora"
    )
    calls: list[httpx.Request] = []
    search_started = asyncio.Event()
    release_search = asyncio.Event()

    async def no_network(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/knowledge-bases"):
            return httpx.Response(
                200, json={"success": True, "data": [{"id": "kb-a", "name": "Source A"}]}
            )
        if request.url.path.endswith("/knowledge-search"):
            search_started.set()
            await release_search.wait()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "knowledge_id": "doc-1",
                            "knowledge_base_id": "kb-a",
                            "knowledge_title": "Official source title",
                            "content": "synthetic source text",
                            "source_locators": {"page": 2},
                        }
                    ],
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        provider_module,
        "_build_async_client",
        lambda _ca: httpx.AsyncClient(transport=httpx.MockTransport(no_network)),
    )
    prepared = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 0},
        },
    )
    assert prepared.status_code == 200, prepared.text
    operation = prepared.json()["result"]["operation"]
    assert operation["status"] == "VALIDATING"
    assert operation["session_revision"] == 0
    assert operation["observed_knowledge_bases"] is None
    assert calls == []

    listed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.list",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation["operation_id"]},
        },
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["result"]["evidence"] == []

    ticket = issue_execution_ticket(
        server.plugin_host,
        PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"]),
        plugin_context={"enterprise-knowledge": {"operation_id": operation["operation_id"]}},
    )
    ticket_token = activate_ticket(server.plugin_host, ticket)
    try:
        retrieval = asyncio.create_task(service.retrieve_current("synthetic question"))
        await search_started.wait()
        with pytest.raises(PluginActionError) as replay:
            await service.retrieve_current("synthetic question")
        assert replay.value.code == "CONFIG_CONFLICT"
        claimed = await facade.get_record("operations", operation["operation_id"])
        assert claimed is not None and claimed.payload["status"] == "RETRIEVING"
        assert len(calls) == 2
        release_search.set()
        retrieved = await retrieval
        assert retrieved.operation.status == "GENERATING"
        assert len(retrieved.evidence) == 1
        evidence = retrieved.evidence[0]
        assert evidence.title == "Official source title"
        assert evidence.source_page == 2
        assert evidence.source_version is None
        assert [
            (item.kb_id, item.name) for item in retrieved.operation.observed_knowledge_bases or []
        ] == [("kb-a", "Source A")]
        assert len(calls) == 2
        request_count = len(calls)
        evidence_module = import_module(
            "harness_plugin_enterprise_knowledge.enterprise_knowledge.evidence"
        )
        generated_ids: list[str] = []

        def counted_uuid4():
            value = uuid4()
            generated_ids.append(str(value))
            return value

        monkeypatch.setattr(evidence_module, "uuid4", counted_uuid4)
        cached = await service.search_current()
        assert cached.evidence == retrieved.evidence
        bases = await service.list_current()
        assert [item.kb_id for item in bases.knowledge_bases] == ["kb-a"]
        with pytest.raises(PluginActionError) as empty_narrowing:
            await service.search_current([])
        assert empty_narrowing.value.code == "SCOPE_INVALID"
        with pytest.raises(PluginActionError) as foreign_narrowing:
            await service.search_current(["kb-outside"])
        assert foreign_narrowing.value.code == "SCOPE_INVALID"
        assert generated_ids == []
        assert len(calls) == request_count

        await create_user(client, admin_auth, username="query-foreign-employee")
        other_user_id = await resolve_user_id(client, admin_auth, "query-foreign-employee")
        foreign_operation_id = str(uuid4())
        await facade.commit(
            (
                RecordWrite(
                    "operations",
                    foreign_operation_id,
                    other_user_id,
                    {
                        **retrieved.operation.model_dump(mode="json"),
                        "operation_id": foreign_operation_id,
                        "user_id": other_user_id,
                    },
                    0,
                ),
            )
        )
        foreign_ticket = issue_execution_ticket(
            server.plugin_host,
            PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"]),
            plugin_context={"enterprise-knowledge": {"operation_id": foreign_operation_id}},
        )
        foreign_token = activate_ticket(server.plugin_host, foreign_ticket)
        try:
            with pytest.raises(PluginActionError) as foreign_operation:
                await service.list_current()
            assert foreign_operation.value.code == "ACCESS_DENIED"
        finally:
            reset_ticket(foreign_token)

        wrong_actor_ticket = issue_execution_ticket(
            server.plugin_host,
            PluginActor(other_user_id, False, agent_id=agent_id, thread_id=context["thread_id"]),
            plugin_context={"enterprise-knowledge": {"operation_id": operation["operation_id"]}},
        )
        wrong_actor_token = activate_ticket(server.plugin_host, wrong_actor_ticket)
        try:
            with pytest.raises(PluginHostError):
                await service.list_current()
        finally:
            reset_ticket(wrong_actor_token)
        assert len(calls) == request_count
        completed = await service.complete_current()
        assert completed.operation.status == "COMPLETED"
        replayed_completion = await service.complete_current()
        assert replayed_completion.operation.revision == completed.operation.revision
    finally:
        reset_ticket(ticket_token)

    stale = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 1},
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFIG_CONFLICT"

    second_prepare = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 0},
        },
    )
    assert second_prepare.status_code == 200, second_prepare.text
    second_operation_id = second_prepare.json()["result"]["operation"]["operation_id"]
    second_ticket = issue_execution_ticket(
        server.plugin_host,
        PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"]),
        plugin_context={"enterprise-knowledge": {"operation_id": second_operation_id}},
    )
    second_ticket_token = activate_ticket(server.plugin_host, second_ticket)
    original_commit = PluginHostFacade.commit
    session_key = json.dumps([user_id, agent_id, context["thread_id"]], separators=(",", ":"))
    inserted_session = False

    async def conflict_final_publish(self, writes, activations=(), *, requirements=(), checks=()):
        nonlocal inserted_session
        if not inserted_session and any(write.namespace == "evidence" for write in writes):
            inserted_session = True
            await original_commit(
                self,
                (RecordWrite("sessions", session_key, user_id, {"concurrent": True}, 0),),
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", conflict_final_publish)
    try:
        try:
            await service.retrieve_current("synthetic question")
        except PluginActionError as exc:
            assert exc.code == "READ_SOURCE_CHANGED"
        else:
            raise AssertionError("concurrent session creation must reject evidence publication")
    finally:
        reset_ticket(second_ticket_token)
    assert inserted_session is True
    assert len(await facade.list_records("evidence")) == 1
    failed_operation = await facade.get_record("operations", second_operation_id)
    assert failed_operation is not None and failed_operation.payload["status"] == "FAILED"

    operations = await facade.list_records("operations")
    assert len(operations) == 3


async def test_prepare_rejects_implicit_session_creation_race(env, tmp_path, monkeypatch) -> None:
    (
        client,
        _server,
        _admin_auth,
        employee_auth,
        user_id,
        agent_id,
        context,
        _connection_id,
        _facade,
        _service,
    ) = await _query_fixture(env, tmp_path, username="prepare-race-employee")
    original_commit = PluginHostFacade.commit
    inserted = False
    session_key = json.dumps([user_id, agent_id, context["thread_id"]], separators=(",", ":"))

    async def race_session_create(self, writes, activations=(), *, requirements=(), checks=()):
        nonlocal inserted
        if not inserted and any(write.namespace == "operations" for write in writes):
            inserted = True
            await original_commit(
                self, (RecordWrite("sessions", session_key, user_id, {"raced": True}, 0),)
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", race_session_create)
    response = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 0},
        },
    )
    assert inserted is True
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert await _facade.list_records("operations") == []


async def test_evidence_list_rechecks_intent_for_empty_and_nonempty_results(
    env, tmp_path, monkeypatch
) -> None:
    (
        client,
        _server,
        _admin_auth,
        employee_auth,
        user_id,
        agent_id,
        context,
        connection_id,
        facade,
        _service,
    ) = await _query_fixture(env, tmp_path, username="list-intent-employee")
    models = import_module("harness_plugin_enterprise_knowledge.enterprise_knowledge.models")
    original_commit = PluginHostFacade.commit
    inject = [False]

    async def quarantine_before_empty_check(
        self, writes, activations=(), *, requirements=(), checks=()
    ):
        if (
            inject[0]
            and not writes
            and any(check.namespace == "connection_intents" for check in checks)
        ):
            inject[0] = False
            current = await self.get_record("connection_intents", connection_id)
            assert current is not None
            await original_commit(
                self,
                (
                    RecordWrite(
                        "connection_intents",
                        connection_id,
                        current.owner_user_id,
                        {**current.payload, "state": "PREPARING"},
                        current.revision,
                    ),
                ),
            )
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", quarantine_before_empty_check)
    for with_evidence in (True, False):
        prepared = await client.post(
            "/api/plugins/enterprise-knowledge/actions/operation.prepare",
            headers=employee_auth,
            json={
                "api_version": 1,
                "context": context,
                "payload": {"expected_session_revision": 0},
            },
        )
        assert prepared.status_code == 200, prepared.text
        operation = prepared.json()["result"]["operation"]
        if with_evidence:
            evidence_id = str(uuid4())
            evidence = models.EvidenceRecord(
                evidence_id=evidence_id,
                operation_id=operation["operation_id"],
                user_id=user_id,
                agent_id=agent_id,
                thread_id=context["thread_id"],
                source_kind="KNOWLEDGE",
                provider="WEKNORA",
                connection_id=connection_id,
                kb_id="kb-a",
                knowledge_id="doc-a",
                content="synthetic evidence",
                truncated=False,
                retrieved_at=datetime.now(UTC).isoformat(),
            )
            await facade.commit(
                (
                    RecordWrite(
                        "evidence",
                        json.dumps([operation["operation_id"], evidence_id], separators=(",", ":")),
                        user_id,
                        evidence.model_dump(mode="json"),
                        0,
                    ),
                )
            )
        intent = await facade.get_record("connection_intents", connection_id)
        assert intent is not None
        saved_intent = dict(intent.payload)
        inject[0] = True
        listed = await client.post(
            "/api/plugins/enterprise-knowledge/actions/evidence.list",
            headers=employee_auth,
            json={
                "api_version": 1,
                "context": context,
                "payload": {"operation_id": operation["operation_id"]},
            },
        )
        assert inject[0] is False
        assert listed.status_code == 409
        assert listed.json()["error"]["code"] == "READ_SOURCE_CHANGED"
        changed = await facade.get_record("connection_intents", connection_id)
        assert changed is not None
        await original_commit(
            facade,
            (
                RecordWrite(
                    "connection_intents",
                    connection_id,
                    changed.owner_user_id,
                    saved_intent,
                    changed.revision,
                ),
            ),
        )


async def test_retrieval_pins_claim_intent_across_aborted_staging_for_success_and_empty(
    env, tmp_path, monkeypatch
) -> None:
    (
        client,
        server,
        _admin_auth,
        employee_auth,
        user_id,
        agent_id,
        context,
        connection_id,
        facade,
        service,
    ) = await _query_fixture(env, tmp_path, username="retrieve-intent-employee")
    provider = import_module("harness_plugin_enterprise_knowledge.enterprise_knowledge.weknora")
    search_mode = ["success"]
    calls: list[str] = []

    async def source(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/knowledge-bases"):
            return httpx.Response(
                200, json={"success": True, "data": [{"id": "kb-a", "name": "A"}]}
            )
        if request.url.path.endswith("/knowledge-search"):
            if search_mode[0] == "grant":
                grant_key = f"{connection_id}:{user_id}"
                current_grant = await facade.get_record("grants", grant_key)
                assert current_grant is not None
                await facade.commit(
                    (
                        RecordWrite(
                            "grants",
                            grant_key,
                            current_grant.owner_user_id,
                            dict(current_grant.payload),
                            current_grant.revision,
                        ),
                    )
                )
            else:
                current = await facade.get_record("connection_intents", connection_id)
                assert current is not None
                preparing = await facade.commit(
                    (
                        RecordWrite(
                            "connection_intents",
                            connection_id,
                            current.owner_user_id,
                            {**current.payload, "state": "PREPARING"},
                            current.revision,
                        ),
                    )
                )
                await facade.commit(
                    (
                        RecordWrite(
                            "connection_intents",
                            connection_id,
                            preparing[0].owner_user_id,
                            {**preparing[0].payload, "state": "ABORTED"},
                            preparing[0].revision,
                        ),
                    )
                )
            hits = (
                []
                if search_mode[0] == "empty"
                else [
                    {
                        "knowledge_id": "doc-a",
                        "knowledge_base_id": "kb-a",
                        "content": "synthetic source text",
                    }
                ]
            )
            return httpx.Response(200, json={"success": True, "data": hits})
        return httpx.Response(404)

    monkeypatch.setattr(
        provider,
        "_build_async_client",
        lambda _ca: httpx.AsyncClient(transport=httpx.MockTransport(source)),
    )
    connection = await facade.get_record("connections", connection_id)
    assert connection is not None
    connection_revision = connection.revision
    actor = PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"])
    for mode in ("success", "empty", "grant"):
        prepared = await client.post(
            "/api/plugins/enterprise-knowledge/actions/operation.prepare",
            headers=employee_auth,
            json={
                "api_version": 1,
                "context": context,
                "payload": {"expected_session_revision": 0},
            },
        )
        assert prepared.status_code == 200, prepared.text
        operation_id = prepared.json()["result"]["operation"]["operation_id"]
        search_mode[0] = mode
        ticket = issue_execution_ticket(
            server.plugin_host,
            actor,
            plugin_context={"enterprise-knowledge": {"operation_id": operation_id}},
        )
        token = activate_ticket(server.plugin_host, ticket)
        try:
            with pytest.raises(PluginActionError) as changed:
                await service.retrieve_current("synthetic question")
            assert changed.value.code == "READ_SOURCE_CHANGED"
        finally:
            reset_ticket(token)
        operation = await facade.get_record("operations", operation_id)
        assert operation is not None and operation.payload["status"] == "FAILED"
        assert await facade.list_records("evidence", f'["{operation_id}",') == []
        unchanged = await facade.get_record("connections", connection_id)
        assert unchanged is not None and unchanged.revision == connection_revision
    assert len(calls) == 6


async def test_resolved_service_proxy_enforces_pair_and_invalidates_old_generations(
    env, tmp_path
) -> None:
    (
        _client,
        server,
        _admin_auth,
        _employee_auth,
        user_id,
        agent_id,
        context,
        _connection_id,
        facade,
        old_proxy,
    ) = await _query_fixture(env, tmp_path, username="proxy-lifecycle-employee")
    manager = server.plugin_manager
    assert manager is not None
    actor = PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"])
    models = import_module("harness_plugin_enterprise_knowledge.enterprise_knowledge.models")
    request = models.OperationPrepareInput(expected_session_revision=0)

    await manager.mutate(lambda: manager.set_enabled("enterprise-policy", False))
    with pytest.raises(PluginHostError):
        facade.resolve_service("enterprise-knowledge", 1)
    with pytest.raises(PluginHostError):
        await old_proxy.prepare(actor, request)

    await manager.mutate(lambda: manager.set_enabled("enterprise-policy", True))
    with pytest.raises(PluginHostError):
        await old_proxy.prepare(actor, request)
    refreshed_facade = server.plugin_host.facade("enterprise-knowledge")
    before_knowledge_reload = refreshed_facade.resolve_service("enterprise-knowledge", 1)
    await manager.mutate(lambda: manager.set_enabled("enterprise-knowledge", False))
    await manager.mutate(lambda: manager.set_enabled("enterprise-knowledge", True))
    with pytest.raises(PluginHostError):
        await before_knowledge_reload.prepare(actor, request)

    reloaded_facade = server.plugin_host.facade("enterprise-knowledge")
    current_proxy = reloaded_facade.resolve_service("enterprise-knowledge", 1)
    prepared = await current_proxy.prepare(actor, request)
    assert prepared.operation.status == "VALIDATING"


async def test_retrieval_budget_covers_entry_preflight_and_final_publication(
    env, tmp_path, monkeypatch
) -> None:
    (
        client,
        server,
        _admin_auth,
        employee_auth,
        user_id,
        agent_id,
        context,
        _connection_id,
        facade,
        service,
    ) = await _query_fixture(env, tmp_path, username="budget-employee")
    service_module = import_module(
        "harness_plugin_enterprise_knowledge.enterprise_knowledge.service"
    )
    provider = import_module("harness_plugin_enterprise_knowledge.enterprise_knowledge.weknora")
    operation_budget = service_module.OperationBudget
    requests: list[str] = []

    async def source(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/knowledge-bases"):
            return httpx.Response(
                200, json={"success": True, "data": [{"id": "kb-a", "name": "A"}]}
            )
        if request.url.path.endswith("/knowledge-search"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "knowledge_id": "doc-budget",
                            "knowledge_base_id": "kb-a",
                            "content": "bounded source text",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        provider,
        "_build_async_client",
        lambda _ca: httpx.AsyncClient(transport=httpx.MockTransport(source)),
    )
    operation_service = service_module.OperationService
    original_owned = operation_service._owned_operation

    async def slow_preflight(self, actor, operation_id):
        await asyncio.sleep(0.1)
        return await original_owned(self, actor, operation_id)

    monkeypatch.setattr(operation_service, "_owned_operation", slow_preflight)
    monkeypatch.setattr(
        service_module, "OperationBudget", lambda: operation_budget(operation_seconds=0.05)
    )
    prepared = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 0},
        },
    )
    assert prepared.status_code == 200, prepared.text
    preflight_operation_id = prepared.json()["result"]["operation"]["operation_id"]
    actor = PluginActor(user_id, False, agent_id=agent_id, thread_id=context["thread_id"])
    ticket = issue_execution_ticket(
        server.plugin_host,
        actor,
        plugin_context={"enterprise-knowledge": {"operation_id": preflight_operation_id}},
    )
    token = activate_ticket(server.plugin_host, ticket)
    try:
        with pytest.raises(PluginActionError) as preflight_timeout:
            await service.retrieve_current("synthetic question")
        assert preflight_timeout.value.code == "UPSTREAM_BUDGET_EXCEEDED"
    finally:
        reset_ticket(token)
    assert requests == []
    preflight_record = await facade.get_record("operations", preflight_operation_id)
    assert preflight_record is not None and preflight_record.payload["status"] == "VALIDATING"

    monkeypatch.setattr(operation_service, "_owned_operation", original_owned)
    monkeypatch.setattr(
        service_module, "OperationBudget", lambda: operation_budget(operation_seconds=0.75)
    )
    final_prepared = await client.post(
        "/api/plugins/enterprise-knowledge/actions/operation.prepare",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"expected_session_revision": 0},
        },
    )
    assert final_prepared.status_code == 200, final_prepared.text
    final_operation_id = final_prepared.json()["result"]["operation"]["operation_id"]
    final_ticket = issue_execution_ticket(
        server.plugin_host,
        actor,
        plugin_context={"enterprise-knowledge": {"operation_id": final_operation_id}},
    )
    final_token = activate_ticket(server.plugin_host, final_ticket)
    original_commit = PluginHostFacade.commit
    delayed = [False]

    async def delay_publication(self, writes, activations=(), *, requirements=(), checks=()):
        if not delayed[0] and any(write.namespace == "evidence" for write in writes):
            delayed[0] = True
            await asyncio.sleep(1)
        return await original_commit(
            self, writes, activations, requirements=requirements, checks=checks
        )

    monkeypatch.setattr(PluginHostFacade, "commit", delay_publication)
    try:
        with pytest.raises(PluginActionError) as final_timeout:
            await service.retrieve_current("synthetic question")
        assert final_timeout.value.code == "UPSTREAM_BUDGET_EXCEEDED"
    finally:
        reset_ticket(final_token)
    assert delayed[0] is True
    final_record = await facade.get_record("operations", final_operation_id)
    assert final_record is not None and final_record.payload["status"] == "FAILED"
    assert await facade.list_records("evidence", f'["{final_operation_id}",') == []
