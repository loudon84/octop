from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from pathlib import Path

import pytest

from octop.infra.agents.plugins.host_contracts import HostRecord, PluginActionError, PluginActor

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_policy = import_module("enterprise_knowledge.policy")
ScopePolicy = _policy.ScopePolicy
decide_scope = _policy.decide_scope


class _Provider:
    def __init__(self, visible: list[str], metadata: list[dict[str, str]]) -> None:
        self.visible = visible
        self.metadata = metadata
        self.visible_calls = 0
        self.metadata_calls: list[list[str]] = []
        self.search_calls = 0
        self.read_calls = 0

    async def visible_kb_ids(self, _connection_id: str, _actor: PluginActor) -> list[str]:
        self.visible_calls += 1
        return self.visible

    async def metadata_for(
        self, _connection_id: str, kb_ids: list[str], _actor: PluginActor
    ) -> list[dict[str, str]]:
        self.metadata_calls.append(kb_ids)
        return self.metadata


class _Facade:
    def __init__(self, records: dict[tuple[str, str], HostRecord]) -> None:
        self.records = records

    async def get_record(self, namespace: str, key: str) -> HostRecord | None:
        return self.records.get((namespace, key))


def _records(
    *,
    grant_ids: list[str] | None = None,
    selected: list[str] | None = None,
    mode: str = "KB_ONLY",
    intent: str | None = None,
) -> tuple[_Facade, PluginActor]:
    agent_id = "agent-1"
    thread_id = "thread-1"
    connection_id = "00000000-0000-4000-8000-000000000001"
    actor = PluginActor(user_id=9, is_admin=False, agent_id=agent_id, thread_id=thread_id)
    records: dict[tuple[str, str], HostRecord] = {
        ("bindings", agent_id): HostRecord(
            id=agent_id,
            namespace="bindings",
            logical_key=agent_id,
            owner_user_id=1,
            revision=2,
            payload={
                "enabled": True,
                "connection_ref": connection_id,
                "expert_allowed_kb_ids": ["kb-a", "kb-b", "kb-c"],
            },
        ),
        ("connections", connection_id): HostRecord(
            id=connection_id,
            namespace="connections",
            logical_key=connection_id,
            owner_user_id=1,
            revision=3,
            payload={"enabled": True, "allowed_kb_ids": ["kb-a", "kb-b", "kb-c"]},
        ),
        (
            "grants",
            f"{connection_id}:9",
        ): HostRecord(
            id="grant-9",
            namespace="grants",
            logical_key=f"{connection_id}:9",
            owner_user_id=1,
            revision=4,
            payload={
                "user_allowed_kb_ids": (
                    grant_ids if grant_ids is not None else ["kb-a", "kb-b", "kb-c"]
                )
            },
        ),
        ("sessions", '[9,"agent-1","thread-1"]'): HostRecord(
            id="session-9",
            namespace="sessions",
            logical_key='[9,"agent-1","thread-1"]',
            owner_user_id=9,
            revision=5,
            payload={
                "selected_kb_ids": selected if selected is not None else ["kb-a", "kb-b"],
                "mode": mode,
                "connection_id": connection_id,
                "config_revision": 3,
                "binding_revision": 2,
                "grant_revision": 4,
            },
        ),
    }
    if intent is not None:
        records[("connection_intents", connection_id)] = HostRecord(
            id=connection_id,
            namespace="connection_intents",
            logical_key=connection_id,
            owner_user_id=1,
            revision=1,
            payload={"state": intent},
        )
    return _Facade(records), actor


def test_five_layer_scope_is_a_sorted_intersection() -> None:
    decision = decide_scope(
        credential_visible_ids=["kb-z", "kb-b", "kb-a", "kb-a"],
        enterprise_allowed_ids=["kb-a", "kb-b", "kb-c"],
        employee_allowed_ids=["kb-b", "kb-a"],
        expert_allowed_ids=["kb-a", "kb-b"],
        session_selected_ids=["kb-a", "kb-b"],
    )

    assert decision == ["kb-a", "kb-b"]


def test_requested_ids_absent_uses_scope_empty_rejects_and_mixed_rejects_all() -> None:
    scope = ["kb-a", "kb-b"]
    actor = PluginActor(user_id=9, is_admin=False, agent_id="agent-1", thread_id="thread-1")
    assert _policy.apply_requested_ids(actor, scope, None) == ["kb-a", "kb-b"]
    with pytest.raises(PluginActionError):
        _policy.apply_requested_ids(actor, scope, [])
    with pytest.raises(PluginActionError):
        _policy.apply_requested_ids(actor, scope, ["kb-a", "kb-outside"])


def test_catalog_uses_first_four_layers_and_never_returns_unapproved_metadata() -> None:
    facade, actor = _records(selected=["kb-a"])
    provider = _Provider(
        ["kb-a", "kb-b", "kb-secret"],
        [
            {"kb_id": "kb-b", "title": "Authorized but not selected"},
            {"kb_id": "kb-secret", "title": "Secret"},
        ],
    )

    result = asyncio.run(ScopePolicy(facade, provider).catalog(actor))

    assert [item["kb_id"] for item in result] == ["kb-b"]
    assert provider.metadata_calls == [["kb-a", "kb-b"]]


