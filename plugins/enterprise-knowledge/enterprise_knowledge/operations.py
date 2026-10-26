"""CAS-backed query operation and approval records; no network dispatch lives here."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

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
from .sessions import SESSIONS, _session_key

OPERATIONS = "operations"
APPROVALS = "approvals"
TERMINAL = {"COMPLETED", "EMPTY", "DENIED", "FAILED", "CANCELLED", "INTERRUPTED"}
_NEXT = {
    "VALIDATING": {"RETRIEVING", "DENIED", "FAILED", "CANCELLED", "INTERRUPTED"},
    "RETRIEVING": {"GENERATING", "EMPTY", "DENIED", "FAILED", "CANCELLED", "INTERRUPTED"},
    "GENERATING": {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"},
}


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
        if session.mode != "KB_ONLY" or session.invalid:
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
            "connection_id": session.connection_id,
            "status": "VALIDATING",
            "error_code": None,
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
        if session.connection_id is not None:
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
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        OPERATIONS, operation_id, record.owner_user_id, payload, expected_revision
                    ),
                )
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        return OperationRecord.model_validate({**saved[0].payload, "revision": saved[0].revision})

    async def approve(
        self, actor: PluginActor, operation_id: str, public_query: str
    ) -> QueryApproval:
        operation = await self._owned_operation(actor, operation_id)
        if operation.payload["status"] not in {"VALIDATING", "RETRIEVING"}:
            raise _error(actor, "CONFIG_CONFLICT", 409)
        approval_id = str(uuid4())
        payload = {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "approval_id": approval_id,
            "operation_id": operation_id,
            "user_id": actor.user_id,
            "agent_id": actor.agent_id,
            "thread_id": actor.thread_id,
            "public_query": public_query,
            "session_revision": operation.payload["session_revision"],
            "config_revision": operation.payload["config_revision"],
            "binding_revision": operation.payload["binding_revision"],
            "grant_revision": operation.payload["grant_revision"],
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
        return QueryApproval.model_validate(records[0].payload)

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
        return QueryApproval.model_validate(saved[0].payload)

    async def consume_approval(
        self, actor: PluginActor, approval_id: str, expected_revision: int
    ) -> str:
        record = await self._owned_approval(actor, approval_id)
        approval = QueryApproval.model_validate(record.payload)
        operation = await self._owned_operation(actor, approval.operation_id)
        if (
            record.revision != expected_revision
            or approval.state != "PENDING"
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
        return approval.public_query

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
