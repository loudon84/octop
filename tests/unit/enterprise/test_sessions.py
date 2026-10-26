from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from pathlib import Path

from octop.infra.agents.plugins.host_contracts import HostRecord, PluginActor

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_sessions = import_module("enterprise_knowledge.sessions")
SessionService = _sessions.SessionService
session_key = _sessions._session_key
PROBES = import_module("enterprise_knowledge.probes").PROBES


class _RecordsFacade:
    def __init__(self, records: dict[tuple[str, str], HostRecord]) -> None:
        self.records = records

    async def get_record(self, namespace: str, key: str) -> HostRecord | None:
        return self.records.get((namespace, key))


def test_session_identity_uses_unambiguous_structured_encoding() -> None:
    assert session_key(7, "a:b", "c") != session_key(7, "a", "b:c")
    assert session_key(7, "agent", "thread") != session_key(8, "agent", "thread")


def test_missing_grant_keeps_new_session_selection_empty() -> None:
    agent_id = "agent-1"
    connection_id = "00000000-0000-4000-8000-000000000001"
    binding = HostRecord(
        id=agent_id,
        namespace="bindings",
        logical_key=agent_id,
        owner_user_id=1,
        revision=1,
        payload={
            "enabled": True,
            "connection_ref": connection_id,
            "expert_allowed_kb_ids": ["kb-default"],
            "default_kb_ids": ["kb-default"],
        },
    )
    connection = HostRecord(
        id=connection_id,
        namespace="connections",
        logical_key=connection_id,
        owner_user_id=1,
        revision=1,
        payload={"enabled": True, "allowed_kb_ids": ["kb-default"]},
    )
    facade = _RecordsFacade(
        {("bindings", agent_id): binding, ("connections", connection_id): connection}
    )
    actor = PluginActor(user_id=9, is_admin=False, agent_id=agent_id, thread_id="thread-1")

    snapshot = asyncio.run(SessionService(facade).get(actor))

    assert snapshot.selection_present is False
    assert snapshot.selected_kb_ids == []
    assert snapshot.grant_revision == 0


def test_session_capabilities_separate_saving_from_ready_and_selectable() -> None:
    agent_id = "agent-1"
    connection_id = "00000000-0000-4000-8000-000000000001"
    actor = PluginActor(user_id=9, is_admin=False, agent_id=agent_id, thread_id="thread-1")

    no_source = asyncio.run(SessionService(_RecordsFacade({})).get(actor))

    assert no_source.capabilities.model_dump() == {
        "can_save_selection": True,
        "can_select_knowledge": False,
        "knowledge_ready": False,
        "web_ready": False,
    }

    records = {
        ("bindings", agent_id): HostRecord(
            id=agent_id,
            namespace="bindings",
            logical_key=agent_id,
            owner_user_id=1,
            revision=1,
            payload={
                "enabled": True,
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a"],
                "default_kb_ids": ["kb-a"],
            },
        ),
        ("connections", connection_id): HostRecord(
            id=connection_id,
            namespace="connections",
            logical_key=connection_id,
            owner_user_id=1,
            revision=1,
            payload={"enabled": True, "allowed_kb_ids": ["kb-a"]},
        ),
        ("grants", f"{connection_id}:9"): HostRecord(
            id="grant-1",
            namespace="grants",
            logical_key=f"{connection_id}:9",
            owner_user_id=1,
            revision=1,
            payload={"user_allowed_kb_ids": ["kb-a"]},
        ),
    }
    available = asyncio.run(SessionService(_RecordsFacade(records)).get(actor))

    assert available.capabilities.model_dump() == {
        "can_save_selection": True,
        "can_select_knowledge": True,
        "knowledge_ready": False,
        "web_ready": False,
    }

    records[("connection_intents", connection_id)] = HostRecord(
        id=connection_id,
        namespace="connection_intents",
        logical_key=connection_id,
        owner_user_id=1,
        revision=1,
        payload={"state": "BLOCKED"},
    )
    isolated = asyncio.run(SessionService(_RecordsFacade(records)).get(actor))
    assert isolated.capabilities.model_dump() == {
        "can_save_selection": False,
        "can_select_knowledge": False,
        "knowledge_ready": False,
        "web_ready": False,
    }


def test_session_readiness_uses_three_valid_current_proofs_without_network() -> None:
    agent_id = "agent-1"
    connection_id = "00000000-0000-4000-8000-000000000001"
    actor = PluginActor(user_id=9, is_admin=False, agent_id=agent_id, thread_id="thread-1")
    records = {
        ("bindings", agent_id): HostRecord(
            id=agent_id,
            namespace="bindings",
            logical_key=agent_id,
            owner_user_id=1,
            revision=1,
            payload={
                "enabled": True,
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a"],
                "default_kb_ids": ["kb-a"],
            },
        ),
        ("connections", connection_id): HostRecord(
            id=connection_id,
            namespace="connections",
            logical_key=connection_id,
            owner_user_id=1,
            revision=4,
            payload={"enabled": True, "allowed_kb_ids": ["kb-a"]},
        ),
        ("grants", f"{connection_id}:9"): HostRecord(
            id="grant-1",
            namespace="grants",
            logical_key=f"{connection_id}:9",
            owner_user_id=1,
            revision=1,
            payload={"user_allowed_kb_ids": ["kb-a"]},
        ),
    }
    facade = _RecordsFacade(records)
    before = asyncio.run(SessionService(facade).get(actor))
    assert before.capabilities.knowledge_ready is False

    from datetime import UTC, datetime

    for component in ("LIST", "SEARCH", "READ"):
        key = f"{connection_id}:{component}"
        records[(PROBES, key)] = HostRecord(
            id=key,
            namespace=PROBES,
            logical_key=key,
            owner_user_id=1,
            revision=1,
            payload={
                "schema_id": "octop.enterprise.knowledge.v1",
                "version": 1,
                "connection_id": connection_id,
                "component": component,
                "observed_revision": 4,
                "checked_at": datetime.now(UTC).isoformat(),
                "status": "SUCCESS",
                "error_code": None,
            },
        )

    ready = asyncio.run(SessionService(facade).get(actor))
    assert ready.capabilities.knowledge_ready is True
    assert ready.capabilities.web_ready is False

    old = records[(PROBES, f"{connection_id}:READ")]
    records[(PROBES, old.logical_key)] = HostRecord(
        id=old.id,
        namespace=old.namespace,
        logical_key=old.logical_key,
        owner_user_id=old.owner_user_id,
        revision=old.revision + 1,
        payload={**old.payload, "observed_revision": 3},
    )
    stale = asyncio.run(SessionService(facade).get(actor))
    assert stale.capabilities.knowledge_ready is False
