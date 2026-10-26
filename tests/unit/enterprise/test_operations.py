from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_connections = import_module("enterprise_knowledge.connections")
ConnectionService = _connections.ConnectionService
OperationService = import_module("enterprise_knowledge.operations").OperationService
EvidenceService = import_module("enterprise_knowledge.evidence").EvidenceService
SessionSnapshot = import_module("enterprise_knowledge.models").SessionSnapshot


class _MemoryFacade:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], HostRecord] = {}

    async def get_record(self, namespace: str, logical_key: str) -> HostRecord | None:
        return self.records.get((namespace, logical_key))

    async def list_records(self, namespace: str, prefix: str = "") -> list[HostRecord]:
        return [
            record
            for (ns, key), record in self.records.items()
            if ns == namespace and key.startswith(prefix)
        ]

    async def commit(
        self,
        writes: tuple[RecordWrite, ...],
        *,
        checks: tuple[RecordCheck, ...] = (),
        **_kwargs: object,
    ) -> list[HostRecord]:
        for check in checks:
            current = self.records.get((check.namespace, check.logical_key))
            if (current.revision if current else 0) != check.expected_revision:
                raise PluginHostError("conflict")
        for write in writes:
            current = self.records.get((write.namespace, write.logical_key))
            if (current.revision if current else 0) != write.expected_revision:
                raise PluginHostError("conflict")
        output = []
        for write in writes:
            current = self.records.get((write.namespace, write.logical_key))
            record = HostRecord(
                id=write.logical_key,
                namespace=write.namespace,
                logical_key=write.logical_key,
                owner_user_id=write.owner_user_id,
                revision=(current.revision + 1) if current else 1,
                payload=write.payload,
            )
            self.records[(write.namespace, write.logical_key)] = record
            output.append(record)
        return output


class _PriorIntentFacade:
    async def get_record(self, namespace: str, logical_key: str) -> HostRecord:
        assert namespace == "connection_intents"
        return HostRecord(
            id="record-1",
            namespace=namespace,
            logical_key=logical_key,
            owner_user_id=1,
            revision=1,
            payload={"state": "BLOCKED"},
        )


def test_concurrent_start_rejection_uses_actor_locale() -> None:
    actor = PluginActor(user_id=1, is_admin=True, locale="zh")
    service = ConnectionService(_PriorIntentFacade())

    with pytest.raises(PluginActionError) as caught:
        asyncio.run(
            service._start_connection_intent(
                "00000000-0000-4000-8000-000000000001",
                1,
                None,
                None,
                None,
                None,
                actor=actor,
            )
        )

    assert caught.value.code == "RECOVERY_BLOCKED"
    assert caught.value.message == "连接恢复尚未完成，已阻止新的知识调用"


