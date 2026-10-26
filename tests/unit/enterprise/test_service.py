from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    PluginHostError,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_service = import_module("enterprise_knowledge.service")


def _operation_record(
    operation_id: str, status: str, revision: int = 4, error_code: str | None = None
):
    from octop.infra.agents.plugins.host_contracts import HostRecord

    return HostRecord(
        id="record-id",
        namespace="operations",
        logical_key=operation_id,
        owner_user_id=4,
        revision=revision,
        payload={
            "operation_id": operation_id,
            "user_id": 4,
            "agent_id": "agent-a",
            "thread_id": "thread-a",
            "session_revision": 0,
            "config_revision": 1,
            "binding_revision": 1,
            "grant_revision": 1,
            "connection_id": None,
            "status": status,
            "error_code": error_code,
        },
    )


class _TerminalFacade:
    def __init__(self, execution: object, record) -> None:
        self.execution = execution
        self.calls = 0
        self.record = record
        self.writes = []

    async def current_execution(self) -> object:
        self.calls += 1
        return self.execution

    async def get_record(self, _namespace: str, _key: str):
        return self.record

    async def commit(self, writes, **_kwargs):
        self.writes.extend(writes)
        write = writes[0]
        self.record = _operation_record(
            write.logical_key,
            write.payload["status"],
            self.record.revision + 1,
            write.payload.get("error_code"),
        )
        return [self.record]


class _ExecutionFacade:
    def __init__(self, execution: object) -> None:
        self.execution = execution
        self.calls = 0

    async def current_execution(self) -> object:
        self.calls += 1
        return self.execution


@pytest.mark.asyncio
async def test_current_operation_requires_exact_pair_and_canonical_context_uuid() -> None:
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation_id = str(uuid4())
    valid = {
        "actor": actor,
        "context": {"operation_id": operation_id},
        "execution_kind": "turn",
        "required_plugins": {
            "enterprise-knowledge": "0.1.0",
            "enterprise-policy": "0.1.0",
        },
    }
    facade = _ExecutionFacade(SimpleNamespace(**valid))
    service = _service.KnowledgeService(facade)
    first = await service._current_operation()
    second = await service._current_operation()
    assert first[2] == operation_id == second[2]
    assert facade.calls == 2

    for changes in (
        {"required_plugins": {"enterprise-knowledge": "0.1.0"}},
        {
            "required_plugins": {
                **valid["required_plugins"],
                "unrelated": "1.0.0",
            }
        },
        {"execution_kind": "http"},
        {"context": {"operation_id": "00000000-0000-1000-8000-000000000001"}},
        {"execution_kind": "resume"},
        {"execution_kind": "continuation"},
    ):
        execution = SimpleNamespace(**{**valid, **changes})
        with pytest.raises(PluginActionError):
            await _service.KnowledgeService(_ExecutionFacade(execution))._current_operation()


@pytest.mark.asyncio
async def test_owned_tools_matches_only_actual_enabled_async_functions(monkeypatch) -> None:
    def own_list() -> None:
        return None

    async def own_search() -> None:
        return None

    async def own_read() -> None:
        return None

    async def own_arbitrary() -> None:
        return None

    async def foreign() -> None:
        return None

    callbacks = {
        "list": own_list,
        "search": own_search,
        "read": own_read,
        "arbitrary": own_arbitrary,
    }
    service = _service.KnowledgeService(object(), tool_functions=callbacks)
    callbacks["read"] = foreign
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation = object()

    async def current_generating():
        return actor, operation

    checked = False

    async def final_check(_actor, _operation):
        nonlocal checked
        checked = True

    monkeypatch.setattr(service, "_current_generating", current_generating)
    monkeypatch.setattr(service, "_final_current_check", final_check)
    tools = [
        SimpleNamespace(name="list_renamed", coroutine=own_list),
        SimpleNamespace(name="foreign_same_name", coroutine=foreign),
        SimpleNamespace(name="search_renamed", coroutine=own_search, func=own_search),
        SimpleNamespace(name="mismatched_search", coroutine=foreign, func=own_search),
        SimpleNamespace(name="not_a_fixed_role", coroutine=own_arbitrary),
    ]

    assert await service.owned_tools_current(tools) == {
        "list": "list_renamed",
        "search": "search_renamed",
    }
    assert checked


