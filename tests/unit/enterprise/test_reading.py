from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
_models = import_module("enterprise_knowledge.models")
_actions = import_module("enterprise_knowledge.actions")
_reading = import_module("enterprise_knowledge.reading")
_connections = import_module("enterprise_knowledge.connections")
_evidence = import_module("enterprise_knowledge.evidence")

_CONNECTION_ID = "00000000-0000-4000-8000-000000000001"
_OPERATION_ID = "00000000-0000-4000-8000-000000000002"
_EVIDENCE_ID = "00000000-0000-4000-8000-000000000003"
_ACTOR = PluginActor(1, False, agent_id="agent-a", thread_id="thread-a")


def test_read_actions_are_authenticated_and_have_strict_contracts() -> None:
    actions = {action.name: action for action in _actions.register_actions(object())}
    assert actions["evidence.read"].permission == "authenticated"
    assert actions["evidence.original"].permission == "authenticated"
    read = _models.ReadInput.model_validate(
        {
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "evidence_id": "00000000-0000-4000-8000-000000000002",
        }
    )
    assert read.continuation is None
    with pytest.raises(ValueError):
        _models.ReadInput.model_validate(
            {
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "evidence_id": "00000000-0000-4000-8000-000000000002",
                "document_id": "client-selected-document",
            }
        )


def test_read_page_contract_rejects_boolean_schema_version() -> None:
    with pytest.raises(ValueError):
        _models.ReadPage.model_validate(
            {
                "schema_id": "octop.enterprise.knowledge.v1",
                "version": True,
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "evidence_id": "00000000-0000-4000-8000-000000000002",
                "knowledge_id": "doc-a",
                "status": "SUCCESS",
                "content": "x",
                "truncated": False,
                "continuation": None,
                "source_version": None,
            }
        )


def test_read_page_contract_caps_content_at_8000_characters() -> None:
    with pytest.raises(ValueError):
        _models.ReadPage.model_validate(
            {
                "schema_id": "octop.enterprise.knowledge.v1",
                "version": 1,
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "evidence_id": "00000000-0000-4000-8000-000000000002",
                "knowledge_id": "doc-a",
                "status": "SUCCESS",
                "content": "x" * 8001,
                "truncated": False,
                "continuation": None,
                "source_version": None,
            }
        )


class _Facade:
    def __init__(self, records: dict[tuple[str, str], HostRecord]) -> None:
        self.records = records
        self.pause_operation_read: tuple[asyncio.Event, asyncio.Event] | None = None
        self.mutate_evidence_on_first_read = False
        self._evidence_reads = 0
        self.race_on_empty_commit_number: int | None = None
        self.empty_commit_count = 0

    async def get_record(self, namespace: str, key: str) -> HostRecord | None:
        if namespace == "operations" and self.pause_operation_read is not None:
            entered, resume = self.pause_operation_read
            entered.set()
            await resume.wait()
        record = self.records.get((namespace, key))
        if namespace == "evidence" and self.mutate_evidence_on_first_read:
            self._evidence_reads += 1
            if self._evidence_reads == 1 and record is not None:
                self.records[(namespace, key)] = HostRecord(
                    id=record.id,
                    namespace=record.namespace,
                    logical_key=record.logical_key,
                    owner_user_id=record.owner_user_id,
                    revision=record.revision + 1,
                    payload={**record.payload, "knowledge_id": "doc-b"},
                )
        return record

    async def list_records(self, namespace: str, prefix: str = "") -> list[HostRecord]:
        return [
            record
            for (stored_namespace, key), record in self.records.items()
            if stored_namespace == namespace and key.startswith(prefix)
        ]

    async def commit(
        self,
        writes: tuple[RecordWrite, ...],
        activations: tuple[Any, ...] = (),
        *,
        requirements: tuple[Any, ...] = (),
        checks: tuple[RecordCheck, ...] = (),
    ) -> tuple[HostRecord, ...]:
        del activations, requirements
        if not writes:
            self.empty_commit_count += 1
            if self.empty_commit_count == self.race_on_empty_commit_number:
                key = ("connection_intents", _CONNECTION_ID)
                current = self.records.get(key)
                revision = current.revision if current is not None else 0
                self.records[key] = HostRecord(
                    id=_CONNECTION_ID,
                    namespace="connection_intents",
                    logical_key=_CONNECTION_ID,
                    owner_user_id=1,
                    revision=revision + 1,
                    payload={"state": "RESOLVED"},
                )
        for check in checks:
            current = self.records.get((check.namespace, check.logical_key))
            revision = current.revision if current is not None else 0
            if revision != check.expected_revision:
                raise PluginHostError("conflict")
        result = []
        for write in writes:
            key = (write.namespace, write.logical_key)
            current = self.records.get(key)
            revision = current.revision if current is not None else 0
            if revision != write.expected_revision:
                raise PluginHostError("conflict")
            record = HostRecord(
                id=write.logical_key,
                namespace=write.namespace,
                logical_key=write.logical_key,
                owner_user_id=write.owner_user_id,
                revision=revision + 1,
                payload=write.payload,
            )
            self.records[key] = record
            result.append(record)
        return tuple(result)


