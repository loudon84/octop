from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_probes = import_module("enterprise_knowledge.probes")
PROBES = _probes.PROBES
connection_projection = _probes.connection_projection
ProbeService = _probes.ProbeService
ConnectionProbeInput = import_module("enterprise_knowledge.models").ConnectionProbeInput
ConnectionCatalogInput = import_module("enterprise_knowledge.models").ConnectionCatalogInput
CONNECTIONS = import_module("enterprise_knowledge.connections").CONNECTIONS
CONNECTION_INTENTS = import_module("enterprise_knowledge.connections").CONNECTION_INTENTS
BINDINGS = import_module("enterprise_knowledge.connections").BINDINGS
ConnectionService = import_module("enterprise_knowledge.connections").ConnectionService
GRANTS = import_module("enterprise_knowledge.connections").GRANTS

_CONNECTION_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def no_external_tls_configuration(monkeypatch):
    monkeypatch.delenv("OCTOP_WEKNORA_CA_BUNDLE", raising=False)


class _Facade:
    def __init__(self, records: dict[tuple[str, str], HostRecord]) -> None:
        self.records = records

    async def get_record(self, namespace: str, key: str) -> HostRecord | None:
        return self.records.get((namespace, key))


def _record(namespace: str, key: str, revision: int, payload: dict[str, Any]) -> HostRecord:
    return HostRecord(
        id=key,
        namespace=namespace,
        logical_key=key,
        owner_user_id=1,
        revision=revision,
        payload=payload,
    )


def _probe(component: str, revision: int, status: str) -> HostRecord:
    key = f"{_CONNECTION_ID}:{component}"
    return _record(
        PROBES,
        key,
        1,
        {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "connection_id": _CONNECTION_ID,
            "component": component,
            "observed_revision": revision,
            "checked_at": datetime.now(UTC).isoformat(),
            "status": status,
            "error_code": None,
        },
    )


def test_readiness_requires_all_three_successes_on_current_revision() -> None:
    connection = _record(
        "connections", _CONNECTION_ID, 4, {"enabled": True, "allowed_kb_ids": ["kb-a"]}
    )
    records = {
        (PROBES, f"{_CONNECTION_ID}:LIST"): _probe("LIST", 4, "SUCCESS"),
        (PROBES, f"{_CONNECTION_ID}:SEARCH"): _probe("SEARCH", 4, "EMPTY"),
        (PROBES, f"{_CONNECTION_ID}:READ"): _probe("READ", 3, "SUCCESS"),
    }

    async def run() -> tuple[str, dict[str, dict[str, str | None]]]:
        return await connection_projection(_Facade(records), connection)

    status, components = asyncio.run(run())
    assert status == "DEGRADED"
    assert set(components) == {"LIST", "SEARCH"}

    records[(PROBES, f"{_CONNECTION_ID}:SEARCH")] = _probe("SEARCH", 4, "SUCCESS")
    records[(PROBES, f"{_CONNECTION_ID}:READ")] = _probe("READ", 4, "SUCCESS")
    status, components = asyncio.run(run())
    assert status == "READY"
    assert set(components) == {"LIST", "SEARCH", "READ"}

    disabled = _record(
        "connections", _CONNECTION_ID, 4, {"enabled": False, "allowed_kb_ids": ["kb-a"]}
    )
    status, _ = asyncio.run(connection_projection(_Facade(records), disabled))
    assert status == "DISABLED"


class _SettingsFacade(_Facade):
    def __init__(self, records: dict[tuple[str, str], HostRecord]) -> None:
        super().__init__(records)

    async def list_records(self, namespace: str) -> list[HostRecord]:
        return [
            record
            for (record_namespace, _), record in self.records.items()
            if record_namespace == namespace
        ]

    async def get_requirement(self, _actor: PluginActor, _agent_id: str) -> None:
        return None


