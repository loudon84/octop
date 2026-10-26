"""CAS-backed query operation and approval records; no network dispatch lives here."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4
from weakref import WeakKeyDictionary

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import (
    BINDINGS,
    CONNECTION_INTENTS,
    CONNECTIONS,
    GRANTS,
    ConnectionService,
)
from .i18n import error_message
from .models import OperationRecord, QueryApproval, SessionSnapshot
from .search_settings import CONFIG_KEY, SEARCH_INTENTS, SEARCH_SOURCES
from .sessions import SESSIONS, _session_key

OPERATIONS = "operations"
APPROVALS = "approvals"
TERMINAL = {"COMPLETED", "EMPTY", "DENIED", "FAILED", "CANCELLED", "INTERRUPTED"}
_NEXT = {
    "VALIDATING": {"RETRIEVING", "DENIED", "FAILED", "CANCELLED", "INTERRUPTED"},
    "RETRIEVING": {"GENERATING", "EMPTY", "DENIED", "FAILED", "CANCELLED", "INTERRUPTED"},
    "GENERATING": {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"},
}
_PUBLIC_QUERIES: WeakKeyDictionary[Any, dict[str, str]] = WeakKeyDictionary()


def forget_public_query(facade: Any, approval_id: str) -> None:
    queries = _PUBLIC_QUERIES.get(facade)
    if queries is not None:
        queries.pop(approval_id, None)


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _record_key(*parts: object) -> str:
    return json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))


class OperationService:
    def __init__(self, facade: Any) -> None:
        self._facade = facade

    async def create(self, actor: PluginActor, session: SessionSnapshot) -> OperationRecord:
        if (
            actor.user_id <= 0
            or actor.agent_id != session.agent_id
            or actor.thread_id != session.thread_id
        ):
            raise _error(actor, "ACCESS_DENIED", 403)
        if session.invalid:
            raise _error(actor, "MODE_NOT_ALLOWED", 409)
        if session.mode == "WEB_ONLY" and not session.capabilities.web_ready:
            raise _error(actor, "MODE_NOT_ALLOWED", 409)
        if session.mode == "HYBRID" and (
            not session.capabilities.web_ready
            or not session.capabilities.knowledge_ready
            or not session.selected_kb_ids
        ):
            raise _error(actor, "MODE_NOT_ALLOWED", 409)
        operation_id = str(uuid4())
        payload = {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "operation_id": operation_id,
            "user_id": actor.user_id,
            "agent_id": session.agent_id,
            "thread_id": session.thread_id,
            "session_revision": session.revision,
            "config_revision": session.config_revision,
            "binding_revision": session.binding_revision,
            "grant_revision": session.grant_revision,
            "search_revision": session.search_revision,
            "connection_id": session.connection_id,
            "mode": session.mode,
            "status": "VALIDATING",
            "error_code": None,
            "partial_failure": None,
            "approval_id": None,
        }
        checks = [
            RecordCheck(
                SESSIONS,
                _session_key(actor.user_id, session.agent_id, session.thread_id),
                session.revision,
            ),
            RecordCheck(BINDINGS, session.agent_id, session.binding_revision),
        ]
        search_intent: HostRecord | None = None
        if session.connection_id is not None and session.mode != "WEB_ONLY":
            intent_revision = await ConnectionService(self._facade).assert_connection_available(
                session.connection_id, actor
            )
            checks.extend(
                (
                    RecordCheck(CONNECTIONS, session.connection_id, session.config_revision),
                    RecordCheck(
                        GRANTS,
                        f"{session.connection_id}:{actor.user_id}",
                        session.grant_revision,
                    ),
                    RecordCheck(CONNECTION_INTENTS, session.connection_id, intent_revision),
                )
            )
        if session.mode in {"WEB_ONLY", "HYBRID"}:
            search_intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
            if search_intent is not None and search_intent.payload.get("state") in {
                "PREPARING",
                "BLOCKED",
            }:
                raise _error(actor, "RECOVERY_BLOCKED", 409)
            checks.extend(
                (
                    RecordCheck(SEARCH_SOURCES, CONFIG_KEY, session.search_revision),
                    RecordCheck(
                        SEARCH_INTENTS,
                        CONFIG_KEY,
                        search_intent.revision if search_intent is not None else 0,
                    ),
                )
            )
        try:
            records = await self._facade.commit(
                (RecordWrite(OPERATIONS, operation_id, actor.user_id, payload, 0),),
                checks=tuple(checks),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        return OperationRecord.model_validate(
            {**records[0].payload, "revision": records[0].revision}
        )

    async def transition(
        self,
        actor: PluginActor,
        operation_id: str,
        expected_revision: int,
        status: Literal[
            "RETRIEVING",
            "GENERATING",
            "COMPLETED",
            "EMPTY",
            "DENIED",
            "FAILED",
            "CANCELLED",
            "INTERRUPTED",
        ],
        *,
        error_code: str | None = None,
    ) -> OperationRecord:
        record = await self._owned_operation(actor, operation_id)
        current = record.payload["status"]
        if record.revision != expected_revision or status not in _NEXT.get(current, set()):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        payload = {**record.payload, "status": status, "error_code": error_code}
        writes = [
            RecordWrite(OPERATIONS, operation_id, record.owner_user_id, payload, expected_revision)
        ]
        approval_id = record.payload.get("approval_id")
        if status in {"CANCELLED", "INTERRUPTED"} and isinstance(approval_id, str):
            approval = await self._facade.get_record(APPROVALS, approval_id)
            if (
                approval is not None
                and approval.owner_user_id == actor.user_id
                and approval.payload.get("operation_id") == operation_id
                and approval.payload.get("state") == "PENDING"
            ):
                writes.append(
                    RecordWrite(
                        APPROVALS,
                        approval_id,
                        approval.owner_user_id,
                        {**approval.payload, "state": "CANCELLED"},
                        approval.revision,
                    )
                )
        if status in {"CANCELLED", "INTERRUPTED"}:
            execution = await self._facade.get_record("search_executions", operation_id)
            if (
                execution is not None
                and execution.owner_user_id == actor.user_id
                and execution.payload.get("operation_id") == operation_id
                and execution.payload.get("state") in {"PRIMARY_RESERVED", "SECONDARY_RESERVED"}
            ):
                writes.append(
                    RecordWrite(
                        "search_executions",
                        operation_id,
                        execution.owner_user_id,
                        {
                            **execution.payload,
                            "state": status,
                            "active_role": None,
                            "error_code": error_code,
                        },
                        execution.revision,
                    )
                )
        try:
            saved = await self._facade.commit(tuple(writes))
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        if status in {"CANCELLED", "INTERRUPTED"} and isinstance(approval_id, str):
            forget_public_query(self._facade, approval_id)
        return OperationRecord.model_validate({**saved[0].payload, "revision": saved[0].revision})

    async def approve(
        self, actor: PluginActor, operation_id: str, public_query: str
    ) -> QueryApproval:
        if not isinstance(public_query, str) or not public_query:
            raise _error(actor, "CONFIG_INVALID", 400)
        operation = await self._owned_operation(actor, operation_id)
        if operation.payload["status"] not in {"VALIDATING", "RETRIEVING"}:
            raise _error(actor, "CONFIG_CONFLICT", 409)
        replaced_approval_id = operation.payload.get("approval_id")
        approval_id = str(uuid4())
        payload = {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "approval_id": approval_id,
            "operation_id": operation_id,
            "user_id": actor.user_id,
            "agent_id": actor.agent_id,
            "thread_id": actor.thread_id,
            "query_digest": hashlib.sha256(public_query.encode("utf-8")).hexdigest(),
            "session_revision": operation.payload["session_revision"],
            "config_revision": operation.payload["config_revision"],
            "binding_revision": operation.payload["binding_revision"],
            "grant_revision": operation.payload["grant_revision"],
            "search_revision": operation.payload.get("search_revision", 0),
            "state": "PENDING",
        }
        updated_operation = {**operation.payload, "approval_id": approval_id}
        try:
            records = await self._facade.commit(
                (
                    RecordWrite(APPROVALS, approval_id, actor.user_id, payload, 0),
                    RecordWrite(
                        OPERATIONS,
                        operation_id,
                        actor.user_id,
                        updated_operation,
                        operation.revision,
                    ),
                )
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        if isinstance(replaced_approval_id, str):
            forget_public_query(self._facade, replaced_approval_id)
        _PUBLIC_QUERIES.setdefault(self._facade, {})[approval_id] = public_query
        return QueryApproval.model_validate({**records[0].payload, "public_query": public_query})

    async def cancel_approval(
        self, actor: PluginActor, approval_id: str, expected_revision: int
    ) -> QueryApproval:
        record = await self._owned_approval(actor, approval_id)
        if record.revision != expected_revision or record.payload["state"] != "PENDING":
            raise _error(actor, "CONFIG_CONFLICT", 409)
        payload = {**record.payload, "state": "CANCELLED"}
        saved = await self._facade.commit(
            (RecordWrite(APPROVALS, approval_id, actor.user_id, payload, expected_revision),)
        )
        forget_public_query(self._facade, approval_id)
        return QueryApproval.model_validate(saved[0].payload)

    async def revoke_for_operation(self, actor: PluginActor, operation_id: str) -> QueryApproval:
        operation = await self._owned_operation(actor, operation_id)
        approval_id = operation.payload.get("approval_id")
        if not isinstance(approval_id, str):
            raise _error(actor, "NOT_FOUND", 404)
        approval = await self._owned_approval(actor, approval_id)
        return await self.cancel_approval(actor, approval_id, approval.revision)

    async def approved_query(self, actor: PluginActor, operation_id: str) -> tuple[str, str] | None:
        operation = await self._owned_operation(actor, operation_id)
        approval_id = operation.payload.get("approval_id")
        if not isinstance(approval_id, str):
            return None
        approval_record = await self._owned_approval(actor, approval_id)
        approval = QueryApproval.model_validate(approval_record.payload)
        public_query = _PUBLIC_QUERIES.get(self._facade, {}).get(approval_id)
        if (
            approval.state != "PENDING"
            or approval.operation_id != operation_id
            or public_query is None
            or approval.query_digest != hashlib.sha256(public_query.encode("utf-8")).hexdigest()
        ):
            return None
        return approval_id, public_query

    async def consume_approval(
        self, actor: PluginActor, approval_id: str, expected_revision: int
    ) -> str:
        record = await self._owned_approval(actor, approval_id)
        approval = QueryApproval.model_validate(record.payload)
        operation = await self._owned_operation(actor, approval.operation_id)
        public_query = _PUBLIC_QUERIES.get(self._facade, {}).get(approval_id)
        query_digest = (
            hashlib.sha256(public_query.encode("utf-8")).hexdigest()
            if public_query is not None
            else None
        )
        if (
            record.revision != expected_revision
            or approval.state != "PENDING"
            or public_query is None
            or approval.query_digest != query_digest
            or operation.payload.get("approval_id") != approval_id
            or operation.payload["status"] != "VALIDATING"
            or any(
                approval.model_dump()[field] != operation.payload[field]
                for field in (
                    "user_id",
                    "agent_id",
                    "thread_id",
                    "session_revision",
                    "config_revision",
                    "binding_revision",
                    "grant_revision",
                    "search_revision",
                )
            )
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        payload = {**record.payload, "state": "CONSUMED"}
        checks = [
            RecordCheck(APPROVALS, approval_id, expected_revision),
            RecordCheck(OPERATIONS, approval.operation_id, operation.revision),
            RecordCheck(
                SESSIONS,
                _session_key(
                    actor.user_id, operation.payload["agent_id"], operation.payload["thread_id"]
                ),
                operation.payload["session_revision"],
            ),
            RecordCheck(
                BINDINGS, operation.payload["agent_id"], operation.payload["binding_revision"]
            ),
        ]
        connection_id = operation.payload.get("connection_id")
        if connection_id is not None:
            intent_revision = await ConnectionService(self._facade).assert_connection_available(
                connection_id, actor
            )
            checks.append(
                RecordCheck(CONNECTIONS, connection_id, operation.payload["config_revision"])
            )
            checks.append(
                RecordCheck(
                    GRANTS, f"{connection_id}:{actor.user_id}", operation.payload["grant_revision"]
                )
            )
            checks.append(RecordCheck(CONNECTION_INTENTS, connection_id, intent_revision))
        try:
            await self._facade.commit(
                (RecordWrite(APPROVALS, approval_id, actor.user_id, payload, expected_revision),),
                checks=tuple(checks),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        forget_public_query(self._facade, approval_id)
        return public_query

    async def _owned_operation(self, actor: PluginActor, operation_id: str) -> HostRecord:
        record = await self._facade.get_record(OPERATIONS, operation_id)
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

    async def _owned_approval(self, actor: PluginActor, approval_id: str) -> HostRecord:
        record = await self._facade.get_record(APPROVALS, approval_id)
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