@pytest.mark.asyncio
async def test_owned_tools_denies_missing_current_context_and_source_drift(monkeypatch) -> None:
    service = _service.KnowledgeService(object(), tool_functions={})
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation = object()
    final_checks = 0

    async def unavailable_context():
        raise PluginActionError("ACCESS_DENIED", "denied", status=403)

    async def final_check(_actor, _operation):
        nonlocal final_checks
        final_checks += 1
        raise PluginActionError("READ_SOURCE_CHANGED", "changed", status=409)

    monkeypatch.setattr(service, "_current_generating", unavailable_context)
    monkeypatch.setattr(service, "_final_current_check", final_check)
    with pytest.raises(PluginActionError) as denied:
        await service.owned_tools_current([])
    assert denied.value.code == "ACCESS_DENIED"
    assert final_checks == 0

    async def current_generating():
        return actor, operation

    monkeypatch.setattr(service, "_current_generating", current_generating)
    with pytest.raises(PluginActionError) as changed:
        await service.owned_tools_current([])
    assert changed.value.code == "READ_SOURCE_CHANGED"
    assert final_checks == 1


@pytest.mark.asyncio
async def test_terminal_methods_use_closed_codes_and_preserve_non_owned_statuses() -> None:
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation_id = str(uuid4())
    execution = SimpleNamespace(
        actor=actor,
        context={"operation_id": operation_id},
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )
    facade = _TerminalFacade(execution, _operation_record(operation_id, "GENERATING"))
    service = _service.KnowledgeService(facade)

    with pytest.raises(PluginHostError):
        await service.deny_current("FAILED")
    with pytest.raises(PluginHostError):
        await service.fail_current(operation_id, "DENIED")
    assert facade.writes == []

    denied = await service.deny_current("ACCESS_DENIED")
    assert denied.operation.status == "GENERATING"
    assert facade.writes == []

    with pytest.raises(PluginActionError) as mismatch:
        await service.fail_current(str(uuid4()), "ACCESS_DENIED")
    assert mismatch.value.code == "ACCESS_DENIED"
    assert facade.writes == []

    failed = await service.fail_current(operation_id, "READ_SOURCE_CHANGED")
    assert failed.operation.status == "FAILED"
    assert failed.operation.error_code == "READ_SOURCE_CHANGED"
    assert facade.writes[0].expected_revision == 4
    assert facade.writes[0].payload["status"] == "FAILED"


@pytest.mark.asyncio
async def test_terminal_methods_reject_unavailable_host_before_any_write() -> None:
    operation_id = str(uuid4())

    class UnavailableFacade(_TerminalFacade):
        async def current_execution(self):
            self.calls += 1
            raise PluginHostError("denied")

    facade = UnavailableFacade(object(), _operation_record(operation_id, "GENERATING"))
    service = _service.KnowledgeService(facade)
    with pytest.raises(PluginHostError):
        await service.deny_current("ACCESS_DENIED")
    with pytest.raises(PluginHostError):
        await service.fail_current(operation_id, "READ_SOURCE_CHANGED")
    assert facade.calls == 2
    assert facade.writes == []
    assert facade.record.payload["status"] == "GENERATING"
    assert facade.record.revision == 4


@pytest.mark.asyncio
async def test_deny_current_transitions_only_its_validating_operation() -> None:
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation_id = str(uuid4())
    execution = SimpleNamespace(
        actor=actor,
        context={"operation_id": operation_id},
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )
    facade = _TerminalFacade(execution, _operation_record(operation_id, "VALIDATING"))
    result = await _service.KnowledgeService(facade).deny_current("CONFIG_INVALID")
    assert result.operation.status == "DENIED"
    assert result.operation.error_code == "CONFIG_INVALID"
    assert facade.writes[0].expected_revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["RETRIEVING", "GENERATING", "EMPTY", "DENIED", "FAILED"])
async def test_deny_current_never_rewrites_an_operation_outside_validation(status: str) -> None:
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation_id = str(uuid4())
    execution = SimpleNamespace(
        actor=actor,
        context={"operation_id": operation_id},
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )
    facade = _TerminalFacade(execution, _operation_record(operation_id, status))
    result = await _service.KnowledgeService(facade).deny_current("ACCESS_DENIED")
    assert result.operation.status == status
    assert facade.writes == []


@pytest.mark.asyncio
async def test_terminal_cas_conflict_preserves_the_winning_operation() -> None:
    actor = PluginActor(4, False, agent_id="agent-a", thread_id="thread-a")
    operation_id = str(uuid4())
    execution = SimpleNamespace(
        actor=actor,
        context={"operation_id": operation_id},
        execution_kind="turn",
        required_plugins={"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"},
    )

    class ConflictFacade(_TerminalFacade):
        async def commit(self, _writes, **_kwargs):
            raise PluginHostError("conflict")

    facade = ConflictFacade(execution, _operation_record(operation_id, "GENERATING"))
    with pytest.raises(PluginHostError) as conflict:
        await _service.KnowledgeService(facade).fail_current(operation_id, "UPSTREAM_UNAVAILABLE")
    assert conflict.value.code == "conflict"
    assert facade.record.payload["status"] == "GENERATING"
    assert facade.writes == []