def test_settings_exposes_only_sanitized_current_revision_probe_metadata() -> None:
    async def run() -> None:
        checked_at = "2026-10-25T00:10:11+00:00"
        connection = _record(
            CONNECTIONS,
            _CONNECTION_ID,
            4,
            {
                "schema_id": "octop.enterprise.knowledge.v1",
                "version": 1,
                "connection_id": _CONNECTION_ID,
                "name": "Primary",
                "base_url": "https://knowledge.example/api/v1",
                "tenant_id": None,
                "allowed_kb_ids": ["kb-a"],
                "enabled": True,
                "probe_status": "CONFIGURED",
                "credential_ref": "synthetic-secret-reference",
                "credential_owner_user_id": 1,
            },
        )
        current = _probe("LIST", 4, "SUCCESS")
        current.payload["checked_at"] = checked_at
        current.payload.update(query="private query", api_key="provider secret")
        stale = _probe("SEARCH", 3, "SUCCESS")
        malformed = _probe("READ", 4, "SUCCESS")
        malformed.payload["checked_at"] = "not-a-timestamp"
        facade = _SettingsFacade(
            {
                (CONNECTIONS, _CONNECTION_ID): connection,
                (PROBES, f"{_CONNECTION_ID}:LIST"): current,
                (PROBES, f"{_CONNECTION_ID}:SEARCH"): stale,
                (PROBES, f"{_CONNECTION_ID}:READ"): malformed,
            }
        )

        admin_settings = await ConnectionService(facade).get_settings(_admin_actor())
        components = admin_settings.connections[0].probe_components
        assert set(components) == {"LIST"}
        assert components["LIST"].status == "SUCCESS"
        assert components["LIST"].observed_revision == 4
        assert components["LIST"].checked_at == checked_at
        serialized = admin_settings.model_dump_json()
        assert "private query" not in serialized
        assert "provider secret" not in serialized
        assert "synthetic-secret-reference" not in serialized

        employee = PluginActor(
            user_id=2,
            is_admin=False,
            permissions=frozenset({"agent_plugins"}),
            agent_id="agent-a",
        )
        facade.records[(GRANTS, f"{_CONNECTION_ID}:2")] = _record(
            GRANTS,
            f"{_CONNECTION_ID}:2",
            1,
            {
                "schema_id": "octop.enterprise.knowledge.v1",
                "version": 1,
                "connection_id": _CONNECTION_ID,
                "user_id": 2,
                "user_allowed_kb_ids": ["kb-a"],
            },
        )
        employee_settings = await ConnectionService(facade).get_settings(employee)
        assert employee_settings.connections[0].probe_components == {}

    asyncio.run(run())


class _ServiceFacade:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], HostRecord] = {
            (CONNECTIONS, _CONNECTION_ID): _record(
                CONNECTIONS,
                _CONNECTION_ID,
                4,
                {
                    "enabled": True,
                    "allowed_kb_ids": ["kb-a"],
                    "base_url": "https://knowledge.example/api/v1",
                    "credential_ref": "synthetic-ref",
                    "credential_owner_user_id": 1,
                },
            ),
            (BINDINGS, "agent-a"): _record(
                BINDINGS,
                "agent-a",
                1,
                {"enabled": True, "connection_ref": _CONNECTION_ID},
            ),
        }
        self.writes: list[tuple[str, str]] = []
        self.checks: list[tuple[RecordCheck, ...]] = []
        self.commit_gate: tuple[asyncio.Event, asyncio.Event] | None = None
        self.pause_initial_read: tuple[asyncio.Event, asyncio.Event] | None = None

    async def get_record(self, namespace: str, key: str) -> HostRecord | None:
        if self.pause_initial_read and (namespace, key) == (CONNECTIONS, _CONNECTION_ID):
            entered, resume = self.pause_initial_read
            entered.set()
            await resume.wait()
        return self.records.get((namespace, key))

    async def resolve_credential(self, credential_ref: str, owner_user_id: int) -> dict[str, str]:
        assert credential_ref == "synthetic-ref" and owner_user_id == 1
        return {"api_key": "synthetic-key"}

    async def commit(self, writes, activations=(), *, requirements=(), checks=()):
        check_tuple = tuple(checks)
        self.checks.append(check_tuple)
        if self.commit_gate is not None:
            entered, resume = self.commit_gate
            entered.set()
            await resume.wait()
        for check in check_tuple:
            current = self.records.get((check.namespace, check.logical_key))
            if (current.revision if current else 0) != check.expected_revision:
                raise PluginHostError("conflict")
        for write in writes:
            current = self.records.get((write.namespace, write.logical_key))
            if (current.revision if current else 0) != write.expected_revision:
                raise PluginHostError("conflict")
        for write in writes:
            old = self.records.get((write.namespace, write.logical_key))
            revision = old.revision + 1 if old else 1
            self.records[(write.namespace, write.logical_key)] = _record(
                write.namespace, write.logical_key, revision, write.payload
            )
            self.writes.append((write.namespace, write.logical_key))


def _admin_actor() -> PluginActor:
    return PluginActor(user_id=1, is_admin=True)