class _PausedProvider:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.closed = False

    async def visible_kb_ids(self, _connection_id: str, _actor: PluginActor) -> list[str]:
        self.entered.set()
        await self.resume.wait()
        return ["kb-a"]

    async def aclose(self) -> None:
        self.closed = True


def _service_fixture() -> _Facade:
    records: dict[tuple[str, str], HostRecord] = {}

    def add(namespace: str, key: str, revision: int, payload: dict[str, Any]) -> None:
        records[(namespace, key)] = HostRecord(
            id=key,
            namespace=namespace,
            logical_key=key,
            owner_user_id=1,
            revision=revision,
            payload=payload,
        )

    add(
        "connections",
        _CONNECTION_ID,
        1,
        {"enabled": True, "allowed_kb_ids": ["kb-a"]},
    )
    add(
        "bindings",
        "agent-a",
        1,
        {
            "enabled": True,
            "connection_ref": _CONNECTION_ID,
            "expert_allowed_kb_ids": ["kb-a"],
            "default_kb_ids": ["kb-a"],
        },
    )
    add(
        "grants",
        f"{_CONNECTION_ID}:1",
        1,
        {"user_allowed_kb_ids": ["kb-a"]},
    )
    add(
        "operations",
        _OPERATION_ID,
        1,
        {
            "operation_id": _OPERATION_ID,
            "user_id": 1,
            "agent_id": "agent-a",
            "thread_id": "thread-a",
            "session_revision": 0,
            "config_revision": 1,
            "binding_revision": 1,
            "grant_revision": 1,
            "connection_id": _CONNECTION_ID,
            "status": "COMPLETED",
        },
    )
    evidence_payload = {
        "schema_id": "octop.enterprise.knowledge.v1",
        "version": 1,
        "evidence_id": _EVIDENCE_ID,
        "operation_id": _OPERATION_ID,
        "user_id": 1,
        "agent_id": "agent-a",
        "thread_id": "thread-a",
        "source_kind": "KNOWLEDGE",
        "provider": "WEKNORA",
        "connection_id": _CONNECTION_ID,
        "kb_id": "kb-a",
        "knowledge_id": "doc-a",
        "title": "A",
        "content": "snippet",
        "truncated": False,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }
    key = json.dumps([_OPERATION_ID, _EVIDENCE_ID], separators=(",", ":"))
    add("evidence", key, 1, evidence_payload)
    return _Facade(records)


class _ReadProvider:
    def __init__(self, content: str | None = "source text") -> None:
        self.content = content

    async def visible_kb_ids(self, _connection_id: str, _actor: PluginActor) -> list[str]:
        return ["kb-a"]

    async def knowledge_detail(self, knowledge_id: str, _allowed: list[str]) -> Any:
        return SimpleNamespace(knowledge_id=knowledge_id, knowledge_base_id="kb-a")

    async def chunks(
        self, knowledge_id: str, _allowed: list[str], *, page: int, page_size: int
    ) -> Any:
        items = ()
        total = 0
        if self.content is not None:
            items = (
                SimpleNamespace(
                    id="chunk-a",
                    knowledge_id=knowledge_id,
                    knowledge_base_id="kb-a",
                    chunk_type="text",
                    chunk_index=0,
                    content=self.content,
                ),
            )
            total = 1
        return SimpleNamespace(items=items, total=total, page=page, page_size=page_size)

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("action", ["read", "original"])
def test_evidence_revision_cannot_back_a_different_payload(action: str) -> None:
    async def run() -> None:
        facade = _service_fixture()
        facade.mutate_evidence_on_first_read = True
        service = _reading.ReadingService(
            facade, provider_factory=lambda *_args, **_kwargs: _ReadProvider()
        )
        if action == "read":
            request = _models.ReadInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID)
            read = service.read(_ACTOR, request)
        else:
            request = _models.OriginalInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID)
            read = service.original(_ACTOR, request)
        with pytest.raises(PluginActionError) as error:
            await read
        assert error.value.code == "READ_SOURCE_CHANGED"

    asyncio.run(run())