def test_operation_terminal_state_cannot_be_rewritten_and_retry_is_new_identity() -> None:
    facade = _MemoryFacade()
    service = OperationService(facade)
    actor = PluginActor(user_id=7, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    session = SessionSnapshot(
        agent_id="agent-1", thread_id="thread-1", revision=0, selection_present=False
    )

    first = asyncio.run(service.create(actor, session))
    completed = asyncio.run(service.transition(actor, first.operation_id, 1, "RETRIEVING"))
    completed = asyncio.run(
        service.transition(actor, first.operation_id, completed.revision, "EMPTY")
    )
    with pytest.raises(PluginActionError) as caught:
        asyncio.run(service.transition(actor, first.operation_id, completed.revision, "RETRIEVING"))
    assert caught.value.code == "CONFIG_CONFLICT"
    retry = asyncio.run(service.create(actor, session))
    assert retry.operation_id != first.operation_id


@pytest.mark.parametrize(
    ("path", "terminal"),
    [
        ((), "DENIED"),
        ((), "FAILED"),
        ((), "CANCELLED"),
        ((), "INTERRUPTED"),
        (("RETRIEVING",), "EMPTY"),
        (("RETRIEVING", "GENERATING"), "COMPLETED"),
    ],
)
def test_every_operation_terminal_state_rejects_late_updates(path, terminal) -> None:
    facade = _MemoryFacade()
    service = OperationService(facade)
    actor = PluginActor(user_id=7, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    session = SessionSnapshot(
        agent_id="agent-1", thread_id="thread-1", revision=0, selection_present=False
    )
    operation = asyncio.run(service.create(actor, session))
    for status in (*path, terminal):
        operation = asyncio.run(
            service.transition(actor, operation.operation_id, operation.revision, status)
        )
    with pytest.raises(PluginActionError):
        asyncio.run(
            service.transition(actor, operation.operation_id, operation.revision, "RETRIEVING")
        )


def test_cancelled_public_query_approval_cannot_be_consumed() -> None:
    facade = _MemoryFacade()
    service = OperationService(facade)
    actor = PluginActor(user_id=7, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    session = SessionSnapshot(
        agent_id="agent-1", thread_id="thread-1", revision=0, selection_present=False
    )
    operation = asyncio.run(service.create(actor, session))
    query = "  exact\tuser query  "
    approval = asyncio.run(service.approve(actor, operation.operation_id, query))
    assert approval.public_query == query
    cancelled = asyncio.run(service.cancel_approval(actor, approval.approval_id, 1))
    assert cancelled.state == "CANCELLED"
    with pytest.raises(PluginActionError):
        asyncio.run(service.consume_approval(actor, approval.approval_id, 2))


def test_replaced_approval_cannot_be_consumed_and_current_query_is_exact() -> None:
    facade = _MemoryFacade()
    service = OperationService(facade)
    actor = PluginActor(user_id=7, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    session = SessionSnapshot(
        agent_id="agent-1", thread_id="thread-1", revision=0, selection_present=False
    )
    operation = asyncio.run(service.create(actor, session))
    obsolete = asyncio.run(service.approve(actor, operation.operation_id, "old query"))
    current_query = " \tquery remains exact\n"
    current = asyncio.run(service.approve(actor, operation.operation_id, current_query))
    with pytest.raises(PluginActionError):
        asyncio.run(service.consume_approval(actor, obsolete.approval_id, 1))
    assert asyncio.run(service.consume_approval(actor, current.approval_id, 1)) == current_query


def test_evidence_requires_successful_source_and_all_knowledge_ids() -> None:
    facade = _MemoryFacade()
    operations = OperationService(facade)
    actor = PluginActor(user_id=7, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    connection_id = "00000000-0000-4000-8000-000000000001"
    session = SessionSnapshot(
        agent_id="agent-1",
        thread_id="thread-1",
        revision=0,
        selection_present=False,
        connection_id=connection_id,
        config_revision=1,
        binding_revision=1,
        grant_revision=1,
    )
    for namespace, key, payload in (
        ("connections", connection_id, {"enabled": True}),
        ("bindings", "agent-1", {"enabled": True}),
        ("grants", f"{connection_id}:7", {"user_allowed_kb_ids": ["kb-1"]}),
    ):
        facade.records[(namespace, key)] = HostRecord(
            id=key,
            namespace=namespace,
            logical_key=key,
            owner_user_id=7,
            revision=1,
            payload=payload,
        )
    operation = asyncio.run(operations.create(actor, session))
    operation = asyncio.run(operations.transition(actor, operation.operation_id, 1, "RETRIEVING"))
    evidence = EvidenceService(facade)
    with pytest.raises(PluginActionError):
        asyncio.run(
            evidence.record_successful_source(
                actor, operation.operation_id, outcome="FAILED", items=[]
            )
        )
    with pytest.raises(ValidationError):
        asyncio.run(
            evidence.record_successful_source(
                actor,
                operation.operation_id,
                outcome="SUCCESS",
                items=[
                    {
                        "source_kind": "KNOWLEDGE",
                        "connection_id": connection_id,
                        "kb_id": "kb-1",
                        "knowledge_id": "doc-1",
                        "content": "valid first item",
                        "truncated": False,
                    },
                    {"source_kind": "KNOWLEDGE", "content": "untrusted", "truncated": False},
                ],
            )
        )
    assert asyncio.run(evidence.list_for_operation(actor, operation.operation_id)) == []
    saved = asyncio.run(
        evidence.record_successful_source(
            actor,
            operation.operation_id,
            outcome="SUCCESS",
            items=[
                {
                    "source_kind": "KNOWLEDGE",
                    "connection_id": connection_id,
                    "kb_id": "kb-1",
                    "knowledge_id": "doc-1",
                    "title": "",
                    "content": "verified source content",
                    "truncated": False,
                }
            ],
        )
    )
    assert len(saved) == 1
    assert (
        asyncio.run(evidence.get(actor, operation.operation_id, saved[0].evidence_id)) == saved[0]
    )
    stranger = PluginActor(user_id=8, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    with pytest.raises(PluginActionError):
        asyncio.run(evidence.get(stranger, operation.operation_id, saved[0].evidence_id))
    other_thread = PluginActor(user_id=7, is_admin=False, agent_id="agent-1", thread_id="thread-2")
    with pytest.raises(PluginActionError):
        asyncio.run(evidence.get(other_thread, operation.operation_id, saved[0].evidence_id))