def _probe_request(component: str = "LIST", **fields: Any) -> Any:
    return ConnectionProbeInput(
        connection_id=_CONNECTION_ID,
        expected_revision=4,
        component=component,
        **fields,
    )


class _GateTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


def _client_factory(handler, clients: list[httpx.AsyncClient] | None = None):
    async def create() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=_GateTransport(handler))
        if clients is not None:
            clients.append(client)
        return client

    return create


@pytest.mark.parametrize("outcome", ["success", "exception", "cancel"])
def test_employee_catalog_closes_provider_for_success_failure_and_cancel(
    monkeypatch, outcome
) -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        actor = PluginActor(
            user_id=1,
            is_admin=False,
            permissions=frozenset({"agent_plugins"}),
            agent_id="agent-a",
            thread_id="thread-a",
        )
        clients: list[httpx.AsyncClient] = []
        entered = asyncio.Event()
        resume = asyncio.Event()

        async def respond(_request: httpx.Request) -> httpx.Response:
            entered.set()
            if outcome == "cancel":
                await resume.wait()
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb-a"}]})

        async def catalog(policy, _actor):
            provider = policy._provider
            rows = await provider.metadata_for(_CONNECTION_ID, ["kb-a"], actor)
            if outcome == "exception":
                raise RuntimeError("synthetic policy failure")
            return [dict(row) for row in rows]

        monkeypatch.setattr(_probes.ScopePolicy, "catalog", catalog)
        service = ProbeService(facade, client_factory=_client_factory(respond, clients))
        request = ConnectionCatalogInput(connection_id=_CONNECTION_ID, expected_revision=4)
        task = asyncio.create_task(service.catalog(actor, request))
        if outcome == "cancel":
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "exception":
            with pytest.raises(RuntimeError, match="synthetic policy failure"):
                await task
        else:
            assert (await task)["knowledge_bases"] == [{"kb_id": "kb-a"}]
        assert len(clients) == 1
        assert clients[0].is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    ("allowed", "chosen"),
    [
        ([], ["kb-a"]),
        (["kb-a"], ["kb-a", "kb-outside"]),
        (["bad/id"], ["kb-a"]),
    ],
)
def test_probe_rejects_invalid_enterprise_scope_before_http(allowed, chosen) -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        record = facade.records[(CONNECTIONS, _CONNECTION_ID)]
        record.payload["allowed_kb_ids"] = allowed
        calls = 0

        async def respond(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb-a"}]})

        service = ProbeService(facade, client_factory=_client_factory(respond))
        result = await service.probe(
            _admin_actor(), _probe_request("SEARCH", query="q", knowledge_base_ids=chosen)
        )
        assert result.status == "BLOCKED"
        assert result.error_code == "SCOPE_INVALID"
        assert calls == 0
        assert facade.writes == [("connection_probes", f"{_CONNECTION_ID}:SEARCH")]

    asyncio.run(run())


def test_probe_captures_component_revision_before_outbound_and_conflicts_on_late_result() -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        first_entered, second_entered = asyncio.Event(), asyncio.Event()
        release_first, release_second = asyncio.Event(), asyncio.Event()
        ordinal = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal ordinal
            ordinal += 1
            if ordinal == 1:
                first_entered.set()
                await release_first.wait()
                return httpx.Response(200, json={"success": True, "data": [{"id": "kb-a"}]})
            second_entered.set()
            await release_second.wait()
            return httpx.Response(401, json={"success": False, "data": []})

        service = ProbeService(facade, client_factory=_client_factory(respond))
        older = asyncio.create_task(service.probe(_admin_actor(), _probe_request("LIST")))
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        newer = asyncio.create_task(service.probe(_admin_actor(), _probe_request("LIST")))
        await asyncio.wait_for(second_entered.wait(), timeout=2)
        release_second.set()
        newer_result = await newer
        assert newer_result.status == "FAILED"
        release_first.set()
        with pytest.raises(PluginActionError) as error:
            await older
        assert error.value.code == "CONFIG_CONFLICT"
        current = facade.records[("connection_probes", f"{_CONNECTION_ID}:LIST")]
        assert current.payload["status"] == "FAILED"
        assert current.revision == 1

    asyncio.run(run())


def test_probe_concurrent_different_components_keep_both_status_records() -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        first_requests_started = asyncio.Event()
        release = asyncio.Event()
        gated_requests = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal gated_requests
            gated_requests += 1
            if gated_requests <= 2:
                if gated_requests == 2:
                    first_requests_started.set()
                await release.wait()
            if request.url.path.endswith("/knowledge-bases"):
                return httpx.Response(200, json={"success": True, "data": [{"id": "kb-a"}]})
            if request.url.path.endswith("/knowledge-search"):
                return httpx.Response(200, json={"success": True, "data": []})
            return httpx.Response(404)

        service = ProbeService(facade, client_factory=_client_factory(respond))
        list_task = asyncio.create_task(service.probe(_admin_actor(), _probe_request("LIST")))
        search_task = asyncio.create_task(
            service.probe(
                _admin_actor(),
                _probe_request("SEARCH", query="q", knowledge_base_ids=["kb-a"]),
            )
        )
        await asyncio.wait_for(first_requests_started.wait(), timeout=2)
        release.set()
        list_result, search_result = await asyncio.gather(list_task, search_task)
        assert list_result.status == "SUCCESS"
        assert search_result.status == "EMPTY"
        assert facade.records[("connection_probes", f"{_CONNECTION_ID}:LIST")].revision == 1
        assert facade.records[("connection_probes", f"{_CONNECTION_ID}:SEARCH")].revision == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", ["connection", "intent"])
def test_probe_does_not_persist_or_succeed_after_source_or_intent_changes(change) -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def respond(_request: httpx.Request) -> httpx.Response:
            entered.set()
            await resume.wait()
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb-a"}]})

        service = ProbeService(facade, client_factory=_client_factory(respond))
        task = asyncio.create_task(service.probe(_admin_actor(), _probe_request()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if change == "connection":
            facade.records[(CONNECTIONS, _CONNECTION_ID)] = _record(
                CONNECTIONS,
                _CONNECTION_ID,
                5,
                facade.records[(CONNECTIONS, _CONNECTION_ID)].payload,
            )
        else:
            facade.records[(CONNECTION_INTENTS, _CONNECTION_ID)] = _record(
                CONNECTION_INTENTS,
                _CONNECTION_ID,
                1,
                {"state": "RESOLVED"},
            )
        resume.set()
        with pytest.raises(PluginActionError) as error:
            await task
        assert error.value.code == "CONFIG_CONFLICT"
        assert not facade.writes

    asyncio.run(run())


@pytest.mark.parametrize("change", ["connection", "intent"])
def test_probe_atomic_source_and_intent_checks_reject_change_at_commit(change) -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        entered, resume = asyncio.Event(), asyncio.Event()
        facade.commit_gate = (entered, resume)

        async def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb-a"}]})

        service = ProbeService(facade, client_factory=_client_factory(respond))
        task = asyncio.create_task(service.probe(_admin_actor(), _probe_request()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert facade.checks == [
            (
                RecordCheck(CONNECTIONS, _CONNECTION_ID, 4),
                RecordCheck(CONNECTION_INTENTS, _CONNECTION_ID, 0),
            )
        ]
        if change == "connection":
            facade.records[(CONNECTIONS, _CONNECTION_ID)] = _record(
                CONNECTIONS,
                _CONNECTION_ID,
                5,
                facade.records[(CONNECTIONS, _CONNECTION_ID)].payload,
            )
        else:
            facade.records[(CONNECTION_INTENTS, _CONNECTION_ID)] = _record(
                CONNECTION_INTENTS,
                _CONNECTION_ID,
                1,
                {"state": "RESOLVED"},
            )
        resume.set()
        with pytest.raises(PluginActionError) as error:
            await task
        assert error.value.code == "CONFIG_CONFLICT"
        assert not facade.writes

    asyncio.run(run())


def test_probe_preflight_host_wait_uses_operation_budget_and_never_reaches_http(
    monkeypatch,
) -> None:
    async def run() -> None:
        facade = _ServiceFacade()
        facade.pause_initial_read = (asyncio.Event(), asyncio.Event())
        clients_created = 0

        async def factory() -> httpx.AsyncClient:
            nonlocal clients_created
            clients_created += 1
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))

        original_budget = _probes.OperationBudget

        def short_budget():
            return original_budget(operation_seconds=0.02)

        monkeypatch.setattr(_probes, "OperationBudget", short_budget)
        service = ProbeService(facade, client_factory=factory)
        with pytest.raises(PluginActionError) as error:
            await service.probe(_admin_actor(), _probe_request())
        assert error.value.code == "UPSTREAM_BUDGET_EXCEEDED"
        assert clients_created == 0
        assert not facade.writes

    asyncio.run(run())
