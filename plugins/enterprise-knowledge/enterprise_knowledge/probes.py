"""Authorized knowledge directory and revision-bound capability probes."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import httpx

from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import BINDINGS, CONNECTION_INTENTS, CONNECTIONS
from .i18n import error_message
from .models import (
    SCHEMA_ID,
    ConnectionCatalogInput,
    ConnectionProbeInput,
    ProbeOutput,
)
from .policy import ScopePolicy
from .weknora import OperationBudget, WeKnoraError, WeKnoraProvider, _WeKnoraReadClient

PROBES = "connection_probes"
Component = Literal["LIST", "SEARCH", "READ"]
ProbeStatus = Literal["SUCCESS", "BLOCKED", "EMPTY", "FAILED"]
_READY = {"LIST", "SEARCH", "READ"}


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _admin(actor: PluginActor) -> None:
    if not actor.is_admin:
        raise _error(actor, "ACCESS_DENIED", 403)


def _exception_code(exc: WeKnoraError) -> str:
    return exc.code


class _BudgetFacade:
    """Apply the same operation deadline to all asynchronous Host facade calls."""

    def __init__(self, facade: Any, budget: OperationBudget, actor: PluginActor) -> None:
        self._facade, self._budget, self._actor = facade, budget, actor

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._facade, name)
        if not inspect.iscoroutinefunction(method):
            return method

        async def bounded(*args: Any, **kwargs: Any) -> Any:
            try:
                remaining = self._budget.remaining_lifetime_seconds()
                return await asyncio.wait_for(method(*args, **kwargs), timeout=remaining)
            except TimeoutError:
                self._budget.cancel()
                raise _error(self._actor, "UPSTREAM_BUDGET_EXCEEDED", 503) from None

        return bounded


def _status_payload(
    connection_id: str,
    component: Component,
    revision: int,
    status: ProbeStatus,
    error_code: str | None,
) -> dict[str, Any]:
    return {
        "schema_id": SCHEMA_ID,
        "version": 1,
        "connection_id": connection_id,
        "component": component,
        "observed_revision": revision,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": status,
        "error_code": error_code,
    }


def valid_probe_record(payload: Any, connection_id: str, component: str, revision: int) -> bool:
    if not isinstance(payload, dict):
        return False
    if (
        payload.get("schema_id") != SCHEMA_ID
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("connection_id") != connection_id
        or payload.get("component") != component
        or type(payload.get("observed_revision")) is not int
        or payload.get("observed_revision") != revision
        or payload.get("status") not in {"SUCCESS", "BLOCKED", "EMPTY", "FAILED"}
    ):
        return False
    error_code = payload.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        return False
    if payload["status"] in {"SUCCESS", "EMPTY"} and error_code is not None:
        return False
    if payload["status"] in {"BLOCKED", "FAILED"} and not error_code:
        return False
    try:
        checked_at = datetime.fromisoformat(payload["checked_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return checked_at.tzinfo is not None and checked_at.utcoffset() == timedelta(0)


class ProbeService:
    """Host-authorized service; each operation owns one shared bounded read budget."""

    def __init__(self, facade: Any, *, client_factory: Any = None) -> None:
        self._facade = facade
        self._client_factory = client_factory

    async def catalog(self, actor: PluginActor, request: ConnectionCatalogInput) -> dict[str, Any]:
        budget = OperationBudget()
        facade = _BudgetFacade(self._facade, budget, actor)
        read: _WeKnoraReadClient | None = None
        try:
            connection = await self._host(
                budget, actor, self._facade.get_record(CONNECTIONS, request.connection_id)
            )
            self._revision(actor, connection, request.expected_revision)
            intent = await self._host(
                budget, actor, self._facade.get_record(CONNECTION_INTENTS, request.connection_id)
            )
            if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
                raise _error(actor, "RECOVERY_BLOCKED", 409)
            intent_revision = intent.revision if intent else 0
            read = None
            rows: list[Mapping[str, Any]]
            if actor.is_admin:
                read = _WeKnoraReadClient(
                    facade,
                    actor,
                    request.connection_id,
                    request.expected_revision,
                    client=await self._host(budget, actor, self._client()),
                    ca_bundle=os.environ.get("OCTOP_WEKNORA_CA_BUNDLE"),
                    budget=budget,
                )
                rows = await read.metadata_for(
                    request.connection_id,
                    await read.visible_kb_ids(request.connection_id, actor),
                    actor,
                )
            else:
                self._employee_connection(actor, connection)
                binding = await self._host(
                    budget, actor, facade.get_record(BINDINGS, actor.agent_id or "")
                )
                if binding is None or not binding.payload.get("enabled"):
                    raise _error(actor, "CONNECTION_DISABLED", 409)
                if binding.payload.get("connection_ref") != request.connection_id:
                    raise _error(actor, "CONFIG_CONFLICT", 409)
                read = WeKnoraProvider(
                    facade,
                    actor,
                    request.connection_id,
                    request.expected_revision,
                    client=await self._host(budget, actor, self._client()),
                    ca_bundle=os.environ.get("OCTOP_WEKNORA_CA_BUNDLE"),
                    budget=budget,
                )
                rows = [dict(row) for row in await ScopePolicy(facade, read).catalog(actor)]
            await self._current(
                actor, request.connection_id, request.expected_revision, intent_revision, budget
            )
            return {
                "schema_id": SCHEMA_ID,
                "version": 1,
                "connection_id": request.connection_id,
                "observed_revision": request.expected_revision,
                "knowledge_bases": rows,
            }
        finally:
            if read is not None:
                await read.aclose()
            budget.cancel()

    async def probe(self, actor: PluginActor, request: ConnectionProbeInput) -> ProbeOutput:
        _admin(actor)
        budget = OperationBudget()
        facade = _BudgetFacade(self._facade, budget, actor)
        read = None
        intent_revision = 0
        component_key = f"{request.connection_id}:{request.component}"
        old_probe = None
        status: ProbeStatus = "SUCCESS"
        error_code: str | None = None
        try:
            connection = await self._host(
                budget, actor, self._facade.get_record(CONNECTIONS, request.connection_id)
            )
            self._revision(actor, connection, request.expected_revision)
            intent = await self._host(
                budget, actor, self._facade.get_record(CONNECTION_INTENTS, request.connection_id)
            )
            if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
                raise _error(actor, "RECOVERY_BLOCKED", 409)
            intent_revision = intent.revision if intent else 0
            old_probe = await self._host(
                budget, actor, self._facade.get_record(PROBES, component_key)
            )
            allowed_fields = {
                "LIST": {"connection_id", "expected_revision", "component"},
                "SEARCH": {
                    "connection_id",
                    "expected_revision",
                    "component",
                    "query",
                    "knowledge_base_ids",
                },
                "READ": {
                    "connection_id",
                    "expected_revision",
                    "component",
                    "knowledge_base_ids",
                    "knowledge_id",
                },
            }[request.component]
            supplied = request.model_fields_set
            if supplied - allowed_fields:
                status, error_code = "BLOCKED", "PROBE_INPUT_INVALID"
            elif (
                request.component == "SEARCH"
                and (
                    not request.query or not request.query.strip() or not request.knowledge_base_ids
                )
                or request.component == "READ"
                and (not request.knowledge_id or not request.knowledge_base_ids)
            ):
                status, error_code = "BLOCKED", "PROBE_INPUT_REQUIRED"

            if status == "SUCCESS" and request.component in {"SEARCH", "READ"}:
                allowed = connection.payload.get("allowed_kb_ids")
                chosen = request.knowledge_base_ids or []
                if (
                    not isinstance(allowed, list)
                    or not allowed
                    or any(not _valid_scope_id(kb_id) for kb_id in allowed)
                    or not chosen
                    or not set(chosen).issubset(allowed)
                ):
                    status, error_code = "BLOCKED", "SCOPE_INVALID"

            if status == "SUCCESS":
                read = _WeKnoraReadClient(
                    facade,
                    actor,
                    request.connection_id,
                    request.expected_revision,
                    client=await self._host(budget, actor, self._client()),
                    ca_bundle=os.environ.get("OCTOP_WEKNORA_CA_BUNDLE"),
                    budget=budget,
                )
                visible = await read.visible_kb_ids(request.connection_id, actor)
                if request.component == "LIST":
                    pass
                else:
                    chosen = request.knowledge_base_ids or []
                    if not set(chosen).issubset(visible):
                        status, error_code = "BLOCKED", "SCOPE_INVALID"
                    elif request.component == "SEARCH":
                        hits = await read.search(request.query or "", chosen)
                        if not hits:
                            status = "EMPTY"
                    else:
                        detail = await read.knowledge_detail(request.knowledge_id or "", chosen)
                        chunks = await read.chunks(detail.knowledge_id, chosen)
                        if not any(chunk.content.strip() for chunk in chunks.items):
                            status = "EMPTY"
            await self._current(
                actor, request.connection_id, request.expected_revision, intent_revision, budget
            )
            await self._persist(
                actor, request, status, error_code, intent_revision, old_probe, budget
            )
            return ProbeOutput(
                connection_id=request.connection_id,
                observed_revision=request.expected_revision,
                component=request.component,
                status=status,
                error_code=error_code,
            )
        except WeKnoraError as exc:
            status, error_code = "FAILED", _exception_code(exc)
            # The operation has already made/attempted a provider request. Persist only if still current.
            await self._persist(
                actor, request, status, error_code, intent_revision, old_probe, budget
            )
            return ProbeOutput(
                connection_id=request.connection_id,
                observed_revision=request.expected_revision,
                component=request.component,
                status=status,
                error_code=error_code,
            )
        finally:
            if read is not None:
                await read.aclose()
            budget.cancel()

    async def _client(self) -> httpx.AsyncClient | None:
        if self._client_factory is None:
            return None
        return cast(httpx.AsyncClient, await self._client_factory())

    async def _host(self, budget: OperationBudget, actor: PluginActor, awaitable: Any) -> Any:
        try:
            remaining = budget.remaining_lifetime_seconds()
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError:
            budget.cancel()
            raise _error(actor, "UPSTREAM_BUDGET_EXCEEDED", 503) from None
        except PluginHostError as exc:
            code = "CONFIG_CONFLICT" if exc.code == "conflict" else "PLUGIN_NOT_READY"
            raise _error(actor, code, 409) from None

    async def _current(
        self,
        actor: PluginActor,
        connection_id: str,
        revision: int,
        intent_revision: int,
        budget: OperationBudget,
    ) -> None:
        connection = await self._host(
            budget, actor, self._facade.get_record(CONNECTIONS, connection_id)
        )
        self._revision(actor, connection, revision)
        intent = await self._host(
            budget, actor, self._facade.get_record(CONNECTION_INTENTS, connection_id)
        )
        if (intent.revision if intent else 0) != intent_revision:
            raise _error(actor, "CONFIG_CONFLICT", 409)
        if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
            raise _error(actor, "RECOVERY_BLOCKED", 409)

    async def _persist(
        self,
        actor: PluginActor,
        request: ConnectionProbeInput,
        status: ProbeStatus,
        error_code: str | None,
        intent_revision: int,
        old: Any,
        budget: OperationBudget,
    ) -> None:
        key = f"{request.connection_id}:{request.component}"
        connection = await self._host(
            budget, actor, self._facade.get_record(CONNECTIONS, request.connection_id)
        )
        intent = await self._host(
            budget, actor, self._facade.get_record(CONNECTION_INTENTS, request.connection_id)
        )
        if connection is None or connection.revision != request.expected_revision:
            raise _error(actor, "CONFIG_CONFLICT", 409)
        current_intent_revision = intent.revision if intent is not None else 0
        if current_intent_revision != intent_revision or (
            intent and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
        ):
            raise _error(
                actor,
                "RECOVERY_BLOCKED"
                if intent and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
                else "CONFIG_CONFLICT",
                409,
            )
        payload = _status_payload(
            request.connection_id, request.component, request.expected_revision, status, error_code
        )
        try:
            await self._host(
                budget,
                actor,
                self._facade.commit(
                    (
                        RecordWrite(
                            PROBES,
                            key,
                            connection.owner_user_id,
                            payload,
                            old.revision if old else 0,
                        ),
                    ),
                    checks=(
                        RecordCheck(CONNECTIONS, request.connection_id, request.expected_revision),
                        RecordCheck(CONNECTION_INTENTS, request.connection_id, intent_revision),
                    ),
                ),
            )
        except PluginHostError as exc:
            code = "CONFIG_CONFLICT" if exc.code == "conflict" else "PLUGIN_NOT_READY"
            raise _error(actor, code, 409) from None

    @staticmethod
    def _revision(actor: PluginActor, record: Any, expected: int) -> None:
        if record is None:
            raise _error(actor, "NOT_FOUND", 404)
        if record.revision != expected:
            raise _error(actor, "CONFIG_CONFLICT", 409)

    @staticmethod
    def _employee_connection(actor: PluginActor, connection: Any) -> None:
        if not actor.agent_id or not actor.thread_id:
            raise _error(actor, "AGENT_REQUIRED", 400)
        if not connection.payload.get("enabled"):
            raise _error(actor, "CONNECTION_DISABLED", 409)


def _valid_scope_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "/" not in value
        and "\\" not in value
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


async def connection_projection(
    facade: Any, record: Any, intent: Any = None
) -> tuple[str, dict[str, dict[str, str | int | None]]]:
    rows: dict[str, Any] = {}
    for component in ("LIST", "SEARCH", "READ"):
        probe = await facade.get_record(PROBES, f"{record.logical_key}:{component}")
        if probe is not None and valid_probe_record(
            probe.payload, record.logical_key, component, record.revision
        ):
            rows[component] = probe.payload
    summary = {
        name: {
            "status": row["status"],
            "error_code": row.get("error_code"),
            "observed_revision": row["observed_revision"],
            "checked_at": row["checked_at"],
        }
        for name, row in rows.items()
    }
    if not record.payload.get("enabled"):
        return "DISABLED", summary
    if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
        return "DEGRADED", summary
    if any(
        row.get("status") == "FAILED" and row.get("error_code") == "UPSTREAM_AUTH_FAILED"
        for row in rows.values()
    ):
        return "AUTH_FAILED", summary
    if any(
        row.get("status") == "FAILED"
        and row.get("error_code") in {"UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE"}
        for row in rows.values()
    ):
        return "UNREACHABLE", summary
    if _READY.issubset(rows) and all(rows[name].get("status") == "SUCCESS" for name in _READY):
        return "READY", summary
    if rows:
        return "DEGRADED", summary
    return "CONFIGURED", summary
