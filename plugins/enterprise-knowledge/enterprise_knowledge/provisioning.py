"""Local configuration and retained summaries for user provisioning."""

from __future__ import annotations

import asyncio
import hmac
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import StrictStr, field_validator

from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
    RetainedRecordCheck,
    RetainedRecordWrite,
    UserIdentityCheck,
)

from .i18n import error_message
from .models import StrictDto
from .provisioning_models import (
    ProvisioningConfig,
    ProvisioningConfigSaveInput,
    ProvisioningRetainedListInput,
    ProvisioningRetainedListOutput,
    ProvisioningRetainedSummary,
    _canonical_uuid4,
)
from .provisioning_records import (
    ProvisioningAuthBlockRecord,
    ProvisioningConfigPreparationRecord,
    ProvisioningConfigRecord,
    ProvisioningDomainRecord,
    ProvisioningIntentHeader,
    ProvisioningReceiptHeader,
)

CONFIG_NAMESPACE = "provision_config"
DOMAIN_NAMESPACE = "provision_domains"
PREPARATION_NAMESPACE = "provision_config_preparations"
AUTH_BLOCK_NAMESPACE = "provision_auth_blocks"
INTENT_NAMESPACE = "provision_intents"
RECEIPT_NAMESPACE = "provision_receipts"
PRIMARY_KEY = "primary"