def test_original_output_is_constructed_before_final_source_check(monkeypatch) -> None:
    constructed = False
    original_output = _reading.OriginalOutput
    original_final_check = _reading.ReadingService._final_check

    def track_output(**kwargs: Any) -> Any:
        nonlocal constructed
        constructed = True
        return original_output(**kwargs)

    async def assert_output_precedes_check(self, *args: Any, **kwargs: Any) -> None:
        assert constructed is True
        await original_final_check(self, *args, **kwargs)

    monkeypatch.setattr(_reading, "OriginalOutput", track_output)
    monkeypatch.setattr(_reading.ReadingService, "_final_check", assert_output_precedes_check)

    async def run() -> None:
        result = await _reading.ReadingService(_service_fixture()).original(
            _ACTOR,
            _models.OriginalInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID),
        )
        assert result.operation_id == _OPERATION_ID

    asyncio.run(run())


@pytest.mark.parametrize("content", ["short source", ""])
def test_complete_and_empty_final_checks_reject_intent_race(content: str) -> None:
    async def run() -> None:
        facade = _service_fixture()
        facade.race_on_empty_commit_number = 2
        service = _reading.ReadingService(
            facade, provider_factory=lambda *_args, **_kwargs: _ReadProvider(content)
        )
        request = _models.ReadInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID)
        with pytest.raises(PluginActionError) as error:
            await service.read(_ACTOR, request)
        assert error.value.code == "READ_SOURCE_CHANGED"
        assert await facade.list_records("reading_cursors") == []

    asyncio.run(run())


def test_scan_result_retains_only_one_bounded_window_of_text() -> None:
    async def run() -> None:
        facade = _service_fixture()
        service = _reading.ReadingService(
            facade, provider_factory=lambda *_args, **_kwargs: _ReadProvider("x" * 50000)
        )
        request = _models.ReadInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID)
        result = await service.read(_ACTOR, request)
        assert len(result.content) == 8000
        retained = {field.name for field in _reading._ScanResult.__dataclass_fields__.values()}
        assert "chunks" not in retained and "full_content" not in retained
        assert retained == {
            "digest",
            "total_chunks",
            "text_length",
            "start_offset",
            "content",
            "next_position",
        }

    asyncio.run(run())


def test_cancelled_source_listing_closes_provider() -> None:
    async def run() -> None:
        facade = _service_fixture()
        provider = _PausedProvider()
        service = _reading.ReadingService(
            facade, provider_factory=lambda *_args, **_kwargs: provider
        )
        request = _models.ReadInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID)
        task = asyncio.create_task(service.read(_ACTOR, request))
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.closed is True
        assert await facade.list_records("reading_cursors") == []

    asyncio.run(run())


def test_service_deadline_bounds_a_blocked_host_read() -> None:
    class ShortBudget:
        def __init__(self) -> None:
            pass

        def remaining_lifetime_seconds(self) -> float:
            return 0.01

    async def run() -> None:
        facade = _service_fixture()
        entered, resume = asyncio.Event(), asyncio.Event()
        facade.pause_operation_read = (entered, resume)
        original = _reading.OperationBudget
        _reading.OperationBudget = ShortBudget
        try:
            service = _reading.ReadingService(facade)
            request = _models.ReadInput(operation_id=_OPERATION_ID, evidence_id=_EVIDENCE_ID)
            with pytest.raises(PluginActionError) as error:
                await service.read(_ACTOR, request)
            assert error.value.code == "UPSTREAM_BUDGET_EXCEEDED"
        finally:
            _reading.OperationBudget = original
            resume.set()

    asyncio.run(run())
