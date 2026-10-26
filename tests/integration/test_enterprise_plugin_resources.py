from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from octop.infra.agents.plugins.host_contracts import PluginActor, RecordWrite
from tests.support.auth import create_agent, create_user, resolve_user_id
from tests.support.enterprise import (
    install_enterprise_knowledge,
    install_synthetic_enterprise_policy,
)


async def test_unregistered_enterprise_resource_is_authenticated_not_found(env) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    response = await client.get(
        "/api/plugins/enterprise-knowledge/resources/00000000-0000-4000-8000-000000000099",
        headers=admin_auth,
    )
    assert response.status_code == 404


async def test_authenticated_evidence_read_uses_scoped_source_and_opaque_cursor(
    env, monkeypatch, tmp_path
) -> None:
    client, server, admin_auth = env
    await install_enterprise_knowledge(server)
    await install_synthetic_enterprise_policy(server, tmp_path)
    employee_auth = await create_user(client, admin_auth, username="reading-employee")
    user_id = await resolve_user_id(client, admin_auth, "reading-employee")
    agent_id = await create_agent(client, employee_auth, name="Reading agent")
    thread_response = await client.post(f"/api/agents/{agent_id}/threads", headers=employee_auth)
    assert thread_response.status_code == 201, thread_response.text
    thread_id = thread_response.json()["thread_id"]
    context = {"agent_id": agent_id, "thread_id": thread_id}
    calls: list[str] = []
    source_content = {
        "doc-a": "文" * 12000,
        "doc-8000": "A" * 8000,
        "doc-8001": "B" * 8001,
        "doc-many": "x" * 101,
        "doc-network": "N" * 12000,
        "doc-family": "F" * 48500,
        "doc-cross": "".join(chr(65 + index % 26) * 100 for index in range(205)),
    }
    document_chunk_counts = {
        "doc-many": 101,
        "doc-cross": 205,
        "doc-empty-all": 2,
        "doc-duplicate": 2,
        "doc-index-drift": 2,
        "doc-page-size": 1,
        "doc-overbudget": 900,
    }
    drift_pages = [False]
    fail_network_continuation = [False]
    network_pages: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/knowledge-bases"):
            data = [{"id": "kb-a", "name": "A"}]
        elif "/knowledge/" in path:
            knowledge_id = path.rsplit("/", 1)[-1]
            data = {"id": knowledge_id, "knowledge_base_id": "kb-a", "title": "A"}
        elif "/chunks/" in path:
            knowledge_id = path.rsplit("/", 1)[-1].split("?", 1)[0]
            page = int(request.url.params["page"])
            if knowledge_id == "doc-network":
                network_pages.append(page)
                if fail_network_continuation[0]:
                    fail_network_continuation[0] = False
                    return httpx.Response(503)
            if knowledge_id in document_chunk_counts or knowledge_id in {"doc-drift", "doc-empty"}:
                total_chunks = document_chunk_counts.get(
                    knowledge_id, 0 if knowledge_id == "doc-empty" else 101
                )
                response_total = total_chunks
                if knowledge_id == "doc-drift" and page == 2 and drift_pages[0]:
                    response_total += 1
                response_page_size = 99 if knowledge_id == "doc-page-size" else 100
                page_items = [
                    {
                        "id": (
                            "duplicate-id"
                            if knowledge_id == "doc-duplicate"
                            else f"chunk-{knowledge_id}-{index}"
                        ),
                        "knowledge_id": knowledge_id,
                        "knowledge_base_id": "kb-a",
                        "chunk_type": "text",
                        "chunk_index": 0 if knowledge_id == "doc-index-drift" else index,
                        "content": (
                            ""
                            if knowledge_id == "doc-empty-all"
                            else chr(65 + index % 26) * 100
                            if knowledge_id == "doc-cross"
                            else "x"
                        ),
                    }
                    for index in range((page - 1) * 100, min(page * 100, response_total))
                ]
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": page_items,
                        "total": response_total,
                        "page": page,
                        "page_size": response_page_size,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": f"chunk-{knowledge_id}",
                            "knowledge_id": knowledge_id,
                            "knowledge_base_id": "kb-a",
                            "chunk_type": "text",
                            "chunk_index": 0,
                            "content": source_content.get(knowledge_id, ""),
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 100,
                },
            )
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    facade = server.plugin_host.facade("enterprise-knowledge")
    service = facade.resolve_service("enterprise-knowledge", 1)
    service_module = sys.modules["harness_plugin_enterprise_knowledge.enterprise_knowledge.service"]
    models_module = sys.modules["harness_plugin_enterprise_knowledge.enterprise_knowledge.models"]
    reading_module = sys.modules[service_module.ReadingService.__module__]
    weknora = sys.modules[service_module.WeKnoraProvider.__module__]

    monkeypatch.setattr(
        weknora,
        "_build_async_client",
        lambda _ca: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    saved = await client.post(
        "/api/plugins/enterprise-knowledge/actions/connection.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "name": "Reading fixture",
                "base_url": "https://knowledge.example",
                "allowed_kb_ids": ["kb-a"],
                "enabled": True,
                "api_key": "READING_FIXTURE_KEY",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    connection = saved.json()["result"]["connection"]
    connection_id = connection["connection_id"]
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
    binding = await client.post(
        "/api/plugins/enterprise-knowledge/actions/binding.save",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": agent_id},
            "payload": {
                "expected_revision": 0,
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a"],
                "default_kb_ids": ["kb-a"],
                "enabled": True,
            },
        },
    )
    assert binding.status_code == 200, binding.text
    facade = server.plugin_host.facade("enterprise-knowledge")
    session = await client.post(
        "/api/plugins/enterprise-knowledge/actions/session.get",
        headers=employee_auth,
        json={"api_version": 1, "context": context, "payload": {}},
    )
    assert session.status_code == 200, session.text
    snapshot = session.json()["result"]["session"]

    operation_id, evidence_id = str(uuid4()), str(uuid4())
    operation_payload = {
        "schema_id": "octop.enterprise.knowledge.v1",
        "version": 1,
        "operation_id": operation_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "thread_id": thread_id,
        "session_revision": snapshot["revision"],
        "config_revision": connection["revision"],
        "binding_revision": binding.json()["result"]["binding"]["revision"],
        "grant_revision": grant.json()["result"]["grant"]["revision"],
        "connection_id": connection_id,
        "status": "COMPLETED",
        "error_code": None,
        "approval_id": None,
    }
    evidence_payload = {
        "schema_id": "octop.enterprise.knowledge.v1",
        "version": 1,
        "evidence_id": evidence_id,
        "operation_id": operation_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "thread_id": thread_id,
        "source_kind": "KNOWLEDGE",
        "provider": "WEKNORA",
        "connection_id": connection_id,
        "kb_id": "kb-a",
        "knowledge_id": "doc-a",
        "title": "A",
        "content": "retrieved snippet",
        "truncated": True,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "source_version": None,
        "chunk_index": 0,
        "source_page": None,
        "locator": None,
    }
    await facade.commit(
        (
            RecordWrite("operations", operation_id, user_id, operation_payload, 0),
            RecordWrite(
                "evidence",
                json.dumps([operation_id, evidence_id], separators=(",", ":")),
                user_id,
                evidence_payload,
                0,
            ),
        )
    )
    exact_evidence_ids = {knowledge_id: str(uuid4()) for knowledge_id in ("doc-8000", "doc-8001")}
    await facade.commit(
        tuple(
            RecordWrite(
                "evidence",
                json.dumps([operation_id, evidence_id], separators=(",", ":")),
                user_id,
                {
                    **evidence_payload,
                    "evidence_id": evidence_id,
                    "knowledge_id": knowledge_id,
                },
                0,
            )
            for knowledge_id, evidence_id in exact_evidence_ids.items()
        )
    )

    async def add_evidence(knowledge_id: str) -> str:
        new_evidence_id = str(uuid4())
        await facade.commit(
            (
                RecordWrite(
                    "evidence",
                    json.dumps([operation_id, new_evidence_id], separators=(",", ":")),
                    user_id,
                    {
                        **evidence_payload,
                        "evidence_id": new_evidence_id,
                        "knowledge_id": knowledge_id,
                    },
                    0,
                ),
            )
        )
        return new_evidence_id

    async def read_action(
        target_evidence_id: str,
        continuation: str | None = None,
        target_context=context,
        target_operation_id=operation_id,
    ) -> httpx.Response:
        payload = {"operation_id": target_operation_id, "evidence_id": target_evidence_id}
        if continuation is not None:
            payload["continuation"] = continuation
        return await client.post(
            "/api/plugins/enterprise-knowledge/actions/evidence.read",
            headers=employee_auth,
            json={"api_version": 1, "context": target_context, "payload": payload},
        )

    async def add_source_pair(
        knowledge_id: str,
        target_thread_id: str,
        *,
        session_revision: int,
        config_revision: int | None = None,
    ) -> tuple[str, str]:
        new_operation_id, new_evidence_id = str(uuid4()), str(uuid4())
        await facade.commit(
            (
                RecordWrite(
                    "operations",
                    new_operation_id,
                    user_id,
                    {
                        **operation_payload,
                        "operation_id": new_operation_id,
                        "thread_id": target_thread_id,
                        "session_revision": session_revision,
                        "config_revision": (
                            config_revision
                            if config_revision is not None
                            else connection["revision"]
                        ),
                    },
                    0,
                ),
                RecordWrite(
                    "evidence",
                    json.dumps([new_operation_id, new_evidence_id], separators=(",", ":")),
                    user_id,
                    {
                        **evidence_payload,
                        "operation_id": new_operation_id,
                        "evidence_id": new_evidence_id,
                        "thread_id": target_thread_id,
                        "knowledge_id": knowledge_id,
                    },
                    0,
                ),
            )
        )
        return new_operation_id, new_evidence_id

    forged = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": evidence_id,
                "continuation": str(uuid4()),
            },
        },
    )
    assert forged.status_code == 400
    assert forged.json()["error"]["code"] == "READ_CURSOR_INVALID"
    assert calls == []

    first = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation_id, "evidence_id": evidence_id},
        },
    )
    assert first.status_code == 200, first.text
    first_page = first.json()["result"]
    assert first_page["content"] == source_content["doc-a"][:8000]
    assert first_page["truncated"] is True
    token = first_page["continuation"]
    assert isinstance(token, str) and len(token) == 36
    typed_page = await service.read(
        PluginActor(user_id, False, agent_id=agent_id, thread_id=thread_id),
        models_module.ReadInput(operation_id=operation_id, evidence_id=evidence_id),
    )
    assert typed_page.content == first_page["content"]
    assert typed_page.continuation != token
    cursor = await facade.get_record("reading_cursors", token)
    assert cursor is not None
    assert "content" not in cursor.payload and "api_key" not in cursor.payload

    calls_before_mismatched = len(calls)
    for mismatched_auth, mismatched_context, mismatched_evidence in (
        (employee_auth, {**context, "thread_id": "other-thread"}, evidence_id),
        (admin_auth, context, evidence_id),
        (employee_auth, context, exact_evidence_ids["doc-8000"]),
    ):
        mismatched = await client.post(
            "/api/plugins/enterprise-knowledge/actions/evidence.read",
            headers=mismatched_auth,
            json={
                "api_version": 1,
                "context": mismatched_context,
                "payload": {
                    "operation_id": operation_id,
                    "evidence_id": mismatched_evidence,
                    "continuation": token,
                },
            },
        )
        assert mismatched.status_code in {403, 404} or (
            mismatched.status_code == 400
            and mismatched.json()["error"]["code"] == "READ_CURSOR_INVALID"
        )
    assert len(calls) == calls_before_mismatched

    second = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": evidence_id,
                "continuation": token,
            },
        },
    )
    assert second.status_code == 200, second.text
    second_page = second.json()["result"]
    assert second_page["content"] == source_content["doc-a"][8000:]
    assert second_page["truncated"] is False
    assert second_page["continuation"] is None
    assert first_page["content"] + second_page["content"] == source_content["doc-a"]

    source_content["doc-a"] = "变" * 12000
    changed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": evidence_id,
                "continuation": token,
            },
        },
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "READ_SOURCE_CHANGED"

    for knowledge_id in ("doc-8000", "doc-8001"):
        exact_evidence_id = exact_evidence_ids[knowledge_id]
        exact = await client.post(
            "/api/plugins/enterprise-knowledge/actions/evidence.read",
            headers=employee_auth,
            json={
                "api_version": 1,
                "context": context,
                "payload": {"operation_id": operation_id, "evidence_id": exact_evidence_id},
            },
        )
        assert exact.status_code == 200, exact.text
        exact_page = exact.json()["result"]
        assert len(exact_page["content"]) == 8000
        assert exact_page["truncated"] is (knowledge_id == "doc-8001")
        if knowledge_id == "doc-8001":
            expired_token = exact_page["continuation"]
            tail = await client.post(
                "/api/plugins/enterprise-knowledge/actions/evidence.read",
                headers=employee_auth,
                json={
                    "api_version": 1,
                    "context": context,
                    "payload": {
                        "operation_id": operation_id,
                        "evidence_id": exact_evidence_id,
                        "continuation": exact_page["continuation"],
                    },
                },
            )
            assert tail.status_code == 200, tail.text
            assert tail.json()["result"]["content"] == "B"
            cursor_record = await facade.get_record("reading_cursors", expired_token)
            assert cursor_record is not None
            expired_payload = {
                **cursor_record.payload,
                "expires_at": "2000-01-01T00:00:00+00:00",
            }
            await facade.commit(
                (
                    RecordWrite(
                        "reading_cursors",
                        expired_token,
                        user_id,
                        expired_payload,
                        cursor_record.revision,
                    ),
                )
            )
            calls_before_expired = len(calls)
            expired = await client.post(
                "/api/plugins/enterprise-knowledge/actions/evidence.read",
                headers=employee_auth,
                json={
                    "api_version": 1,
                    "context": context,
                    "payload": {
                        "operation_id": operation_id,
                        "evidence_id": exact_evidence_id,
                        "continuation": expired_token,
                    },
                },
            )
            assert expired.status_code == 400
            assert expired.json()["error"]["code"] == "READ_CURSOR_INVALID"
            assert len(calls) == calls_before_expired

    many_evidence_id = str(uuid4())
    await facade.commit(
        (
            RecordWrite(
                "evidence",
                json.dumps([operation_id, many_evidence_id], separators=(",", ":")),
                user_id,
                {**evidence_payload, "evidence_id": many_evidence_id, "knowledge_id": "doc-many"},
                0,
            ),
        )
    )
    many = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation_id, "evidence_id": many_evidence_id},
        },
    )
    assert many.status_code == 200, many.text
    assert many.json()["result"]["content"] == "x" * 101
    assert many.json()["result"]["truncated"] is False

    drift_evidence_id = str(uuid4())
    await facade.commit(
        (
            RecordWrite(
                "evidence",
                json.dumps([operation_id, drift_evidence_id], separators=(",", ":")),
                user_id,
                {
                    **evidence_payload,
                    "evidence_id": drift_evidence_id,
                    "knowledge_id": "doc-drift",
                },
                0,
            ),
        )
    )
    drift_pages[0] = True
    cursor_count_before_drift = len(await facade.list_records("reading_cursors"))
    calls_before_drift = len(calls)
    drifted = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation_id, "evidence_id": drift_evidence_id},
        },
    )
    assert drifted.status_code == 503
    assert drifted.json()["error"]["code"] == "UPSTREAM_SCHEMA_INVALID"
    assert len(calls) - calls_before_drift == 4
    assert len(await facade.list_records("reading_cursors")) == cursor_count_before_drift

    network_evidence_id = await add_evidence("doc-network")
    cursor_count_before_network_failure = len(await facade.list_records("reading_cursors"))
    network_first = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation_id, "evidence_id": network_evidence_id},
        },
    )
    assert network_first.status_code == 200, network_first.text
    network_token = network_first.json()["result"]["continuation"]
    network_cursor_before_failure = await facade.get_record("reading_cursors", network_token)
    assert network_cursor_before_failure is not None
    fail_network_continuation[0] = True
    failed_continuation = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": network_evidence_id,
                "continuation": network_token,
            },
        },
    )
    assert failed_continuation.status_code == 503
    assert failed_continuation.json()["error"]["code"] == "UPSTREAM_HTTP_ERROR"
    assert network_pages == [1, 1]
    network_cursor_after_failure = await facade.get_record("reading_cursors", network_token)
    assert network_cursor_after_failure == network_cursor_before_failure
    assert (
        len(await facade.list_records("reading_cursors")) == cursor_count_before_network_failure + 1
    )
    replayed = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": network_evidence_id,
                "continuation": network_token,
            },
        },
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["result"]["content"] == "N" * 4000
    assert replayed.json()["result"]["continuation"] is None
    assert network_pages == [1, 1, 1]

    cross_evidence_id = await add_evidence("doc-cross")
    cross_pages: list[str] = []
    cross_response = await read_action(cross_evidence_id)
    assert cross_response.status_code == 200, cross_response.text
    cross_page = cross_response.json()["result"]
    cross_pages.append(cross_page["content"])
    assert len(cross_page["content"]) == 8000 and cross_page["truncated"] is True
    cross_response = await read_action(cross_evidence_id, cross_page["continuation"])
    assert cross_response.status_code == 200, cross_response.text
    cross_page = cross_response.json()["result"]
    cross_pages.append(cross_page["content"])
    assert len(cross_page["content"]) == 8000 and cross_page["truncated"] is True
    cross_response = await read_action(cross_evidence_id, cross_page["continuation"])
    assert cross_response.status_code == 200, cross_response.text
    cross_page = cross_response.json()["result"]
    cross_pages.append(cross_page["content"])
    assert len(cross_page["content"]) == 4500 and cross_page["continuation"] is None
    assert "".join(cross_pages) == source_content["doc-cross"]

    family_evidence_id = await add_evidence("doc-family")
    real_datetime = reading_module.datetime
    family_clock = [real_datetime(2030, 1, 1, tzinfo=UTC)]

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return family_clock[0] if tz is None else family_clock[0].astimezone(tz)

    monkeypatch.setattr(reading_module, "datetime", FrozenDateTime)
    family_response = await read_action(family_evidence_id)
    assert family_response.status_code == 200, family_response.text
    family_result = family_response.json()["result"]
    family_parts = [family_result["content"]]
    family_token = family_result["continuation"]
    first_cursor = await facade.get_record("reading_cursors", family_token)
    assert first_cursor is not None
    family_expiry = first_cursor.payload["expires_at"]
    assert len(family_parts[0]) == 8000 and family_result["truncated"] is True
    for minutes in (5, 10, 15, 20, 29):
        family_clock[0] = real_datetime(2030, 1, 1, tzinfo=UTC).replace(minute=minutes)
        family_response = await read_action(family_evidence_id, family_token)
        assert family_response.status_code == 200, family_response.text
        family_result = family_response.json()["result"]
        family_parts.append(family_result["content"])
        family_token = family_result["continuation"]
        family_cursor = await facade.get_record("reading_cursors", family_token)
        assert family_cursor is not None
        assert family_cursor.payload["expires_at"] == family_expiry
        assert len(family_result["content"]) == 8000
        assert family_result["truncated"] is True
    assert "".join(family_parts) == source_content["doc-family"][:48000]
    family_clock[0] = real_datetime(2030, 1, 1, tzinfo=UTC).replace(minute=30)
    calls_before_family_expiry = len(calls)
    family_expired = await read_action(family_evidence_id, family_token)
    assert family_expired.status_code == 400
    assert family_expired.json()["error"]["code"] == "READ_CURSOR_INVALID"
    assert len(calls) == calls_before_family_expiry

    for knowledge_id, expected_status, expected_code in (
        ("doc-empty", 200, None),
        ("doc-empty-all", 200, None),
        ("doc-duplicate", 503, "UPSTREAM_SCHEMA_INVALID"),
        ("doc-index-drift", 503, "UPSTREAM_SCHEMA_INVALID"),
        ("doc-page-size", 503, "UPSTREAM_SCHEMA_INVALID"),
    ):
        new_evidence_id = await add_evidence(knowledge_id)
        cursor_count_before_case = len(await facade.list_records("reading_cursors"))
        response = await read_action(new_evidence_id)
        assert response.status_code == expected_status, response.text
        if expected_status == 200:
            assert response.json()["result"]["status"] == "EMPTY"
            assert response.json()["result"]["content"] == ""
            assert response.json()["result"]["truncated"] is False
            assert response.json()["result"]["continuation"] is None
        else:
            assert response.json()["error"]["code"] == expected_code
            assert len(await facade.list_records("reading_cursors")) == cursor_count_before_case

    async def create_context_thread() -> str:
        response = await client.post(f"/api/agents/{agent_id}/threads", headers=employee_auth)
        assert response.status_code == 201, response.text
        return response.json()["thread_id"]

    for stored_mode, stored_config_revision in (
        ("WEB_ONLY", connection["revision"]),
        ("KB_ONLY", connection["revision"] - 1),
    ):
        scoped_thread = await create_context_thread()
        session_key = json.dumps([user_id, agent_id, scoped_thread], separators=(",", ":"))
        await facade.commit(
            (
                RecordWrite(
                    "sessions",
                    session_key,
                    user_id,
                    {
                        "schema_id": "octop.enterprise.knowledge.v1",
                        "version": 1,
                        "user_id": user_id,
                        "agent_id": agent_id,
                        "thread_id": scoped_thread,
                        "selected_kb_ids": ["kb-a"],
                        "mode": stored_mode,
                        "connection_id": connection_id,
                        "config_revision": stored_config_revision,
                        "binding_revision": binding.json()["result"]["binding"]["revision"],
                        "grant_revision": grant.json()["result"]["grant"]["revision"],
                    },
                    0,
                ),
            )
        )
        scoped_operation_id, scoped_evidence_id = await add_source_pair(
            "doc-8000", scoped_thread, session_revision=1
        )
        calls_before_local_rejection = len(calls)
        rejected = await read_action(
            scoped_evidence_id,
            target_context={"agent_id": agent_id, "thread_id": scoped_thread},
            target_operation_id=scoped_operation_id,
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "READ_SOURCE_CHANGED"
        assert len(calls) == calls_before_local_rejection

    stale_config_operation, stale_config_evidence = await add_source_pair(
        "doc-8000",
        thread_id,
        session_revision=snapshot["revision"],
        config_revision=connection["revision"] - 1,
    )
    calls_before_stale_config = len(calls)
    stale_config = await read_action(
        stale_config_evidence, target_operation_id=stale_config_operation
    )
    assert stale_config.status_code == 409
    assert stale_config.json()["error"]["code"] == "READ_SOURCE_CHANGED"
    assert len(calls) == calls_before_stale_config

    quarantine_thread = await create_context_thread()
    quarantine_operation, quarantine_evidence = await add_source_pair(
        "doc-8000", quarantine_thread, session_revision=0
    )
    intent = await facade.get_record("connection_intents", connection_id)
    intent_payload = dict(intent.payload) if intent is not None else {"state": "RESOLVED"}
    await facade.commit(
        (
            RecordWrite(
                "connection_intents",
                connection_id,
                intent.owner_user_id if intent is not None else user_id,
                {**intent_payload, "state": "BLOCKED"},
                intent.revision if intent is not None else 0,
            ),
        )
    )
    calls_before_quarantine = len(calls)
    quarantined = await read_action(
        quarantine_evidence,
        target_context={"agent_id": agent_id, "thread_id": quarantine_thread},
        target_operation_id=quarantine_operation,
    )
    assert quarantined.status_code == 409
    assert quarantined.json()["error"]["code"] == "RECOVERY_BLOCKED"
    assert len(calls) == calls_before_quarantine
    blocked_intent = await facade.get_record("connection_intents", connection_id)
    assert blocked_intent is not None
    await facade.commit(
        (
            RecordWrite(
                "connection_intents",
                connection_id,
                blocked_intent.owner_user_id,
                intent_payload,
                blocked_intent.revision,
            ),
        )
    )

    source_content["doc-a"] = "文" * 12000
    facade_type = type(facade)
    original_commit = facade_type.commit
    for raced_namespace, raced_key in (
        ("operations", operation_id),
        ("connection_intents", connection_id),
    ):
        raced_record = [False]

        async def change_source_before_cursor_commit(
            current_facade,
            writes,
            activations=(),
            *,
            requirements=(),
            checks=(),
            _raced_namespace=raced_namespace,
            _raced_key=raced_key,
            _raced_record=raced_record,
        ):
            if (
                any(write.namespace == "reading_cursors" for write in writes)
                and not _raced_record[0]
            ):
                _raced_record[0] = True
                current = await current_facade.get_record(_raced_namespace, _raced_key)
                payload = dict(current.payload) if current is not None else {}
                await original_commit(
                    current_facade,
                    (
                        RecordWrite(
                            _raced_namespace,
                            _raced_key,
                            current.owner_user_id if current is not None else user_id,
                            {**payload, "race_marker": str(uuid4())},
                            current.revision if current is not None else 0,
                        ),
                    ),
                )
            return await original_commit(
                current_facade, writes, activations, requirements=requirements, checks=checks
            )

        monkeypatch.setattr(facade_type, "commit", change_source_before_cursor_commit)
        cursor_count_before_race = len(await facade.list_records("reading_cursors"))
        raced = await client.post(
            "/api/plugins/enterprise-knowledge/actions/evidence.read",
            headers=employee_auth,
            json={
                "api_version": 1,
                "context": context,
                "payload": {"operation_id": operation_id, "evidence_id": evidence_id},
            },
        )
        assert raced_record[0] is True
        assert raced.status_code == 409
        assert raced.json()["error"]["code"] == "READ_SOURCE_CHANGED"
        assert len(await facade.list_records("reading_cursors")) == cursor_count_before_race
        monkeypatch.setattr(facade_type, "commit", original_commit)

    refreshed_for_revocation = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation_id, "evidence_id": evidence_id},
        },
    )
    assert refreshed_for_revocation.status_code == 200, refreshed_for_revocation.text
    revocation_token = refreshed_for_revocation.json()["result"]["continuation"]

    overbudget_evidence_id = str(uuid4())
    await facade.commit(
        (
            RecordWrite(
                "evidence",
                json.dumps([operation_id, overbudget_evidence_id], separators=(",", ":")),
                user_id,
                {
                    **evidence_payload,
                    "evidence_id": overbudget_evidence_id,
                    "knowledge_id": "doc-overbudget",
                },
                0,
            ),
        )
    )
    cursor_count_before_budget_failure = len(await facade.list_records("reading_cursors"))
    calls_before_budget_failure = len(calls)
    overbudget = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": overbudget_evidence_id,
            },
        },
    )
    assert overbudget.status_code == 503
    assert overbudget.json()["error"]["code"] == "UPSTREAM_BUDGET_EXCEEDED"
    assert len(calls) - calls_before_budget_failure == 10
    assert len(await facade.list_records("reading_cursors")) == cursor_count_before_budget_failure

    calls_before_original = len(calls)
    unavailable = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.original",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {"operation_id": operation_id, "evidence_id": evidence_id},
        },
    )
    assert unavailable.status_code == 200, unavailable.text
    assert unavailable.json()["result"] == {
        "schema_id": "octop.enterprise.knowledge.v1",
        "version": 1,
        "operation_id": operation_id,
        "evidence_id": evidence_id,
        "available": False,
        "reason": "CAPABILITY_UNVERIFIED",
        "resource_token": None,
        "locator": None,
    }
    assert len(calls) == calls_before_original

    revoked = await client.post(
        "/api/plugins/enterprise-knowledge/actions/grant.save",
        headers=admin_auth,
        json={
            "api_version": 1,
            "payload": {
                "connection_id": connection_id,
                "expected_revision": grant.json()["result"]["grant"]["revision"],
                "user_id": user_id,
                "user_allowed_kb_ids": [],
            },
        },
    )
    assert revoked.status_code == 200, revoked.text
    calls_before_revoked_read = len(calls)
    revoked_read = await client.post(
        "/api/plugins/enterprise-knowledge/actions/evidence.read",
        headers=employee_auth,
        json={
            "api_version": 1,
            "context": context,
            "payload": {
                "operation_id": operation_id,
                "evidence_id": evidence_id,
                "continuation": revocation_token,
            },
        },
    )
    assert revoked_read.status_code == 409
    assert revoked_read.json()["error"]["code"] == "READ_SOURCE_CHANGED"
    assert len(calls) == calls_before_revoked_read