def test_query_scope_preserves_omitted_ids_until_credential_visibility_intersection() -> None:
    facade, actor = _records()
    provider = _Provider(["kb-a"], [])
    service = ScopePolicy(facade, provider)

    omitted = asyncio.run(service.query_scope(actor))
    explicit_visible = asyncio.run(service.query_scope(actor, ["kb-a"]))
    with pytest.raises(PluginActionError):
        asyncio.run(service.query_scope(actor, ["kb-a", "kb-b"]))
    with pytest.raises(PluginActionError):
        asyncio.run(service.query_scope(actor, []))
    with pytest.raises(PluginActionError):
        asyncio.run(service.query_scope(actor, ["kb-a", "kb-outside"]))

    assert omitted.effective_kb_ids == ["kb-a"]
    assert explicit_visible.effective_kb_ids == ["kb-a"]
    assert provider.visible_calls == 3


@pytest.mark.parametrize(
    ("mode", "selected", "intent"),
    [
        ("WEB_ONLY", ["kb-a"], None),
        ("INVALID", ["kb-a"], None),
        ("KB_ONLY", [], None),
        ("KB_ONLY", ["kb-a"], "BLOCKED"),
    ],
)
def test_rejected_scope_does_not_call_provider(
    mode: str, selected: list[str], intent: str | None
) -> None:
    facade, actor = _records(mode=mode, selected=selected, intent=intent)
    provider = _Provider(["kb-a"], [{"kb_id": "kb-a", "title": "A"}])

    with pytest.raises(PluginActionError):
        asyncio.run(ScopePolicy(facade, provider).query_scope(actor))

    assert provider.visible_calls == 0
    assert provider.metadata_calls == []
    assert provider.search_calls == 0
    assert provider.read_calls == 0


def test_scope_refreshes_source_revisions_instead_of_caching() -> None:
    facade, actor = _records()
    provider = _Provider(["kb-a", "kb-b"], [])
    service = ScopePolicy(facade, provider)

    first = asyncio.run(service.query_scope(actor))
    connection = facade.records[("connections", "00000000-0000-4000-8000-000000000001")]
    facade.records[("connections", connection.logical_key)] = HostRecord(
        **{**connection.__dict__, "revision": connection.revision + 1}
    )
    second = asyncio.run(service.query_scope(actor))

    assert first.source_revisions.config_revision == 3
    assert second.source_revisions.config_revision == 4
    assert provider.visible_calls == 2


def test_employee_grants_are_isolated_for_a_shared_agent() -> None:
    facade, first_actor = _records()
    second_actor = PluginActor(
        user_id=10,
        is_admin=False,
        agent_id=first_actor.agent_id,
        thread_id=first_actor.thread_id,
    )
    connection_id = "00000000-0000-4000-8000-000000000001"
    facade.records[("grants", f"{connection_id}:10")] = HostRecord(
        id="grant-10",
        namespace="grants",
        logical_key=f"{connection_id}:10",
        owner_user_id=1,
        revision=1,
        payload={"user_allowed_kb_ids": ["kb-b"]},
    )
    facade.records[("sessions", '[10,"agent-1","thread-1"]')] = HostRecord(
        id="session-10",
        namespace="sessions",
        logical_key='[10,"agent-1","thread-1"]',
        owner_user_id=10,
        revision=1,
        payload={
            "selected_kb_ids": ["kb-b"],
            "mode": "KB_ONLY",
            "connection_id": connection_id,
            "config_revision": 3,
            "binding_revision": 2,
            "grant_revision": 1,
        },
    )
    provider = _Provider(["kb-a", "kb-b"], [])
    service = ScopePolicy(facade, provider)

    first = asyncio.run(service.query_scope(first_actor))
    second = asyncio.run(service.query_scope(second_actor))

    assert first.effective_kb_ids == ["kb-a", "kb-b"]
    assert second.effective_kb_ids == ["kb-b"]


def test_missing_grant_unknown_connection_and_disabled_connection_fail_closed() -> None:
    facade, actor = _records()
    connection_id = "00000000-0000-4000-8000-000000000001"
    provider = _Provider(["kb-a"], [])
    facade.records.pop(("grants", f"{connection_id}:9"))
    with pytest.raises(PluginActionError):
        asyncio.run(ScopePolicy(facade, provider).query_scope(actor))
    assert provider.visible_calls == 0

    facade, actor = _records(grant_ids=[])
    with pytest.raises(PluginActionError):
        asyncio.run(ScopePolicy(facade, provider).query_scope(actor))
    assert provider.visible_calls == 0

    facade, actor = _records()
    facade.records.pop(("connections", connection_id))
    with pytest.raises(PluginActionError) as missing:
        asyncio.run(ScopePolicy(facade, provider).query_scope(actor))
    assert missing.value.code == "NOT_FOUND"
    assert provider.visible_calls == 0

    facade, actor = _records()
    connection = facade.records[("connections", connection_id)]
    facade.records[("connections", connection_id)] = HostRecord(
        id=connection.id,
        namespace=connection.namespace,
        logical_key=connection.logical_key,
        owner_user_id=connection.owner_user_id,
        revision=connection.revision,
        payload={**connection.payload, "enabled": False},
    )
    with pytest.raises(PluginActionError) as disabled:
        asyncio.run(ScopePolicy(facade, provider).query_scope(actor))
    assert disabled.value.code == "CONNECTION_DISABLED"
    assert provider.visible_calls == 0


def test_no_provider_is_explicitly_unavailable() -> None:
    facade, actor = _records()

    with pytest.raises(PluginActionError) as caught:
        asyncio.run(ScopePolicy(facade).query_scope(actor))

    assert caught.value.code == "PLUGIN_NOT_READY"
    assert caught.value.status == 503