class _AdminCredentialPayload(StrictDto):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: Literal[1]
    system_admin_jwt: StrictStr

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("invalid credential payload")
        return value

    @field_validator("system_admin_jwt")
    @classmethod
    def nonempty_secret(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("invalid credential payload")
        return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _payload(model: Any) -> dict[str, Any]:
    return cast(dict[str, Any], model.model_dump(mode="python"))


def _bad_record(actor: PluginActor, model: Any, payload: dict[str, Any]) -> Any:
    try:
        return model.model_validate(payload)
    except Exception:
        raise _unavailable(actor) from None


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    locale = actor.locale if isinstance(actor, PluginActor) else "en"
    return PluginActionError(code, error_message(code, locale), status=status)


def _host_error(exc: PluginHostError, actor: PluginActor) -> PluginActionError:
    if exc.code == "conflict":
        return _error(actor, "PROVISIONING_CONFLICT", 409)
    if exc.code == "denied":
        return _error(actor, "ACCESS_DENIED", 403)
    if exc.code == "invalid":
        return _error(actor, "PROVISIONING_INVALID", 400)
    return _error(actor, "PROVISIONING_UNAVAILABLE", 409)


def _unavailable(actor: PluginActor) -> PluginActionError:
    return _error(actor, "PROVISIONING_UNAVAILABLE", 409)


class ProvisioningService:
    def __init__(self, facade: Any) -> None:
        self._facade = facade

    async def get_config(self, actor: PluginActor) -> ProvisioningConfig:
        try:
            fresh = await self._facade.reauthorize_actor(actor, "admin")
            anchor_row = await self._facade.get_retained_record(DOMAIN_NAMESPACE, PRIMARY_KEY)
            config_row = await self._facade.get_record(CONFIG_NAMESPACE, PRIMARY_KEY)
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        try:
            anchor = (
                ProvisioningDomainRecord.model_validate(anchor_row.payload)
                if anchor_row is not None
                else None
            )
            config = (
                ProvisioningConfigRecord.model_validate(config_row.payload)
                if config_row is not None
                else None
            )
        except Exception:
            raise _unavailable(fresh) from None
        if config is not None and (
            anchor is None
            or config.domain_id != anchor.domain_id
            or config.base_url != anchor.canonical_base_url
        ):
            raise _unavailable(fresh)
        credential_configured = False
        if config is not None and config_row is not None:
            try:
                raw_credential = await self._facade.resolve_credential(
                    config.admin_credential_ref, config_row.owner_user_id
                )
            except PluginHostError as exc:
                if exc.code != "not_found":
                    raise _host_error(exc, fresh) from None
            else:
                _bad_record(fresh, _AdminCredentialPayload, raw_credential)
                credential_configured = True
        try:
            await self._facade.reauthorize_actor(fresh, "admin")
        except PluginHostError as exc:
            raise _host_error(exc, fresh) from None
        if config is None:
            return ProvisioningConfig(
                revision=0,
                anchor_revision=anchor_row.revision if anchor_row is not None else 0,
                name=None,
                base_url=anchor.canonical_base_url if anchor is not None else None,
                enabled=False,
                credential_configured=False,
                updated_at=None,
            )
        return ProvisioningConfig(
            revision=config_row.revision,
            anchor_revision=anchor_row.revision,
            name=config.name,
            base_url=config.base_url,
            enabled=config.enabled,
            credential_configured=credential_configured,
            updated_at=config.updated_at,
        )

    async def save_config(
        self, actor: PluginActor, request: ProvisioningConfigSaveInput
    ) -> ProvisioningConfig:
        if not isinstance(actor, PluginActor) or not isinstance(
            request, ProvisioningConfigSaveInput
        ):
            raise _error(actor, "PROVISIONING_INVALID", 400)
        fresh = await self._fresh_admin(actor)
        try:
            anchor_row = await self._facade.get_retained_record(DOMAIN_NAMESPACE, PRIMARY_KEY)
            config_row = await self._facade.get_record(CONFIG_NAMESPACE, PRIMARY_KEY)
        except PluginHostError as exc:
            raise _host_error(exc, fresh) from None
        anchor = (
            _bad_record(fresh, ProvisioningDomainRecord, anchor_row.payload)
            if anchor_row is not None
            else None
        )
        config = (
            _bad_record(fresh, ProvisioningConfigRecord, config_row.payload)
            if config_row is not None
            else None
        )
        if config is not None and (
            anchor is None
            or config.domain_id != anchor.domain_id
            or config.base_url != anchor.canonical_base_url
        ):
            raise _unavailable(fresh)
        config_revision = config_row.revision if config_row is not None else 0
        anchor_revision = anchor_row.revision if anchor_row is not None else 0
        if (
            request.expected_revision != config_revision
            or request.expected_anchor_revision != anchor_revision
        ):
            raise _error(fresh, "PROVISIONING_CONFLICT", 409)
        if anchor is not None and request.base_url != anchor.canonical_base_url:
            raise _error(fresh, "PROVISIONING_INVALID", 400)

        owner_id = config_row.owner_user_id if config_row is not None else fresh.user_id
        previous_ref = config.admin_credential_ref if config is not None else None
        block_revision = 0
        same_material = False
        if config is not None:
            try:
                old_raw = await self._facade.resolve_credential(previous_ref, owner_id)
                block_row = await self._facade.get_record(AUTH_BLOCK_NAMESPACE, previous_ref)
            except PluginHostError:
                raise _unavailable(fresh) from None
            old_secret = _bad_record(fresh, _AdminCredentialPayload, old_raw).system_admin_jwt
            if block_row is not None:
                block = _bad_record(fresh, ProvisioningAuthBlockRecord, block_row.payload)
                if block_row.owner_user_id != owner_id or block.domain_id != config.domain_id:
                    raise _unavailable(fresh)
                block_revision = block_row.revision
            provided = (
                request.system_admin_jwt.get_secret_value() if request.system_admin_jwt else None
            )
            same_material = provided is None or hmac.compare_digest(
                provided.encode("utf-8"), old_secret.encode("utf-8")
            )
        elif request.system_admin_jwt is None:
            raise _error(fresh, "PROVISIONING_INVALID", 400)

        if (
            same_material
            and config is not None
            and config_row is not None
            and anchor_row is not None
        ):
            return await self._save_existing(
                actor, fresh, request, config_row, config, anchor_row, owner_id, block_revision
            )
        return await self._rotate(
            actor, fresh, request, anchor_row, anchor, config_row, config, owner_id, block_revision
        )

    async def _fresh_admin(self, actor: PluginActor) -> PluginActor:
        try:
            return await self._facade.reauthorize_actor(actor, "admin")
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

    async def _identity_checks(self, owner_id: int, actor: PluginActor) -> list[UserIdentityCheck]:
        try:
            return [
                UserIdentityCheck(await self._facade.read_user_identity(user_id))
                for user_id in sorted({owner_id, actor.user_id})
            ]
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

    async def _save_existing(
        self,
        original_actor: PluginActor,
        actor: PluginActor,
        request: ProvisioningConfigSaveInput,
        config_row: Any,
        config: Any,
        anchor_row: Any,
        owner_id: int,
        block_revision: int,
    ) -> ProvisioningConfig:
        now = _now()
        candidate = ProvisioningConfigRecord(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            domain_id=config.domain_id,
            name=request.name,
            base_url=request.base_url,
            enabled=request.enabled,
            admin_credential_ref=config.admin_credential_ref,
            admin_credential_revision=2,
            updated_at=now,
        )
        actor = await self._fresh_admin(original_actor)
        identities = await self._identity_checks(owner_id, actor)
        try:
            await self._facade.commit(
                [
                    RecordWrite(
                        CONFIG_NAMESPACE,
                        PRIMARY_KEY,
                        owner_id,
                        _payload(candidate),
                        request.expected_revision,
                    )
                ],
                checks=[
                    RecordCheck(AUTH_BLOCK_NAMESPACE, config.admin_credential_ref, block_revision)
                ],
                identity_checks=identities,
                retained_checks=[
                    RetainedRecordCheck(DOMAIN_NAMESPACE, PRIMARY_KEY, anchor_row.revision)
                ],
            )
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        return ProvisioningConfig(
            revision=config_row.revision + 1,
            anchor_revision=anchor_row.revision,
            name=candidate.name,
            base_url=candidate.base_url,
            enabled=candidate.enabled,
            credential_configured=True,
            updated_at=now,
        )

    async def _rotate(
        self,
        original_actor: PluginActor,
        actor: PluginActor,
        request: ProvisioningConfigSaveInput,
        anchor_row: Any,
        anchor: Any,
        config_row: Any,
        config: Any,
        owner_id: int,
        block_revision: int,
    ) -> ProvisioningConfig:
        if request.system_admin_jwt is None:
            raise _error(actor, "PROVISIONING_INVALID", 400)
        operation_id, planned_ref = str(uuid4()), str(uuid4())
        domain_id = anchor.domain_id if anchor is not None else str(uuid4())
        previous_ref = config.admin_credential_ref if config is not None else None
        created = _now()
        prep = ProvisioningConfigPreparationRecord(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            domain_id=domain_id,
            expected_config_revision=request.expected_revision,
            expected_anchor_revision=request.expected_anchor_revision,
            previous_credential_ref=previous_ref,
            previous_credential_revision=2 if previous_ref is not None else None,
            planned_credential_ref=planned_ref,
            planned_active_revision=2,
            state="PREPARING",
            staged_cleanup="PENDING",
            created_at=created,
            updated_at=created,
        )
        actor = await self._fresh_admin(original_actor)
        identities = await self._identity_checks(owner_id, actor)
        checks = [RecordCheck(CONFIG_NAMESPACE, PRIMARY_KEY, request.expected_revision)]
        if previous_ref is not None:
            checks.append(RecordCheck(AUTH_BLOCK_NAMESPACE, previous_ref, block_revision))
        prepared, prepare_error, prepare_cancelled = await self._settle(
            self._facade.commit(
                [RecordWrite(PREPARATION_NAMESPACE, operation_id, owner_id, _payload(prep), 0)],
                checks=checks,
                identity_checks=identities,
                retained_checks=[
                    RetainedRecordCheck(
                        DOMAIN_NAMESPACE,
                        PRIMARY_KEY,
                        anchor_row.revision if anchor_row is not None else 0,
                    )
                ],
            )
        )
        if prepare_cancelled:
            raise asyncio.CancelledError
        if prepare_error is not None:
            if isinstance(prepare_error, PluginHostError):
                raise _host_error(prepare_error, actor) from None
            raise _unavailable(actor)
        if prepared is None:
            raise _unavailable(actor)
        prep_revision = prepared[0].revision
        stage_result, stage_error, stage_cancelled = await self._settle(
            self._facade.stage_credential(
                owner_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "system_admin_jwt": request.system_admin_jwt.get_secret_value(),
                },
                credential_id=planned_ref,
            )
        )
        if stage_error is not None or stage_cancelled or stage_result != planned_ref:
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            if stage_cancelled:
                raise asyncio.CancelledError
            if isinstance(stage_error, PluginHostError):
                raise _host_error(stage_error, actor) from None
            raise _unavailable(actor)

        try:
            held_row = await self._facade.get_record(PREPARATION_NAMESPACE, operation_id)
        except PluginHostError as exc:
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            raise _host_error(exc, actor) from None
        if (
            held_row is None
            or held_row.owner_user_id != owner_id
            or held_row.revision != prep_revision
        ):
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            raise _unavailable(actor)
        try:
            held_prep = _bad_record(actor, ProvisioningConfigPreparationRecord, held_row.payload)
        except PluginActionError:
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            raise
        if (
            held_prep.state != "PREPARING"
            or held_prep.planned_credential_ref != planned_ref
            or held_prep != prep
        ):
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            raise _unavailable(actor)

        committed = ProvisioningConfigRecord(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            domain_id=domain_id,
            name=request.name,
            base_url=request.base_url,
            enabled=request.enabled,
            admin_credential_ref=planned_ref,
            admin_credential_revision=2,
            updated_at=_now(),
        )
        completed_prep = held_prep.model_copy(
            update={"state": "COMMITTED", "staged_cleanup": "NOT_REQUIRED", "updated_at": _now()}
        )
        try:
            actor = await self._fresh_admin(original_actor)
            identities = await self._identity_checks(owner_id, actor)
        except PluginActionError:
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            raise
        writes = [
            RecordWrite(
                CONFIG_NAMESPACE,
                PRIMARY_KEY,
                owner_id,
                _payload(committed),
                request.expected_revision,
            ),
            RecordWrite(
                PREPARATION_NAMESPACE,
                operation_id,
                owner_id,
                _payload(completed_prep),
                prep_revision,
            ),
        ]
        checks = [RecordCheck(AUTH_BLOCK_NAMESPACE, planned_ref, 0)]
        if previous_ref is not None:
            checks.append(RecordCheck(AUTH_BLOCK_NAMESPACE, previous_ref, block_revision))
        retained_writes: list[RetainedRecordWrite] = []
        retained_checks: list[RetainedRecordCheck] = []
        if anchor_row is None:
            retained_writes.append(
                RetainedRecordWrite(
                    DOMAIN_NAMESPACE,
                    PRIMARY_KEY,
                    _payload(
                        ProvisioningDomainRecord(
                            schema_id="octop.enterprise.provisioning.v1",
                            version=1,
                            domain_id=domain_id,
                            canonical_base_url=request.base_url,
                        )
                    ),
                    0,
                )
            )
            out_anchor_revision = 1
        else:
            retained_checks.append(
                RetainedRecordCheck(DOMAIN_NAMESPACE, PRIMARY_KEY, anchor_row.revision)
            )
            out_anchor_revision = anchor_row.revision
        deletions = (
            [CredentialDeletion(previous_ref, owner_id, 2)] if previous_ref is not None else []
        )
        _, commit_error, commit_cancelled = await self._settle(
            self._facade.commit(
                writes,
                [CredentialActivation(planned_ref, owner_id, 1)],
                credential_deletions=deletions,
                checks=checks,
                identity_checks=identities,
                retained_writes=retained_writes,
                retained_checks=retained_checks,
            )
        )
        if commit_error is not None:
            await self._abort_and_release(operation_id, prep_revision, planned_ref, owner_id)
            if commit_cancelled:
                raise asyncio.CancelledError
            if isinstance(commit_error, PluginHostError):
                raise _host_error(commit_error, actor) from None
            raise _unavailable(actor)
        if commit_cancelled:
            raise asyncio.CancelledError
        return ProvisioningConfig(
            revision=request.expected_revision + 1,
            anchor_revision=out_anchor_revision,
            name=committed.name,
            base_url=committed.base_url,
            enabled=committed.enabled,
            credential_configured=True,
            updated_at=committed.updated_at,
        )

    async def _settle(self, awaitable: Any) -> tuple[Any | None, BaseException | None, bool]:
        task = asyncio.create_task(awaitable)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            return task.result(), None, cancelled
        except BaseException as exc:
            return None, exc, cancelled

    async def _abort_and_release(
        self, operation_id: str, expected_revision: int, planned_ref: str, owner_id: int
    ) -> None:
        try:
            row = await self._facade.get_record(PREPARATION_NAMESPACE, operation_id)
            if row is None or row.owner_user_id != owner_id or row.revision != expected_revision:
                return
            prep = ProvisioningConfigPreparationRecord.model_validate(row.payload)
            if prep.state != "PREPARING" or prep.planned_credential_ref != planned_ref:
                return
            aborted = prep.model_copy(
                update={"state": "ABORTED", "staged_cleanup": "UNRESOLVED", "updated_at": _now()}
            )
            changed = await self._facade.commit(
                [
                    RecordWrite(
                        PREPARATION_NAMESPACE,
                        operation_id,
                        owner_id,
                        _payload(aborted),
                        expected_revision,
                    )
                ]
            )
            revision = changed[0].revision
            removed, error, cancelled = await self._settle(
                self._facade.release_staged_credential(planned_ref, owner_id)
            )
            if error is not None or cancelled or removed is not True:
                return
            released = aborted.model_copy(
                update={"staged_cleanup": "RELEASED", "updated_at": _now()}
            )
            await self._facade.commit(
                [
                    RecordWrite(
                        PREPARATION_NAMESPACE, operation_id, owner_id, _payload(released), revision
                    )
                ]
            )
        except BaseException:
            return

    async def list_retained(
        self, actor: PluginActor, request: ProvisioningRetainedListInput
    ) -> ProvisioningRetainedListOutput:
        if not isinstance(actor, PluginActor) or not isinstance(
            request, ProvisioningRetainedListInput
        ):
            raise _error(actor, "PROVISIONING_INVALID", 400)
        fresh = await self._fresh_admin(actor)
        try:
            page = await self._facade.list_retained_records(
                INTENT_NAMESPACE,
                "",
                after_key=request.after_key,
                limit=request.limit,
            )
        except PluginHostError as exc:
            raise _host_error(exc, fresh) from None
        summaries: list[ProvisioningRetainedSummary] = []
        for row in page.items:
            try:
                intent_id = _canonical_uuid4(row.logical_key)
            except Exception:
                raise _unavailable(fresh) from None
            if intent_id != row.logical_key:
                raise _unavailable(fresh)
            intent = self._retained_header(
                fresh,
                ProvisioningIntentHeader,
                row.payload,
                (
                    "schema_id",
                    "version",
                    "domain_id",
                    "local_user_id",
                    "resource_kind",
                    "phase",
                    "receipt_id",
                ),
            )
            receipt_header = None
            if intent.receipt_id is not None:
                try:
                    receipt = await self._facade.get_retained_record(
                        RECEIPT_NAMESPACE, intent.receipt_id
                    )
                except PluginHostError as exc:
                    raise _host_error(exc, fresh) from None
                if receipt is not None:
                    if receipt.logical_key != intent.receipt_id:
                        raise _unavailable(fresh)
                    receipt_header = self._retained_header(
                        fresh,
                        ProvisioningReceiptHeader,
                        receipt.payload,
                        (
                            "schema_id",
                            "version",
                            "intent_id",
                            "domain_id",
                            "local_user_id",
                            "resource_kind",
                            "proof_kind",
                        ),
                    )
                    if (
                        receipt_header.intent_id != intent_id
                        or receipt_header.domain_id != intent.domain_id
                        or receipt_header.local_user_id != intent.local_user_id
                        or receipt_header.resource_kind != intent.resource_kind
                    ):
                        raise _unavailable(fresh)
            has_receipt = receipt_header is not None
            review_required = intent.phase in {"PREPARED", "WRITE_PENDING", "UNKNOWN"} or (
                intent.phase == "VERIFIED" and not has_receipt
            )
            summaries.append(
                ProvisioningRetainedSummary(
                    intent_id=intent_id,
                    local_user_id=intent.local_user_id,
                    resource_kind=intent.resource_kind,
                    phase=intent.phase,
                    proof_kind=receipt_header.proof_kind if receipt_header is not None else None,
                    has_receipt=has_receipt,
                    review_required=review_required,
                    revision=row.revision,
                )
            )
        next_key = page.next_logical_key
        if next_key is not None:
            try:
                if _canonical_uuid4(next_key) != next_key:
                    raise ValueError
            except Exception:
                raise _unavailable(fresh) from None
        output = ProvisioningRetainedListOutput(items=summaries, next_key=next_key)
        await self._fresh_admin(fresh)
        return output

    @staticmethod
    def _retained_header(
        actor: PluginActor,
        model: Any,
        payload: dict[str, Any],
        fields: tuple[str, ...],
    ) -> Any:
        try:
            return model.model_validate(
                {field: payload[field] for field in fields if field in payload}
            )
        except Exception:
            raise _unavailable(actor) from None
