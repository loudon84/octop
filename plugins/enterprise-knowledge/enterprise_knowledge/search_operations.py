"""Durable CAS claims and request accounting for controlled web search."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import uuid4

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import BINDINGS
from .i18n import error_message
from .models import QueryApproval, SearchExecutionRecord
from .operations import APPROVALS, OPERATIONS, TERMINAL, forget_public_query
from .search_settings import CONFIG_KEY, SEARCH_INTENTS, SEARCH_SOURCES
from .sessions import SESSIONS, _session_key

SEARCH_EXECUTIONS = "search_executions"
type SearchRole = Literal["PRIMARY", "SECONDARY"]
type SearchTerminal = Literal["COMPLETED", "EMPTY", "FAILED", "CANCELLED", "INTERRUPTED", "UNKNOWN"]


@dataclass(frozen=True)
class SearchReservation:
    execution: SearchExecutionRecord
    acquired: bool


def query_digest(public_query: str) -> str:
    return hashlib.sha256(public_query.encode("utf-8")).hexdigest()


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


class SearchOperationService:
    """Owns persistent search claims; it never performs provider I/O."""

    def __init__(self, facade: Any) -> None:
        self._facade = facade
        self.owner_token = str(uuid4())

    async def reserve_primary(
        self,
        actor: PluginActor,
        operation_id: str,
        approval_id: str,
        public_query: str,
    ) -> SearchReservation:
        actor = await self._fresh_actor(actor)
        existing = await self._owned_execution(actor, operation_id, required=False)
        if existing is not None:
            return SearchReservation(self._model(existing), False)
        if not isinstance(public_query, str) or not public_query:
            raise _error(actor, "CONFIG_INVALID", 400)

        operation = await self._owned_record(OPERATIONS, operation_id, actor)
        approval_record = await self._owned_record(APPROVALS, approval_id, actor)
        approval = QueryApproval.model_validate(approval_record.payload)
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        binding = await self._facade.get_record(BINDINGS, cast(str, actor.agent_id))
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        session_key = _session_key(
            actor.user_id, cast(str, actor.agent_id), cast(str, actor.thread_id)
        )
        session = await self._facade.get_record(SESSIONS, session_key)
        digest = query_digest(public_query)
        search_revision = int(operation.payload.get("search_revision", 0))
        if (
            operation.payload.get("status") in TERMINAL
            or operation.payload.get("mode", "KB_ONLY") not in {"WEB_ONLY", "HYBRID"}
            or operation.payload.get("approval_id") != approval_id
            or approval.operation_id != operation_id
            or approval.state != "PENDING"
            or approval.query_digest != digest
            or approval.search_revision != search_revision
            or sources is None
            or search_revision <= 0
            or sources.revision != search_revision
            or binding is None
            or binding.revision != int(operation.payload.get("binding_revision", 0))
            or binding.payload.get("web_allowed") is not True
            or intent is not None
            and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
            or session is None
            and int(operation.payload.get("session_revision", 0)) != 0
            or session is not None
            and session.revision != int(operation.payload.get("session_revision", 0))
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        self._validate_source_snapshot(actor, sources)

        claim_token = str(uuid4())
        payload = {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "operation_id": operation_id,
            "approval_id": approval_id,
            "user_id": actor.user_id,
            "agent_id": actor.agent_id,
            "thread_id": actor.thread_id,
            "session_revision": operation.payload["session_revision"],
            "config_revision": operation.payload["config_revision"],
            "binding_revision": operation.payload["binding_revision"],
            "grant_revision": operation.payload["grant_revision"],
            "search_revision": search_revision,
            "query_digest": digest,
            "primary_provider": "SEARXNG",
            "secondary_provider": "FIRECRAWL",
            "state": "PRIMARY_RESERVED",
            "attempt_count": 1,
            "active_role": "PRIMARY",
            "claim_token": claim_token,
            "owner_boot_token": self.owner_token,
            "result_provider": None,
            "error_code": None,
        }
        consumed = {**approval_record.payload, "state": "CONSUMED"}
        checks = (
            RecordCheck(OPERATIONS, operation_id, operation.revision),
            RecordCheck(BINDINGS, cast(str, actor.agent_id), binding.revision),
            RecordCheck(SEARCH_SOURCES, CONFIG_KEY, search_revision),
            RecordCheck(SEARCH_INTENTS, CONFIG_KEY, intent.revision if intent is not None else 0),
            RecordCheck(
                SESSIONS,
                session_key,
                session.revision if session is not None else 0,
            ),
        )
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        APPROVALS,
                        approval_id,
                        approval_record.owner_user_id,
                        consumed,
                        approval_record.revision,
                    ),
                    RecordWrite(
                        SEARCH_EXECUTIONS,
                        operation_id,
                        actor.user_id,
                        payload,
                        0,
                    ),
                ),
                checks=checks,
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raced = await self._owned_execution(actor, operation_id, required=False)
                if raced is not None:
                    return SearchReservation(self._model(raced), False)
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        forget_public_query(self._facade, approval_id)
        return SearchReservation(self._model(saved[1]), True)

    async def reserve_secondary(
        self,
        actor: PluginActor,
        operation_id: str,
        claim_token: str,
    ) -> SearchExecutionRecord:
        actor = await self._fresh_actor(actor)
        record = await self._owned_execution(actor, operation_id)
        assert record is not None
        execution = self._model(record)
        if (
            execution.claim_token != claim_token
            or execution.state != "PRIMARY_RESERVED"
            or execution.attempt_count != 1
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        operation_revision, intent_revision, session_revision = await self._assert_current(
            actor, record
        )
        payload = {
            **record.payload,
            "state": "SECONDARY_RESERVED",
            "attempt_count": 2,
            "active_role": "SECONDARY",
        }
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        SEARCH_EXECUTIONS,
                        operation_id,
                        record.owner_user_id,
                        payload,
                        record.revision,
                    ),
                ),
                checks=self._current_checks(
                    execution, operation_revision, intent_revision, session_revision
                ),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        return self._model(saved[0])

    async def pre_send(
        self,
        actor: PluginActor,
        operation_id: str,
        claim_token: str,
        role: SearchRole,
    ) -> None:
        actor = await self._fresh_actor(actor)
        record = await self._owned_execution(actor, operation_id)
        assert record is not None
        execution = self._model(record)
        expected_state = "PRIMARY_RESERVED" if role == "PRIMARY" else "SECONDARY_RESERVED"
        expected_count = 1 if role == "PRIMARY" else 2
        approval = await self._owned_record(APPROVALS, execution.approval_id, actor)
        if (
            execution.claim_token != claim_token
            or execution.state != expected_state
            or execution.active_role != role
            or execution.attempt_count != expected_count
            or approval.payload.get("state") != "CONSUMED"
            or approval.payload.get("query_digest") != execution.query_digest
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        await self._assert_current(actor, record)

    async def finish(
        self,
        actor: PluginActor,
        operation_id: str,
        claim_token: str,
        status: Literal["COMPLETED", "EMPTY", "FAILED"],
        *,
        provider: Literal["SEARXNG", "FIRECRAWL"] | None = None,
        error_code: str | None = None,
    ) -> SearchExecutionRecord:
        actor = await self._fresh_actor(actor)
        record = await self._owned_execution(actor, operation_id)
        assert record is not None
        execution = self._model(record)
        if execution.claim_token != claim_token or execution.state not in {
            "PRIMARY_RESERVED",
            "SECONDARY_RESERVED",
        }:
            raise _error(actor, "CONFIG_CONFLICT", 409)
        operation_revision, intent_revision, session_revision = await self._assert_current(
            actor, record
        )
        payload = {
            **record.payload,
            "state": status,
            "active_role": None,
            "result_provider": provider,
            "error_code": error_code,
        }
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        SEARCH_EXECUTIONS,
                        operation_id,
                        record.owner_user_id,
                        payload,
                        record.revision,
                    ),
                ),
                checks=self._current_checks(
                    execution, operation_revision, intent_revision, session_revision
                ),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        return self._model(saved[0])

    async def terminate(
        self,
        actor: PluginActor,
        operation_id: str,
        status: SearchTerminal,
        *,
        error_code: str | None = None,
    ) -> SearchExecutionRecord:
        record = await self._owned_execution(actor, operation_id)
        assert record is not None
        execution = self._model(record)
        if execution.state not in {"PRIMARY_RESERVED", "SECONDARY_RESERVED"}:
            return execution
        payload = {
            **record.payload,
            "state": status,
            "active_role": None,
            "error_code": error_code,
        }
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        SEARCH_EXECUTIONS,
                        operation_id,
                        record.owner_user_id,
                        payload,
                        record.revision,
                    ),
                )
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                latest = await self._owned_execution(actor, operation_id)
                assert latest is not None
                return self._model(latest)
            raise
        return self._model(saved[0])

    async def get(self, actor: PluginActor, operation_id: str) -> SearchExecutionRecord | None:
        record = await self._owned_execution(actor, operation_id, required=False)
        return self._model(record) if record is not None else None

    async def _fresh_actor(self, actor: PluginActor) -> PluginActor:
        try:
            return await self._facade.reauthorize_actor(actor, "authenticated")
        except PluginHostError:
            raise _error(actor, "ACCESS_DENIED", 403) from None

    async def _assert_current(self, actor: PluginActor, record: HostRecord) -> tuple[int, int, int]:
        execution = self._model(record)
        operation = await self._owned_record(OPERATIONS, execution.operation_id, actor)
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        binding = await self._facade.get_record(BINDINGS, execution.agent_id)
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        session = await self._facade.get_record(
            SESSIONS,
            _session_key(execution.user_id, execution.agent_id, execution.thread_id),
        )
        if (
            operation.payload.get("status") in TERMINAL
            or sources is None
            or sources.revision != execution.search_revision
            or binding is None
            or binding.revision != execution.binding_revision
            or binding.payload.get("web_allowed") is not True
            or intent is not None
            and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
            or session is None
            and execution.session_revision != 0
            or session is not None
            and session.revision != execution.session_revision
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        return (
            operation.revision,
            intent.revision if intent is not None else 0,
            session.revision if session is not None else 0,
        )

    @staticmethod
    def _current_checks(
        execution: SearchExecutionRecord,
        operation_revision: int,
        intent_revision: int,
        session_revision: int,
    ) -> tuple[RecordCheck, ...]:
        return (
            RecordCheck(OPERATIONS, execution.operation_id, operation_revision),
            RecordCheck(BINDINGS, execution.agent_id, execution.binding_revision),
            RecordCheck(SEARCH_SOURCES, CONFIG_KEY, execution.search_revision),
            RecordCheck(SEARCH_INTENTS, CONFIG_KEY, intent_revision),
            RecordCheck(
                SESSIONS,
                _session_key(execution.user_id, execution.agent_id, execution.thread_id),
                session_revision,
            ),
        )

    async def _owned_record(
        self, namespace: str, logical_key: str, actor: PluginActor
    ) -> HostRecord:
        record = await self._facade.get_record(namespace, logical_key)
        if record is None:
            raise _error(actor, "NOT_FOUND", 404)
        if (
            record.owner_user_id != actor.user_id
            or record.payload.get("user_id") != actor.user_id
            or record.payload.get("agent_id") != actor.agent_id
            or record.payload.get("thread_id") != actor.thread_id
        ):
            raise _error(actor, "ACCESS_DENIED", 403)
        return record

    async def _owned_execution(
        self,
        actor: PluginActor,
        operation_id: str,
        *,
        required: bool = True,
    ) -> HostRecord | None:
        record = await self._facade.get_record(SEARCH_EXECUTIONS, operation_id)
        if record is None:
            if required:
                raise _error(actor, "NOT_FOUND", 404)
            return None
        if (
            record.owner_user_id != actor.user_id
            or record.payload.get("user_id") != actor.user_id
            or record.payload.get("agent_id") != actor.agent_id
            or record.payload.get("thread_id") != actor.thread_id
        ):
            raise _error(actor, "ACCESS_DENIED", 403)
        return record

    @staticmethod
    def _model(record: HostRecord) -> SearchExecutionRecord:
        return SearchExecutionRecord.model_validate({**record.payload, "revision": record.revision})

    @staticmethod
    def _validate_source_snapshot(actor: PluginActor, sources: HostRecord) -> None:
        primary = sources.payload.get("primary")
        secondary = sources.payload.get("secondary")
        if (
            not isinstance(primary, dict)
            or primary.get("provider") != "SEARXNG"
            or not isinstance(primary.get("base_url"), str)
            or not isinstance(secondary, dict)
            or secondary.get("provider") != "FIRECRAWL"
            or not isinstance(secondary.get("base_url"), str)
        ):
            raise _error(actor, "CONFIG_INVALID", 400)
