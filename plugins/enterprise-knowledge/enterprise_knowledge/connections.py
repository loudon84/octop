"""Host-facade backed configuration records and management operations."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
    RequiredExtensionWrite,
)

from .i18n import error_message
from .models import (
    SCHEMA_ID,
    BindingConfig,
    BindingSaveInput,
    ConnectionConfig,
    ConnectionSaveInput,
    GrantConfig,
    GrantSaveInput,
    ProbeComponentSummary,
    SettingsOutput,
)

CONNECTIONS = "connections"
GRANTS = "grants"
BINDINGS = "bindings"
CONNECTION_INTENTS = "connection_intents"
CONNECTION_RECOVERY = "connection_recovery"
_KNOWLEDGE_REQUIRED = {
    "enterprise-knowledge": "0.1.0",
    "enterprise-policy": "0.1.0",
}


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _host_error(exc: PluginHostError, actor: PluginActor) -> PluginActionError:
    if exc.code == "conflict":
        return _error(actor, "CONFIG_CONFLICT", 409)
    if exc.code == "unavailable":
        return _error(actor, "PLUGIN_NOT_READY", 409)
    if exc.code == "denied":
        return _error(actor, "ACCESS_DENIED", 403)
    if exc.code == "not_found":
        return _error(actor, "NOT_FOUND", 404)
    return _error(actor, "CONFIG_INVALID", 400)


class ConnectionService:
    """One service object bound to one active Host facade generation."""

    def __init__(self, facade: Any) -> None:
        self._facade = facade

    async def get_settings(self, actor: PluginActor) -> SettingsOutput:
        from .probes import connection_projection

        if actor.is_admin:
            records = await self._facade.list_records(CONNECTIONS)
            grants = await self._facade.list_records(GRANTS)
            intents = {
                record.logical_key: record.payload
                for record in await self._facade.list_records(CONNECTION_INTENTS)
            }
            connections = [
                self._connection(record).model_copy(
                    update={
                        "recovery_blocked": intents.get(record.logical_key, {}).get("state")
                        in {"PREPARING", "BLOCKED"}
                    }
                )
                for record in records
            ]
            for index, record in enumerate(records):
                status, components = await connection_projection(
                    self._facade,
                    record,
                    await self._facade.get_record(CONNECTION_INTENTS, record.logical_key),
                )
                connections[index] = connections[index].model_copy(
                    update={
                        "probe_status": status,
                        "probe_components": {
                            component: ProbeComponentSummary.model_validate(value)
                            for component, value in components.items()
                        },
                    }
                )
            capabilities = {
                "can_manage_connections": True,
                "can_manage_grants": True,
                "can_manage_bindings": True,
            }
        else:
            if "agent_plugins" not in actor.permissions or not actor.agent_id:
                raise _error(actor, "ACCESS_DENIED", 403)
            try:
                await self._facade.get_requirement(actor, actor.agent_id)
            except PluginHostError as exc:
                raise _host_error(exc, actor) from None
            records = await self._facade.list_records(CONNECTIONS)
            intents = {
                record.logical_key: record.payload
                for record in await self._facade.list_records(CONNECTION_INTENTS)
            }
            connections = []
            for record in records:
                if not record.payload.get("enabled") or intents.get(record.logical_key, {}).get(
                    "state"
                ) in {"PREPARING", "BLOCKED"}:
                    continue
                grant = await self._facade.get_record(
                    GRANTS, f"{record.logical_key}:{actor.user_id}"
                )
                if grant is None:
                    continue
                allowed = set(record.payload.get("allowed_kb_ids", [])) & set(
                    grant.payload.get("user_allowed_kb_ids", [])
                )
                connections.append(
                    self._connection(record).model_copy(
                        update={
                            "allowed_kb_ids": [
                                kb_id
                                for kb_id in record.payload.get("allowed_kb_ids", [])
                                if kb_id in allowed
                            ]
                        }
                    )
                )
                status, _components = await connection_projection(self._facade, record)
                connections[-1] = connections[-1].model_copy(
                    update={
                        "probe_status": status,
                        "probe_components": {},
                    }
                )
            grants = []
            capabilities = {
                "can_manage_connections": False,
                "can_manage_grants": False,
                "can_manage_bindings": True,
            }
        return SettingsOutput(
            capabilities=capabilities,
            connections=connections,
            grants=[self._grant(record) for record in grants],
        )

    async def save_connection(
        self, actor: PluginActor, request: ConnectionSaveInput
    ) -> ConnectionConfig:
        self._require_admin(actor)
        old = None
        owner_user_id = actor.user_id
        credential_owner = actor.user_id
        connection_id = str(request.connection_id or uuid4())
        if request.connection_id is not None:
            old = await self._facade.get_record(CONNECTIONS, connection_id)
            self._check_expected_revision(actor, old, request.expected_revision)
            if old is None:
                raise _error(actor, "NOT_FOUND", 404)
            await self.assert_connection_available(connection_id, actor)
            owner_user_id = old.owner_user_id
            credential_owner = int(old.payload["credential_owner_user_id"])
        elif request.expected_revision != 0:
            raise _error(actor, "CONFIG_CONFLICT", 409)

        old_payload: Mapping[str, Any] = old.payload if old is not None else {}
        credential_ref = old_payload.get("credential_ref")
        if not credential_ref and request.api_key is None:
            raise _error(actor, "CONFIG_INVALID", 400)
        secret = request.api_key.get_secret_value() if request.api_key is not None else None
        if request.api_key is not None and not secret:
            raise _error(actor, "CONFIG_INVALID", 400)

        payload = {
            "schema_id": SCHEMA_ID,
            "version": 1,
            "connection_id": connection_id,
            "name": request.name,
            "base_url": request.base_url,
            "tenant_id": request.tenant_id,
            "allowed_kb_ids": list(request.allowed_kb_ids),
            "enabled": request.enabled,
            "credential_ref": credential_ref,
            "credential_owner_user_id": credential_owner,
            "probe_status": "CONFIGURED" if request.enabled else "DISABLED",
            "probe_details": None,
        }
        # Validate the exact public DTO before creating recovery material or staging a key.
        self._connection_candidate(
            payload,
            request.expected_revision + 1,
            credential_configured=request.api_key is not None or bool(credential_ref),
        )
        planned_staged_ref = str(uuid4()) if request.api_key is not None else None
        try:
            intent, recovery = await self._start_connection_intent(
                connection_id,
                owner_user_id,
                old,
                credential_ref,
                planned_staged_ref,
                credential_owner if planned_staged_ref is not None else None,
                actor=actor,
            )
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        staged_ref: str | None = None
        activations: tuple[CredentialActivation, ...] = ()
        if request.api_key is not None:
            assert secret is not None
            try:
                staged_ref = await self._facade.stage_credential(
                    credential_owner, {"api_key": secret}, credential_id=planned_staged_ref
                )
            except PluginHostError as exc:
                try:
                    await self._finish_intent(intent, recovery, "ABORTED", None)
                except PluginHostError:
                    raise _error(actor, "ROLLBACK_FAILED", 500) from None
                raise _host_error(exc, actor) from None
            credential_ref = staged_ref
            activations = (CredentialActivation(staged_ref, credential_owner, 1),)
        payload["credential_ref"] = credential_ref
        self._connection_candidate(payload, request.expected_revision + 1)
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        CONNECTIONS,
                        connection_id,
                        owner_user_id,
                        payload,
                        request.expected_revision,
                    ),
                    RecordWrite(
                        CONNECTION_INTENTS,
                        connection_id,
                        intent.owner_user_id,
                        self._intent_payload(intent.payload, "RESOLVED", staged_ref),
                        intent.revision,
                    ),
                    RecordWrite(
                        CONNECTION_RECOVERY,
                        recovery.logical_key,
                        recovery.owner_user_id,
                        self._intent_payload(recovery.payload, "RESOLVED", staged_ref),
                        recovery.revision,
                    ),
                ),
                activations,
            )
        except PluginHostError as exc:
            if staged_ref is not None:
                if await self._release_staged(staged_ref, credential_owner):
                    try:
                        await self._finish_intent(intent, recovery, "ABORTED", None)
                    except PluginHostError:
                        raise _error(actor, "ROLLBACK_FAILED", 500) from None
                    raise _host_error(exc, actor) from None
                with suppress(PluginHostError):
                    await self._finish_intent(intent, recovery, "BLOCKED", staged_ref)
                    # PREPARING is already a durable fail-closed quarantine.
                raise _error(actor, "ROLLBACK_FAILED", 500) from None
            try:
                await self._finish_intent(intent, recovery, "ABORTED", None)
            except PluginHostError:
                raise _error(actor, "ROLLBACK_FAILED", 500) from None
            raise _host_error(exc, actor) from None
        return self._connection(saved[0])

    async def save_grant(self, actor: PluginActor, request: GrantSaveInput) -> GrantConfig:
        self._require_admin(actor)
        connection_id = str(request.connection_id)
        connection = await self._facade.get_record(CONNECTIONS, connection_id)
        if connection is None:
            raise _error(actor, "NOT_FOUND", 404)
        intent_revision = await self.assert_connection_available(connection_id, actor)
        if not set(request.user_allowed_kb_ids).issubset(connection.payload["allowed_kb_ids"]):
            raise _error(actor, "SCOPE_INVALID", 400)
        key = f"{connection_id}:{request.user_id}"
        old = await self._facade.get_record(GRANTS, key)
        self._check_expected_revision(actor, old, request.expected_revision)
        owner = old.owner_user_id if old is not None else actor.user_id
        payload = {
            "schema_id": SCHEMA_ID,
            "version": 1,
            "connection_id": connection_id,
            "user_id": request.user_id,
            "user_allowed_kb_ids": list(request.user_allowed_kb_ids),
        }
        try:
            saved = await self._facade.commit(
                (RecordWrite(GRANTS, key, owner, payload, request.expected_revision),),
                checks=(RecordCheck(CONNECTION_INTENTS, connection_id, intent_revision),),
            )
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        return self._grant(saved[0])

    async def get_binding(self, actor: PluginActor) -> BindingConfig:
        agent_id = self._require_agent(actor)
        try:
            await self._facade.get_requirement(actor, agent_id)
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        record = await self._facade.get_record(BINDINGS, agent_id)
        if record is None:
            return BindingConfig(agent_id=agent_id)
        return self._binding(record)

    async def save_binding(self, actor: PluginActor, request: BindingSaveInput) -> BindingConfig:
        agent_id = self._require_agent(actor)
        try:
            requirement_state = await self._facade.get_requirement(actor, agent_id)
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        key = agent_id
        old = await self._facade.get_record(BINDINGS, key)
        self._check_expected_revision(actor, old, request.expected_revision)
        owner = old.owner_user_id if old is not None else actor.user_id
        checks: tuple[RecordCheck, ...] = ()
        connection = None
        if request.connection_ref is not None:
            connection = await self._facade.get_record(CONNECTIONS, request.connection_ref)
            if connection is None:
                raise _error(actor, "NOT_FOUND", 404)
            if request.enabled:
                intent_revision = await self.assert_connection_available(
                    request.connection_ref, actor
                )
        if request.enabled:
            if connection is None or not connection.payload.get("enabled", False):
                raise _error(actor, "CONNECTION_DISABLED", 409)
            assert request.connection_ref is not None
            if not set(request.expert_allowed_kb_ids).issubset(
                connection.payload["allowed_kb_ids"]
            ):
                raise _error(actor, "SCOPE_INVALID", 400)
            checks = (
                RecordCheck(CONNECTIONS, request.connection_ref, connection.revision),
                RecordCheck(CONNECTION_INTENTS, request.connection_ref, intent_revision),
            )
        payload = {
            "schema_id": SCHEMA_ID,
            "version": 1,
            "agent_id": agent_id,
            "connection_ref": request.connection_ref,
            "expert_allowed_kb_ids": list(request.expert_allowed_kb_ids),
            "default_kb_ids": list(request.default_kb_ids),
            "web_allowed": request.web_allowed,
            "enabled": request.enabled,
        }
        required_plugins = _KNOWLEDGE_REQUIRED if request.enabled else {}
        requirement = RequiredExtensionWrite(
            actor=actor,
            agent_id=agent_id,
            required_plugins=required_plugins,
            expected_revision=requirement_state.revision,
        )
        try:
            saved = await self._facade.commit(
                (RecordWrite(BINDINGS, key, owner, payload, request.expected_revision),),
                requirements=(requirement,),
                checks=checks,
            )
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        return self._binding(saved[0])

    async def assert_connection_available(self, connection_id: str, actor: PluginActor) -> int:
        """Reject new configuration or knowledge calls while durable recovery is pending."""
        intent = await self._facade.get_record(CONNECTION_INTENTS, connection_id)
        if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
            raise _error(actor, "RECOVERY_BLOCKED", 409)
        return intent.revision if intent is not None else 0

    async def _start_connection_intent(
        self,
        connection_id: str,
        owner_user_id: int,
        old: Any,
        old_credential_ref: Any,
        staged_credential_ref: str | None,
        staged_credential_owner_user_id: int | None,
        *,
        actor: PluginActor,
    ) -> tuple[Any, Any]:
        prior = await self._facade.get_record(CONNECTION_INTENTS, connection_id)
        if prior is not None and prior.payload.get("state") in {"PREPARING", "BLOCKED"}:
            raise _error(actor, "RECOVERY_BLOCKED", 409)
        expected = prior.revision if prior is not None else 0
        t0 = {
            "connection_revision": old.revision if old is not None else 0,
            "owner_user_id": old.owner_user_id if old is not None else owner_user_id,
            "credential_owner_user_id": (
                int(old.payload["credential_owner_user_id"]) if old is not None else owner_user_id
            ),
            "credential_ref": old_credential_ref if old is not None else None,
            "enabled": bool(old.payload.get("enabled", False)) if old is not None else False,
        }
        recovery_id = str(uuid4())
        payload = {
            "connection_id": connection_id,
            "recovery_id": recovery_id,
            "state": "PREPARING",
            "started_at": datetime.now(UTC).isoformat(),
            "retain_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "t0": t0,
            "staged_credential_ref": staged_credential_ref,
            "staged_credential_owner_user_id": staged_credential_owner_user_id,
        }
        saved = await self._facade.commit(
            (
                RecordWrite(
                    CONNECTION_INTENTS,
                    connection_id,
                    prior.owner_user_id if prior is not None else owner_user_id,
                    payload,
                    expected,
                ),
                RecordWrite(CONNECTION_RECOVERY, recovery_id, owner_user_id, payload, 0),
            ),
            checks=(
                RecordCheck(
                    CONNECTIONS,
                    connection_id,
                    old.revision if old is not None else 0,
                ),
            ),
        )
        return saved[0], saved[1]

    async def _update_intent(
        self, intent: Any, recovery: Any, payload: dict[str, Any]
    ) -> tuple[Any, Any]:
        saved = await self._facade.commit(
            (
                RecordWrite(
                    CONNECTION_INTENTS,
                    intent.logical_key,
                    intent.owner_user_id,
                    payload,
                    intent.revision,
                ),
                RecordWrite(
                    CONNECTION_RECOVERY,
                    recovery.logical_key,
                    recovery.owner_user_id,
                    payload,
                    recovery.revision,
                ),
            )
        )
        return saved[0], saved[1]

    async def _finish_intent(
        self, intent: Any, recovery: Any, state: str, staged_ref: str | None
    ) -> tuple[Any, Any]:
        payload = self._intent_payload(intent.payload, state, staged_ref)
        return await self._update_intent(intent, recovery, payload)

    @staticmethod
    def _intent_payload(
        existing: Mapping[str, Any], state: str, staged_ref: str | None
    ) -> dict[str, Any]:
        return {
            **existing,
            "state": state,
            "staged_credential_ref": (
                (staged_ref or existing.get("staged_credential_ref"))
                if state in {"BLOCKED", "ABORTED"}
                else None
            ),
            "staged_credential_owner_user_id": existing.get("staged_credential_owner_user_id")
            if state in {"BLOCKED", "ABORTED"}
            else None,
            "activated_credential_ref": staged_ref if state == "RESOLVED" else None,
            "activated_credential_owner_user_id": existing.get("staged_credential_owner_user_id")
            if state == "RESOLVED"
            else None,
        }

    async def _release_staged(self, credential_ref: str, owner_user_id: int) -> bool:
        try:
            released = await self._facade.release_staged_credential(credential_ref, owner_user_id)
        except PluginHostError:
            released = False
        return cast(bool, released)

    @staticmethod
    def _connection_candidate(
        payload: Mapping[str, Any], revision: int, *, credential_configured: bool | None = None
    ) -> ConnectionConfig:
        candidate = dict(payload)
        candidate["revision"] = revision
        candidate["credential_configured"] = (
            bool(candidate.get("credential_ref"))
            if credential_configured is None
            else credential_configured
        )
        candidate.pop("credential_ref", None)
        candidate.pop("credential_owner_user_id", None)
        candidate.pop("probe_details", None)
        return ConnectionConfig.model_validate(candidate)

    @staticmethod
    def _require_admin(actor: PluginActor) -> None:
        if not actor.is_admin:
            raise _error(actor, "ACCESS_DENIED", 403)

    @staticmethod
    def _require_agent(actor: PluginActor) -> str:
        agent_id = actor.agent_id
        if agent_id is None or not agent_id:
            raise PluginActionError(
                "AGENT_REQUIRED", error_message("AGENT_REQUIRED", actor.locale), status=400
            )
        return cast(str, agent_id)

    @staticmethod
    def _check_expected_revision(actor: PluginActor, record: Any, expected: int) -> None:
        current = record.revision if record is not None else 0
        if current != expected:
            raise _error(actor, "CONFIG_CONFLICT", 409)

    @staticmethod
    def _connection(record: Any) -> ConnectionConfig:
        data = dict(record.payload)
        data.pop("credential_ref", None)
        data.update(
            revision=record.revision,
            credential_configured=bool(record.payload.get("credential_ref")),
        )
        data.pop("credential_owner_user_id", None)
        data.pop("probe_details", None)
        return ConnectionConfig.model_validate(data)

    @staticmethod
    def _grant(record: Any) -> GrantConfig:
        return GrantConfig.model_validate({**record.payload, "revision": record.revision})

    @staticmethod
    def _binding(record: Any) -> BindingConfig:
        return BindingConfig.model_validate({**record.payload, "revision": record.revision})
