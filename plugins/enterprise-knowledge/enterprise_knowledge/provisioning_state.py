"""Local task status and explicit retry actions for enterprise provisioning."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
    RetainedRecordCheck,
    UserIdentityCheck,
)

from .provisioning import (
    AUTH_BLOCK_NAMESPACE,
    CONFIG_NAMESPACE,
    DOMAIN_NAMESPACE,
    INTENT_NAMESPACE,
    PRIMARY_KEY,
    RECEIPT_NAMESPACE,
    ProvisioningService,
    _bad_record,
    _error,
    _host_error,
    _now,
    _payload,
    _unavailable,
)
from .provisioning_models import (
    ProvisioningErrorCode,
    ProvisioningRetryInput,
    ProvisioningState,
    ProvisioningStatus,
    ProvisioningTaskRetryInput,
    ProvisioningTasksInput,
    ProvisioningTasksOutput,
    ProvisioningTaskSummary,
)
from .provisioning_records import (
    ProvisioningAuthBlockRecord,
    ProvisioningConfigPin,
    ProvisioningConfigRecord,
    ProvisioningDomainRecord,
    ProvisioningIdentity,
    ProvisioningIntentRecord,
    ProvisioningReceiptRecord,
    ProvisioningRetryAudit,
    ProvisioningTaskIndexRecord,
    ProvisioningTaskRecord,
)

TASK_NAMESPACE = "provision_tasks"
TASK_INDEX_NAMESPACE = "provision_task_index"


def _identity_record(identity: Any) -> ProvisioningIdentity:
    return ProvisioningIdentity(
        schema_id="octop.enterprise.provisioning.v1",
        version=1,
        user_id=identity.user_id,
        email=identity.email,
        disabled=identity.disabled,
        snapshot_token=identity.snapshot_token,
    )


def _as_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


class ProvisioningStateService:
    def __init__(self, facade: Any) -> None:
        self._facade = facade

    async def get_status(self, actor: PluginActor) -> ProvisioningStatus:
        if not isinstance(actor, PluginActor):
            raise _error(actor, "PROVISIONING_INVALID", 400)
        try:
            fresh = await self._facade.reauthorize_actor(actor, "authenticated")
            identity = await self._facade.read_user_identity(fresh.user_id)
            task_row = await self._facade.get_record(TASK_NAMESPACE, str(fresh.user_id))
            context = await self._read_config(fresh)
            task, index_row = await self._read_task_pair(fresh, task_row)
            intents = await self._read_task_proofs(fresh, task)
            result = self._project(fresh, identity, task_row, task, context, intents)
            final_actor = await self._facade.reauthorize_actor(fresh, "authenticated")
            final_identity = await self._facade.read_user_identity(final_actor.user_id)
            if final_identity.snapshot_token != identity.snapshot_token:
                raise _unavailable(final_actor)
            return result
        except PluginActionError:
            raise
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

    async def retry_self(
        self, actor: PluginActor, request: ProvisioningRetryInput
    ) -> ProvisioningStatus:
        if not isinstance(actor, PluginActor) or not isinstance(request, ProvisioningRetryInput):
            raise _error(actor, "PROVISIONING_INVALID", 400)
        try:
            fresh = await self._facade.reauthorize_actor(actor, "authenticated")
            identity = await self._facade.read_user_identity(fresh.user_id)
            task_row = await self._facade.get_record(TASK_NAMESPACE, str(fresh.user_id))
            context = await self._read_config(fresh)
            task, index_row = await self._read_task_pair(fresh, task_row)
            intents = await self._read_task_proofs(fresh, task)
            return await self._retry(
                fresh,
                identity,
                task_row,
                task,
                index_row,
                context,
                intents,
                request.expected_revision,
                kind="SELF",
                reason=None,
            )
        except PluginActionError:
            raise
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

    async def list_tasks(
        self, actor: PluginActor, request: ProvisioningTasksInput
    ) -> ProvisioningTasksOutput:
        if not isinstance(actor, PluginActor) or not isinstance(request, ProvisioningTasksInput):
            raise _error(actor, "PROVISIONING_INVALID", 400)
        try:
            fresh = await self._facade.reauthorize_actor(actor, "admin")
            page = await self._facade.list_user_identities(
                after_user_id=request.after_user_id, limit=request.limit
            )
            context = await self._read_config(fresh)
            summaries: list[ProvisioningTaskSummary] = []
            checked: dict[int, Any] = {}
            for identity in page.items:
                row = await self._facade.get_record(TASK_NAMESPACE, str(identity.user_id))
                if row is None:
                    continue
                task, _index_row = await self._read_task_pair(fresh, row)
                intents = await self._read_task_proofs(fresh, task)
                status = self._project(fresh, identity, row, task, context, intents)
                if task is None:
                    raise _unavailable(fresh)
                summaries.append(
                    ProvisioningTaskSummary(
                        task_id=task.task_id,
                        user_id=identity.user_id,
                        email=identity.email,
                        status=status,
                    )
                )
                checked[identity.user_id] = identity
            await self._facade.reauthorize_actor(fresh, "admin")
            for identity in checked.values():
                current = await self._facade.read_user_identity(identity.user_id)
                if current.snapshot_token != identity.snapshot_token:
                    raise _unavailable(fresh)
            return ProvisioningTasksOutput(items=summaries, next_user_id=page.next_user_id)
        except PluginActionError:
            raise
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

    async def retry_task(
        self, actor: PluginActor, request: ProvisioningTaskRetryInput
    ) -> ProvisioningStatus:
        if not isinstance(actor, PluginActor) or not isinstance(
            request, ProvisioningTaskRetryInput
        ):
            raise _error(actor, "PROVISIONING_INVALID", 400)
        try:
            fresh = await self._facade.reauthorize_actor(actor, "admin")
            index_row = await self._facade.get_record(TASK_INDEX_NAMESPACE, request.task_id)
            if index_row is None:
                raise _error(fresh, "PROVISIONING_NOT_FOUND", 404)
            index = _bad_record(fresh, ProvisioningTaskIndexRecord, index_row.payload)
            if (
                index_row.logical_key != request.task_id
                or index_row.owner_user_id != index.local_user_id
            ):
                raise _unavailable(fresh)
            task_row = await self._facade.get_record(TASK_NAMESPACE, str(index.local_user_id))
            if task_row is None:
                try:
                    await self._facade.read_user_identity(index.local_user_id)
                except PluginHostError as exc:
                    if exc.code == "not_found":
                        raise _error(fresh, "PROVISIONING_NOT_FOUND", 404) from None
                    raise
                raise _unavailable(fresh)
            task, actual_index = await self._read_task_pair(fresh, task_row)
            if task is None or actual_index is None or task.task_id != request.task_id:
                raise _unavailable(fresh)
            identity = await self._facade.read_user_identity(index.local_user_id)
            context = await self._read_config(fresh)
            intents = await self._read_task_proofs(fresh, task)
            return await self._retry(
                fresh,
                identity,
                task_row,
                task,
                actual_index,
                context,
                intents,
                request.expected_revision,
                kind="ADMIN",
                reason=request.reason,
            )
        except PluginActionError:
            raise
        except PluginHostError as exc:
            if exc.code == "not_found":
                raise _error(actor, "PROVISIONING_NOT_FOUND", 404) from None
            raise _host_error(exc, actor) from None

    async def _read_config(
        self, actor: PluginActor
    ) -> tuple[Any, Any, Any, Any, Any, Any, int, Any | None]:
        config_row = await self._facade.get_record(CONFIG_NAMESPACE, PRIMARY_KEY)
        anchor_row = await self._facade.get_retained_record(DOMAIN_NAMESPACE, PRIMARY_KEY)
        config = (
            _bad_record(actor, ProvisioningConfigRecord, config_row.payload)
            if config_row is not None
            else None
        )
        anchor = (
            _bad_record(actor, ProvisioningDomainRecord, anchor_row.payload)
            if anchor_row is not None
            else None
        )
        if config is not None and (
            anchor is None
            or anchor_row is None
            or config.domain_id != anchor.domain_id
            or config.base_url != anchor.canonical_base_url
        ):
            raise PluginHostError("unavailable")
        owner_identity = None
        if config_row is not None:
            owner_identity = await self._facade.read_user_identity(config_row.owner_user_id)
        block_row = None
        block = None
        if config is not None and config_row is not None:
            block_row = await self._facade.get_record(
                AUTH_BLOCK_NAMESPACE, config.admin_credential_ref
            )
            if block_row is not None:
                block = _bad_record(actor, ProvisioningAuthBlockRecord, block_row.payload)
                if (
                    block_row.logical_key != config.admin_credential_ref
                    or block_row.owner_user_id != config_row.owner_user_id
                    or block.domain_id != config.domain_id
                    or block.credential_revision != config.admin_credential_revision
                ):
                    raise PluginHostError("unavailable")
        return (
            config_row,
            config,
            anchor_row,
            anchor,
            block_row,
            block,
            anchor_row.revision if anchor_row else 0,
            owner_identity,
        )

    async def _read_task_pair(
        self, actor: PluginActor, row: Any
    ) -> tuple[ProvisioningTaskRecord | None, Any | None]:
        if row is None:
            return None, None
        task = _bad_record(actor, ProvisioningTaskRecord, row.payload)
        if (
            row.logical_key != str(task.identity.user_id)
            or row.owner_user_id != task.identity.user_id
        ):
            raise _unavailable(actor)
        index_row = await self._facade.get_record(TASK_INDEX_NAMESPACE, task.task_id)
        if index_row is None:
            raise _unavailable(actor)
        index = _bad_record(actor, ProvisioningTaskIndexRecord, index_row.payload)
        if (
            index_row.logical_key != task.task_id
            or index_row.owner_user_id != task.identity.user_id
            or index.local_user_id != task.identity.user_id
        ):
            raise _unavailable(actor)
        return task, index_row

    async def _read_task_proofs(
        self, actor: PluginActor, task: ProvisioningTaskRecord | None
    ) -> list[tuple[Any, ProvisioningIntentRecord, Any | None, ProvisioningReceiptRecord | None]]:
        if task is None:
            return []
        found: list[
            tuple[Any, ProvisioningIntentRecord, Any | None, ProvisioningReceiptRecord | None]
        ] = []
        for field, kind in (
            ("user_intent_id", "USER"),
            ("kb_intent_id", "KB"),
            ("key_intent_id", "KEY"),
        ):
            intent_id = getattr(task, field)
            if intent_id is None:
                continue
            row = await self._facade.get_retained_record(INTENT_NAMESPACE, intent_id)
            if row is None or row.logical_key != intent_id:
                raise _unavailable(actor)
            intent = _bad_record(actor, ProvisioningIntentRecord, row.payload)
            if (
                intent.domain_id != task.domain_id
                or intent.local_user_id != task.identity.user_id
                or intent.task_id != task.task_id
                or intent.resource_kind != kind
            ):
                raise _unavailable(actor)
            receipt_row = None
            receipt = None
            if intent.receipt_id is not None:
                receipt_row = await self._facade.get_retained_record(
                    RECEIPT_NAMESPACE, intent.receipt_id
                )
                if receipt_row is not None:
                    receipt = _bad_record(actor, ProvisioningReceiptRecord, receipt_row.payload)
                    if (
                        receipt_row.logical_key != intent.receipt_id
                        or receipt.intent_id != intent_id
                        or receipt.domain_id != intent.domain_id
                        or receipt.local_user_id != intent.local_user_id
                        or receipt.task_id != intent.task_id
                        or receipt.resource_kind != intent.resource_kind
                    ):
                        raise _unavailable(actor)
            found.append((row, intent, receipt_row, receipt))
        if task.checkpoint in {"KEY_STORED", "READY"}:
            key_proof = next((entry for entry in found if entry[1].resource_kind == "KEY"), None)
            if (
                key_proof is None
                or key_proof[1].phase != "VERIFIED"
                or key_proof[1].receipt_id is None
                or key_proof[2] is None
                or key_proof[3] is None
            ):
                raise _unavailable(actor)
        for _, intent, _, _ in found:
            if intent.resource_kind == "USER" and intent.parent_intent_id is not None:
                raise _unavailable(actor)
            if intent.resource_kind == "KB":
                parent = next(
                    (entry[1] for entry in found if entry[1].resource_kind == "USER"), None
                )
                if (
                    parent is None
                    or intent.parent_intent_id != task.user_intent_id
                    or intent.remote_user_id != parent.remote_user_id
                    or intent.tenant_id != parent.tenant_id
                ):
                    raise _unavailable(actor)
            if intent.resource_kind == "KEY":
                parent = next((entry[1] for entry in found if entry[1].resource_kind == "KB"), None)
                if (
                    parent is None
                    or intent.parent_intent_id != task.kb_intent_id
                    or intent.remote_user_id != parent.remote_user_id
                    or intent.tenant_id != parent.tenant_id
                    or intent.kb_id != parent.kb_id
                ):
                    raise _unavailable(actor)
        for _, intent, _receipt_row, receipt in found:
            if intent.remote_user_id is not None and task.remote_user_id != intent.remote_user_id:
                raise _unavailable(actor)
            if intent.tenant_id is not None and task.tenant_id != intent.tenant_id:
                raise _unavailable(actor)
            if intent.kb_id is not None and task.kb_id != intent.kb_id:
                raise _unavailable(actor)
            if receipt is not None:
                expected: tuple[Any, ...] = (task.remote_user_id, task.tenant_id)
                actual: tuple[Any, ...] = (receipt.remote_user_id, receipt.tenant_id)
                if intent.resource_kind in {"KB", "KEY"}:
                    expected += (task.kb_id,)
                    actual += (receipt.kb_id,)
                if intent.resource_kind == "KEY":
                    expected += (task.key_id,)
                    actual += (receipt.key_id,)
                if (
                    expected != actual
                    or intent.remote_user_id not in {None, receipt.remote_user_id}
                    or intent.tenant_id not in {None, receipt.tenant_id}
                    or intent.kb_id not in {None, receipt.kb_id}
                ):
                    raise _unavailable(actor)
        return found

    def _project(
        self,
        actor: PluginActor,
        current_identity: Any,
        task_row: Any,
        task: ProvisioningTaskRecord | None,
        context: tuple[Any, Any, Any, Any, Any, Any, int, Any | None],
        intents: list[
            tuple[Any, ProvisioningIntentRecord, Any | None, ProvisioningReceiptRecord | None]
        ],
    ) -> ProvisioningStatus:
        (
            config_row,
            config,
            anchor_row,
            anchor,
            block_row,
            block,
            _anchor_revision,
            _owner_identity,
        ) = context
        revision = task_row.revision if task_row is not None else 0
        if current_identity.disabled:
            return self._status(revision, "SUSPENDED", "USER_DISABLED", task, False)
        if task is not None and (
            task.identity.user_id != current_identity.user_id or task.identity.disabled
        ):
            raise _unavailable(actor)
        has_intent = bool(intents)
        if task is not None and has_intent and task.identity.email != current_identity.email:
            return self._status(revision, "IDENTITY_CONFLICT", "IDENTITY_CONFLICT", task, False)
        if (
            task is not None
            and task.bootstrap_expires_at is not None
            and task.state != "READY"
            and _as_utc(task.bootstrap_expires_at) <= datetime.now(UTC)
        ):
            return self._status(revision, "MANUAL_REVIEW", "BOOTSTRAP_EXPIRED", task, False)
        if (
            task is not None
            and anchor is not None
            and (
                (task.domain_id is not None and task.domain_id != anchor.domain_id)
                or (task.base_url is not None and task.base_url != anchor.canonical_base_url)
            )
        ):
            raise _unavailable(actor)
        for _, intent, receipt_row, _receipt in intents:
            if intent.phase in {"WRITE_PENDING", "UNKNOWN"}:
                return self._status(
                    revision, "MANUAL_REVIEW", "REMOTE_OUTCOME_UNKNOWN", task, False
                )
            if intent.phase == "VERIFIED" and (intent.receipt_id is None or receipt_row is None):
                return self._status(
                    revision, "MANUAL_REVIEW", "REMOTE_OUTCOME_UNKNOWN", task, False
                )
            if intent.phase == "PREPARED" and (
                intent.absence_proof != "NOT_SENT"
                or intent.receipt_id is not None
                or receipt_row is not None
            ):
                return self._status(
                    revision, "MANUAL_REVIEW", "REMOTE_OUTCOME_UNKNOWN", task, False
                )
            if intent.phase == "REJECTED" and (
                intent.absence_proof != "DEFINITE_NO_EFFECT"
                or intent.receipt_id is not None
                or receipt_row is not None
            ):
                return self._status(
                    revision, "MANUAL_REVIEW", "REMOTE_OUTCOME_UNKNOWN", task, False
                )
        if current_identity.email is None:
            return self._status(revision, "EMAIL_REQUIRED", "EMAIL_REQUIRED", task, False)
        if config is None or config_row is None or not config.enabled:
            return self._status(revision, "CONFIG_REQUIRED", "CONFIG_REQUIRED", task, False)
        if anchor is None or anchor_row is None:
            raise _unavailable(actor)
        if block is not None and block_row is not None:
            return self._status(revision, "CONFIG_REQUIRED", "CONFIG_REQUIRED", task, False)
        if task is None:
            return self._status(0, "PENDING", None, None, True)
        if task.attempt is not None and task.attempt.finished_at is None:
            return self._status(revision, task.state, task.error_code, task, False)
        if task.state == "READY" and task.checkpoint == "READY":
            return self._status(revision, "READY", None, task, False)
        if task.state in {"PENDING", "PROVISIONING"}:
            return self._status(revision, task.state, task.error_code, task, False)
        if task.state in {"MANUAL_REVIEW", "IDENTITY_CONFLICT", "SUSPENDED", "READY"}:
            return self._status(revision, task.state, task.error_code, task, False)
        if task.state == "EMAIL_REQUIRED":
            return self._status(revision, "PENDING", None, task, True)
        return self._status(revision, task.state, task.error_code, task, True)

    @staticmethod
    def _status(
        revision: int,
        state: ProvisioningState,
        error: ProvisioningErrorCode | None,
        task: ProvisioningTaskRecord | None,
        can_retry: bool,
    ) -> ProvisioningStatus:
        return ProvisioningStatus(
            revision=revision,
            state=state,
            error_code=error,
            updated_at=task.updated_at if task else None,
            last_attempt_at=task.last_attempt_at if task else None,
            next_retry_at=task.next_retry_at if task else None,
            can_retry=can_retry,
        )

    @staticmethod
    def _is_idempotent_winner(
        task: ProvisioningTaskRecord,
        status: ProvisioningStatus,
        identity: Any,
        context: tuple[Any, Any, Any, Any, Any, Any, int, Any | None],
    ) -> bool:
        if task.state == "PENDING":
            return status.state == "PENDING"
        attempt = task.attempt
        config_row, config, anchor_row, anchor = context[0], context[1], context[2], context[3]
        if (
            task.state != "PROVISIONING"
            or status.state != "PROVISIONING"
            or attempt is None
            or attempt.finished_at is not None
            or config_row is None
            or config is None
            or anchor_row is None
            or anchor is None
            or context[7] is None
        ):
            return False
        now = datetime.now(UTC)
        started_at = _as_utc(attempt.started_at)
        if (
            started_at > now
            or _as_utc(attempt.deadline_at) <= started_at
            or _as_utc(attempt.lease_expires_at) <= started_at
            or _as_utc(attempt.deadline_at) <= now
            or _as_utc(attempt.lease_expires_at) <= now
            or _identity_record(identity) != attempt.employee
            or task.identity != attempt.employee
        ):
            return False
        current_pin = ProvisioningConfigPin(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            owner_user_id=config_row.owner_user_id,
            config_revision=config_row.revision,
            admin_credential_ref=config.admin_credential_ref,
            admin_credential_revision=config.admin_credential_revision,
            domain_id=config.domain_id,
            base_url=config.base_url,
            anchor_revision=anchor_row.revision,
        )
        return attempt.config == current_pin

    async def _confirm_retry_output(
        self,
        actor: PluginActor,
        identity: Any,
        kind: Literal["SELF", "ADMIN"],
        status: ProvisioningStatus,
    ) -> ProvisioningStatus:
        permission = "admin" if kind == "ADMIN" else "authenticated"
        fresh = await self._facade.reauthorize_actor(actor, permission)
        current_identity = await self._facade.read_user_identity(identity.user_id)
        if current_identity.snapshot_token != identity.snapshot_token:
            raise _unavailable(fresh)
        return status

    async def _read_retry_winner(
        self,
        actor: PluginActor,
        user_id: int,
        expected_task_id: str | None,
        prior_identity: Any,
        prior_context: tuple[Any, ...],
        kind: Literal["SELF", "ADMIN"],
    ) -> ProvisioningStatus | None:
        permission = "admin" if kind == "ADMIN" else "authenticated"
        fresh = await self._facade.reauthorize_actor(actor, permission)
        context = await self._read_config(fresh)
        if not self._same_context(prior_context, context):
            return None
        identity = await self._facade.read_user_identity(user_id)
        if identity.snapshot_token != prior_identity.snapshot_token:
            return None
        row = await self._facade.get_record(TASK_NAMESPACE, str(user_id))
        if row is None:
            return None
        task, _index = await self._read_task_pair(fresh, row)
        if task is None or (expected_task_id is not None and task.task_id != expected_task_id):
            return None
        proofs = await self._read_task_proofs(fresh, task)
        status = self._project(fresh, identity, row, task, context, proofs)
        if not self._is_idempotent_winner(task, status, identity, context):
            return None
        return await self._confirm_retry_output(actor, identity, kind, status)

    async def _recover_exact_retry_write(
        self,
        actor: PluginActor,
        user_id: int,
        prior_identity: Any,
        prior_context: tuple[Any, ...],
        prior_index: Any | None,
        expected_task: ProvisioningTaskRecord,
        expected_revision: int,
        kind: Literal["SELF", "ADMIN"],
    ) -> ProvisioningStatus | None:
        permission = "admin" if kind == "ADMIN" else "authenticated"
        fresh = await self._facade.reauthorize_actor(actor, permission)
        context = await self._read_config(fresh)
        if not self._same_context(prior_context, context):
            return None
        identity = await self._facade.read_user_identity(user_id)
        if identity.snapshot_token != prior_identity.snapshot_token:
            return None
        row = await self._facade.get_record(TASK_NAMESPACE, str(user_id))
        if (
            row is None
            or row.revision != expected_revision + 1
            or row.owner_user_id != user_id
            or row.logical_key != str(user_id)
            or row.payload != _payload(expected_task)
        ):
            return None
        task, index = await self._read_task_pair(fresh, row)
        if task != expected_task or index is None:
            return None
        if prior_index is not None:
            if not self._same_row(index, prior_index):
                return None
        elif (
            index.revision != 1
            or index.owner_user_id != user_id
            or index.logical_key != expected_task.task_id
            or index.payload.get("local_user_id") != user_id
        ):
            return None
        proofs = await self._read_task_proofs(fresh, task)
        status = self._project(fresh, identity, row, task, context, proofs)
        return await self._confirm_retry_output(actor, identity, kind, status)

    async def _retry(
        self,
        actor: PluginActor,
        identity: Any,
        task_row: Any,
        task: ProvisioningTaskRecord | None,
        index_row: Any | None,
        context: tuple[Any, Any, Any, Any, Any, Any, int, Any | None],
        intents: list[
            tuple[Any, ProvisioningIntentRecord, Any | None, ProvisioningReceiptRecord | None]
        ],
        expected_revision: int,
        *,
        kind: Literal["SELF", "ADMIN"],
        reason: str | None,
    ) -> ProvisioningStatus:
        current = self._project(actor, identity, task_row, task, context, intents)
        revision = task_row.revision if task_row is not None else 0
        if task is not None and self._is_idempotent_winner(task, current, identity, context):
            return await self._confirm_retry_output(actor, identity, kind, current)
        if expected_revision != revision:
            raise _error(actor, "PROVISIONING_CONFLICT", 409)
        if task is not None and not current.can_retry:
            return await self._confirm_retry_output(actor, identity, kind, current)
        if identity.disabled:
            return await self._confirm_retry_output(actor, identity, kind, current)
        if task is None and kind == "ADMIN":
            raise _error(actor, "PROVISIONING_NOT_FOUND", 404)
        now = _now()
        audit = ProvisioningRetryAudit(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            actor_user_id=actor.user_id,
            kind=kind,
            requested_at=now,
            reason=reason,
        )
        if task is None:
            new_task = ProvisioningTaskRecord(
                schema_id="octop.enterprise.provisioning.v1",
                version=1,
                task_id=str(uuid4()),
                identity=_identity_record(identity),
                state=current.state,
                error_code=current.error_code,
                checkpoint="NONE",
                domain_id=None,
                base_url=None,
                user_intent_id=None,
                kb_intent_id=None,
                key_intent_id=None,
                bootstrap_ref=None,
                bootstrap_revision=None,
                bootstrap_created_at=None,
                bootstrap_expires_at=None,
                final_key_ref=None,
                final_key_revision=None,
                remote_user_id=None,
                tenant_id=None,
                kb_id=None,
                key_id=None,
                attempt=None,
                consecutive_failures=0,
                updated_at=now,
                last_attempt_at=None,
                next_retry_at=None,
                last_retry=audit,
            )
            writes = [
                RecordWrite(
                    TASK_NAMESPACE, str(identity.user_id), identity.user_id, _payload(new_task), 0
                ),
                RecordWrite(
                    TASK_INDEX_NAMESPACE,
                    new_task.task_id,
                    identity.user_id,
                    {
                        "schema_id": "octop.enterprise.provisioning.v1",
                        "version": 1,
                        "local_user_id": identity.user_id,
                    },
                    0,
                ),
            ]
        else:
            new_task = ProvisioningTaskRecord.model_validate(
                {
                    **_payload(task),
                    "identity": _identity_record(identity) if not intents else task.identity,
                    "state": "PENDING",
                    "error_code": None,
                    "consecutive_failures": 0,
                    "next_retry_at": None,
                    "updated_at": now,
                    "last_retry": audit,
                }
            )
            writes = [
                RecordWrite(
                    TASK_NAMESPACE,
                    str(identity.user_id),
                    identity.user_id,
                    _payload(new_task),
                    revision,
                )
            ]
        fresh = await self._facade.reauthorize_actor(
            actor, "admin" if kind == "ADMIN" else "authenticated"
        )
        latest_context = await self._read_config(fresh)
        if not self._same_context(context, latest_context):
            raise _error(fresh, "PROVISIONING_CONFLICT", 409)
        latest_task_row = await self._facade.get_record(TASK_NAMESPACE, str(identity.user_id))
        changed_before_commit = (task_row is None and latest_task_row is not None) or (
            task_row is not None
            and (
                latest_task_row is None
                or latest_task_row.revision != task_row.revision
                or latest_task_row.payload != task_row.payload
            )
        )
        if changed_before_commit:
            winner = await self._read_retry_winner(
                actor,
                identity.user_id,
                task.task_id if task is not None else None,
                identity,
                context,
                kind,
            )
            if winner is not None:
                return winner
            raise _error(fresh, "PROVISIONING_CONFLICT", 409)
        checks = [
            RecordCheck(
                CONFIG_NAMESPACE, PRIMARY_KEY, context[0].revision if context[0] is not None else 0
            )
        ]
        if task is not None and index_row is not None:
            checks.append(RecordCheck(TASK_INDEX_NAMESPACE, task.task_id, index_row.revision))
        if context[1] is not None:
            checks.append(
                RecordCheck(
                    AUTH_BLOCK_NAMESPACE,
                    context[1].admin_credential_ref,
                    context[4].revision if context[4] is not None else 0,
                )
            )
        retained_checks = [RetainedRecordCheck(DOMAIN_NAMESPACE, PRIMARY_KEY, context[6])]
        for intent_row, _intent, receipt_row, _receipt in intents:
            retained_checks.append(
                RetainedRecordCheck(INTENT_NAMESPACE, intent_row.logical_key, intent_row.revision)
            )
            if _intent.receipt_id is not None:
                retained_checks.append(
                    RetainedRecordCheck(
                        RECEIPT_NAMESPACE,
                        _intent.receipt_id,
                        receipt_row.revision if receipt_row is not None else 0,
                    )
                )
        identities: dict[int, Any] = {}
        for current_identity in (
            identity,
            await self._facade.read_user_identity(fresh.user_id),
            context[7],
        ):
            if current_identity is None:
                continue
            prior = identities.get(current_identity.user_id)
            if prior is not None and prior.snapshot_token != current_identity.snapshot_token:
                raise _error(fresh, "PROVISIONING_CONFLICT", 409)
            identities[current_identity.user_id] = current_identity
        final_projection = self._project(fresh, identity, task_row, task, context, intents)
        if final_projection.error_code == "BOOTSTRAP_EXPIRED":
            return await self._confirm_retry_output(actor, identity, kind, final_projection)
        identity_checks = [UserIdentityCheck(value) for value in identities.values()]
        result, error, cancelled = await ProvisioningService(self._facade)._settle(
            self._facade.commit(
                writes,
                checks=checks,
                identity_checks=identity_checks,
                retained_checks=retained_checks,
            )
        )
        if cancelled:
            raise __import__("asyncio").CancelledError
        if error is not None:
            if isinstance(error, PluginHostError):
                if error.code == "conflict":
                    winner = await self._read_retry_winner(
                        actor,
                        identity.user_id,
                        task.task_id if task is not None else None,
                        identity,
                        context,
                        kind,
                    )
                    if winner is not None:
                        return winner
                if error.code != "unavailable":
                    raise _host_error(error, fresh) from None
            recovered = await self._recover_exact_retry_write(
                actor,
                identity.user_id,
                identity,
                context,
                index_row,
                new_task,
                revision,
                kind,
            )
            if recovered is None:
                raise _unavailable(fresh)
            return recovered
        return await self._confirm_retry_output(
            actor,
            identity,
            kind,
            self._status(revision + 1, new_task.state, new_task.error_code, new_task, False),
        )

    @staticmethod
    def _same_row(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return left is right
        return (
            left.logical_key == right.logical_key
            and left.revision == right.revision
            and left.payload == right.payload
            and getattr(left, "owner_user_id", None) == getattr(right, "owner_user_id", None)
        )

    @classmethod
    def _same_context(cls, left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
        owners_match = (left[7] is None and right[7] is None) or (
            left[7] is not None
            and right[7] is not None
            and left[7].snapshot_token == right[7].snapshot_token
        )
        return (
            cls._same_row(left[0], right[0])
            and cls._same_row(left[2], right[2])
            and cls._same_row(left[4], right[4])
            and owners_match
        )
