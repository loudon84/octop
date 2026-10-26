"""Strict internal records for local enterprise provisioning state."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from .models import normalize_base_url
from .provisioning_models import (
    _canonical_uuid4,
    _ProvisioningOutput,
    _trimmed_bounded,
    _utc_timestamp,
)

_Revision = Annotated[StrictInt, Field(ge=0)]
_PositiveUser = Annotated[StrictInt, Field(gt=0)]
_NonNegative = Annotated[StrictInt, Field(ge=0)]
_UInt64 = Annotated[StrictInt, Field(ge=1, le=18_446_744_073_709_551_615)]


class _ProvisioningInternal(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported provisioning record version")
        return value


class ProvisioningIdentity(_ProvisioningInternal):
    user_id: _PositiveUser
    email: StrictStr | None
    disabled: StrictBool
    snapshot_token: StrictStr

    @field_validator("snapshot_token")
    @classmethod
    def nonempty_snapshot(cls, value: str) -> str:
        if not value:
            raise ValueError("identity snapshot token is required")
        return value


class ProvisioningConfigPin(_ProvisioningInternal):
    owner_user_id: _PositiveUser
    config_revision: _PositiveUser
    admin_credential_ref: StrictStr
    admin_credential_revision: Literal[2]
    domain_id: StrictStr
    base_url: StrictStr
    anchor_revision: _PositiveUser

    @field_validator("admin_credential_revision", mode="before")
    @classmethod
    def strict_credential_revision(cls, value: object) -> int:
        if type(value) is not int or value != 2:
            raise ValueError("credential revision must be integer 2")
        return value

    @field_validator("admin_credential_ref", "domain_id")
    @classmethod
    def canonical_ids(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("base_url")
    @classmethod
    def canonical_url(cls, value: str) -> str:
        if normalize_base_url(value) != value:
            raise ValueError("base URL must be canonical")
        return value


class ProvisioningRetryAudit(_ProvisioningInternal):
    actor_user_id: _PositiveUser
    kind: Literal["SELF", "ADMIN"]
    requested_at: StrictStr
    reason: StrictStr | None

    @field_validator("requested_at")
    @classmethod
    def utc_requested_at(cls, value: str) -> str:
        _utc_timestamp(value)
        return value

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str | None) -> str | None:
        if value is not None and _trimmed_bounded(value, "reason", 500) != value:
            raise ValueError("reason must be trimmed")
        return value

    @model_validator(mode="after")
    def reason_matches_kind(self) -> Self:
        if (self.kind == "SELF") != (self.reason is None):
            raise ValueError("retry reason must match actor kind")
        return self


class ProvisioningAttempt(_ProvisioningInternal):
    attempt_id: StrictStr
    mode: Literal["ADVANCE", "RECONCILE", "VERIFY_KEY"]
    config: ProvisioningConfigPin
    employee: ProvisioningIdentity
    started_at: StrictStr
    deadline_at: StrictStr
    lease_expires_at: StrictStr
    finished_at: StrictStr | None

    @field_validator("attempt_id")
    @classmethod
    def canonical_attempt_id(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("started_at", "deadline_at", "lease_expires_at", "finished_at")
    @classmethod
    def utc_attempt_times(cls, value: str | None) -> str | None:
        return _utc_timestamp(value)


class ProvisioningTaskRecord(_ProvisioningInternal):
    task_id: StrictStr
    identity: ProvisioningIdentity
    state: Literal[
        "EMAIL_REQUIRED",
        "PENDING",
        "PROVISIONING",
        "RETRY_WAIT",
        "RETRY_REQUIRED",
        "CONFIG_REQUIRED",
        "IDENTITY_CONFLICT",
        "MANUAL_REVIEW",
        "READY",
        "SUSPENDED",
    ]
    error_code: (
        Literal[
            "EMAIL_REQUIRED",
            "CONFIG_REQUIRED",
            "IDENTITY_CONFLICT",
            "REMOTE_OUTCOME_UNKNOWN",
            "BOOTSTRAP_EXPIRED",
            "REMOTE_REJECTED",
            "REMOTE_UNAVAILABLE",
            "RETRIEVAL_FAILED",
            "RETRY_EXHAUSTED",
            "USER_DISABLED",
        ]
        | None
    )
    checkpoint: Literal[
        "NONE",
        "USER_CONFIRMED",
        "OWNER_CONFIRMED",
        "KB_CONFIRMED",
        "KEY_STORED",
        "READY",
    ]
    domain_id: StrictStr | None
    base_url: StrictStr | None
    user_intent_id: StrictStr | None
    kb_intent_id: StrictStr | None
    key_intent_id: StrictStr | None
    bootstrap_ref: StrictStr | None
    bootstrap_revision: Literal[2] | None
    bootstrap_created_at: StrictStr | None
    bootstrap_expires_at: StrictStr | None
    final_key_ref: StrictStr | None
    final_key_revision: Literal[2] | None
    remote_user_id: StrictStr | None
    tenant_id: _UInt64 | None
    kb_id: StrictStr | None
    key_id: _UInt64 | None
    attempt: ProvisioningAttempt | None
    consecutive_failures: _NonNegative
    updated_at: StrictStr
    last_attempt_at: StrictStr | None
    next_retry_at: StrictStr | None
    last_retry: ProvisioningRetryAudit | None

    @field_validator(
        "task_id",
        "user_intent_id",
        "kb_intent_id",
        "key_intent_id",
        "bootstrap_ref",
        "final_key_ref",
    )
    @classmethod
    def canonical_task_ids(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None

    @field_validator("domain_id")
    @classmethod
    def canonical_domain_id(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None

    @field_validator("base_url")
    @classmethod
    def canonical_base_url(cls, value: str | None) -> str | None:
        if value is not None and (not value or normalize_base_url(value) != value):
            raise ValueError("base URL must be nonempty and canonical")
        return value

    @field_validator("bootstrap_revision", "final_key_revision", mode="before")
    @classmethod
    def strict_revision_two(cls, value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value != 2:
            raise ValueError("credential revision must be integer 2")
        return value

    @field_validator("remote_user_id", "kb_id")
    @classmethod
    def remote_id_shape(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 36):
            raise ValueError("remote identifier must contain 1..36 characters")
        if value is not None:
            value.encode("utf-8", errors="strict")
        return value

    @field_validator(
        "updated_at",
        "last_attempt_at",
        "next_retry_at",
        "bootstrap_created_at",
        "bootstrap_expires_at",
    )
    @classmethod
    def utc_task_times(cls, value: str | None) -> str | None:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def consistent_task_refs_and_deadline(self) -> Self:
        if (self.bootstrap_ref is None) != (self.bootstrap_revision is None):
            raise ValueError("bootstrap reference and revision must be paired")
        if (self.final_key_ref is None) != (self.final_key_revision is None):
            raise ValueError("final key reference and revision must be paired")
        if (self.bootstrap_created_at is None) != (self.bootstrap_expires_at is None):
            raise ValueError("bootstrap timestamps must be paired")
        if (self.bootstrap_ref is None) != (self.bootstrap_created_at is None):
            raise ValueError("bootstrap reference and timestamps must be paired")
        if self.bootstrap_created_at is not None and self.bootstrap_expires_at is not None:
            created = datetime.fromisoformat(
                self.bootstrap_created_at[:-1] + "+00:00"
                if self.bootstrap_created_at.endswith("Z")
                else self.bootstrap_created_at
            )
            expires = datetime.fromisoformat(
                self.bootstrap_expires_at[:-1] + "+00:00"
                if self.bootstrap_expires_at.endswith("Z")
                else self.bootstrap_expires_at
            )
            if expires != created + timedelta(hours=24):
                raise ValueError("bootstrap expiry must be 24 hours after creation")
        if (self.domain_id is None) != (self.base_url is None):
            raise ValueError("task domain and base URL must be paired")
        if self.checkpoint == "READY" and self.state != "READY":
            raise ValueError("READY checkpoint requires READY state")
        if self.state == "READY" and self.checkpoint != "READY":
            raise ValueError("READY state requires READY checkpoint")
        if self.checkpoint in {"KEY_STORED", "READY"} and (
            self.final_key_ref is None
            or self.final_key_revision != 2
            or self.key_id is None
            or self.key_intent_id is None
        ):
            raise ValueError("stored key checkpoints require a durable key reference and IDs")
        return self


class ProvisioningTaskIndexRecord(_ProvisioningInternal):
    local_user_id: _PositiveUser


class ProvisioningCredentialPreparationRecord(_ProvisioningInternal):
    task_id: StrictStr
    intent_id: StrictStr
    purpose: Literal["BOOTSTRAP", "RETRIEVE"]
    attempt_id: StrictStr
    created_at: StrictStr
    expires_at: StrictStr | None
    state: Literal["PREPARING", "ACTIVE", "ABORTED", "DELETED"]
    staged_cleanup: Literal["PENDING", "NOT_REQUIRED", "RELEASED", "UNRESOLVED"]
    updated_at: StrictStr

    @field_validator("task_id", "intent_id", "attempt_id")
    @classmethod
    def canonical_refs(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("created_at", "expires_at", "updated_at")
    @classmethod
    def utc_prep_times(cls, value: str | None) -> str | None:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def consistent_prep_lifecycle(self) -> Self:
        if self.state == "PREPARING" and self.staged_cleanup != "PENDING":
            raise ValueError("preparing credential must be pending cleanup")
        if self.state in {"ACTIVE", "DELETED"} and self.staged_cleanup != "NOT_REQUIRED":
            raise ValueError("settled credential has no staged cleanup")
        if self.state == "ABORTED" and self.staged_cleanup not in {"RELEASED", "UNRESOLVED"}:
            raise ValueError("aborted credential must retain cleanup outcome")
        return self


class ProvisioningIntentRecord(_ProvisioningInternal):
    domain_id: StrictStr
    local_user_id: _PositiveUser
    resource_kind: Literal["USER", "KB", "KEY"]
    phase: Literal["PREPARED", "WRITE_PENDING", "UNKNOWN", "VERIFIED", "REJECTED"]
    receipt_id: StrictStr | None
    task_id: StrictStr
    created_by_attempt_id: StrictStr
    last_send_attempt_id: StrictStr | None
    created_at: StrictStr
    updated_at: StrictStr
    parent_intent_id: StrictStr | None
    remote_user_id: StrictStr | None
    tenant_id: _UInt64 | None
    kb_id: StrictStr | None
    absence_proof: Literal["NOT_SENT", "DEFINITE_NO_EFFECT"] | None

    @field_validator(
        "domain_id",
        "receipt_id",
        "task_id",
        "created_by_attempt_id",
        "last_send_attempt_id",
        "parent_intent_id",
    )
    @classmethod
    def canonical_intent_refs(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_intent_times(cls, value: str) -> str:
        _utc_timestamp(value)
        return value

    @field_validator("remote_user_id", "kb_id")
    @classmethod
    def intent_remote_id_shape(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 36):
            raise ValueError("remote identifier must contain 1..36 characters")
        if value is not None:
            value.encode("utf-8", errors="strict")
        return value

    @model_validator(mode="after")
    def intent_parent_shape(self) -> Self:
        if self.resource_kind == "USER" and self.parent_intent_id is not None:
            raise ValueError("user intent cannot have a parent")
        if self.resource_kind in {"KB", "KEY"} and self.parent_intent_id is None:
            raise ValueError("child intent requires a parent intent")
        if self.resource_kind == "USER" and self.kb_id is not None:
            raise ValueError("user intent cannot bind a knowledge base")
        if self.resource_kind == "KEY" and self.kb_id is None:
            raise ValueError("key intent requires a knowledge base ID")
        return self


class ProvisioningReceiptRecord(_ProvisioningInternal):
    intent_id: StrictStr
    domain_id: StrictStr
    local_user_id: _PositiveUser
    resource_kind: Literal["USER", "KB", "KEY"]
    proof_kind: Literal["CREATED_201", "ADMIN_ATTESTED", "OWNER_MATCH"]
    task_id: StrictStr
    source_attempt_id: StrictStr
    recorded_at: StrictStr
    remote_user_id: StrictStr
    tenant_id: _UInt64
    kb_id: StrictStr | None
    key_id: _UInt64 | None

    @field_validator("intent_id", "domain_id", "task_id", "source_attempt_id")
    @classmethod
    def canonical_receipt_refs(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("recorded_at")
    @classmethod
    def utc_recorded_at(cls, value: str) -> str:
        _utc_timestamp(value)
        return value

    @field_validator("remote_user_id", "kb_id")
    @classmethod
    def receipt_remote_id_shape(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 36):
            raise ValueError("remote identifier must contain 1..36 characters")
        if value is not None:
            value.encode("utf-8", errors="strict")
        return value

    @model_validator(mode="after")
    def receipt_resource_shape(self) -> Self:
        if self.resource_kind == "USER" and self.kb_id is not None:
            raise ValueError("user receipt cannot bind a knowledge base")
        if self.resource_kind == "KB" and self.kb_id is None:
            raise ValueError("knowledge base receipt requires an ID")
        if self.resource_kind == "KEY" and (self.kb_id is None or self.key_id is None):
            raise ValueError("key receipt requires knowledge base and key IDs")
        if self.resource_kind != "KEY" and self.key_id is not None:
            raise ValueError("non-key receipt cannot bind a key ID")
        return self


class ProvisioningResourceClaimRecord(_ProvisioningInternal):
    domain_id: StrictStr
    resource_kind: Literal["USER", "TENANT", "KB", "KEY"]
    tenant_id: _UInt64 | None
    remote_id: StrictStr
    local_user_id: _PositiveUser
    task_id: StrictStr
    intent_id: StrictStr
    receipt_id: StrictStr
    claimed_at: StrictStr

    @field_validator("domain_id", "task_id", "intent_id", "receipt_id")
    @classmethod
    def canonical_claim_refs(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("remote_id")
    @classmethod
    def nonempty_claim_id(cls, value: str) -> str:
        if not value:
            raise ValueError("claim remote ID is required")
        value.encode("utf-8", errors="strict")
        return value

    @field_validator("claimed_at")
    @classmethod
    def utc_claimed_at(cls, value: str) -> str:
        _utc_timestamp(value)
        return value

    @model_validator(mode="after")
    def claim_remote_id_shape(self) -> Self:
        if self.resource_kind in {"USER", "KB"} and len(self.remote_id) > 36:
            raise ValueError("remote resource ID cannot exceed 36 characters")
        if self.resource_kind in {"USER", "TENANT"} and self.tenant_id is not None:
            raise ValueError("user and tenant claims have no tenant scope")
        if self.resource_kind in {"KB", "KEY"} and self.tenant_id is None:
            raise ValueError("knowledge resource claims require tenant scope")
        if self.resource_kind in {"TENANT", "KEY"}:
            if (
                not self.remote_id.isascii()
                or not self.remote_id.isdecimal()
                or str(int(self.remote_id)) != self.remote_id
            ):
                raise ValueError("numeric remote claim ID must be canonical decimal")
            maximum = 18_446_744_073_709_551_615
            if not 1 <= int(self.remote_id) <= maximum:
                raise ValueError("numeric remote claim ID is outside uint64")
        return self


class ProvisioningScanRecord(_ProvisioningInternal):
    after_user_id: _NonNegative
    updated_at: StrictStr

    @field_validator("updated_at")
    @classmethod
    def utc_scan_updated_at(cls, value: str) -> str:
        _utc_timestamp(value)
        return value


class ProvisioningDomainRecord(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt
    domain_id: StrictStr
    canonical_base_url: StrictStr

    @field_validator("domain_id")
    @classmethod
    def canonical_domain_id(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("canonical_base_url")
    @classmethod
    def canonical_url(cls, value: str) -> str:
        if normalize_base_url(value) != value:
            raise ValueError("base URL must be canonical")
        return value


class ProvisioningConfigRecord(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt
    domain_id: StrictStr
    name: StrictStr
    base_url: StrictStr
    enabled: StrictBool
    admin_credential_ref: StrictStr
    admin_credential_revision: Literal[2]
    updated_at: StrictStr

    @field_validator("domain_id", "admin_credential_ref")
    @classmethod
    def canonical_ids(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("name")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        if _trimmed_bounded(value, "name", 80) != value:
            raise ValueError("name must be trimmed")
        return value

    @field_validator("base_url")
    @classmethod
    def canonical_url(cls, value: str) -> str:
        if normalize_base_url(value) != value:
            raise ValueError("base URL must be canonical")
        return value

    @field_validator("admin_credential_revision", mode="before")
    @classmethod
    def strict_active_revision(cls, value: object) -> int:
        if type(value) is not int or value != 2:
            raise ValueError("active credential revision must be integer 2")
        return value

    @field_validator("updated_at")
    @classmethod
    def utc_updated_at(cls, value: str) -> str:
        _utc_timestamp(value)
        return value


class ProvisioningAuthBlockRecord(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt
    domain_id: StrictStr
    credential_revision: Literal[2]
    http_status: Literal[401, 403]
    rejected_at: StrictStr

    @field_validator("domain_id")
    @classmethod
    def canonical_domain_id(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("credential_revision", mode="before")
    @classmethod
    def strict_active_revision(cls, value: object) -> int:
        if type(value) is not int or value != 2:
            raise ValueError("credential revision must be integer 2")
        return value

    @field_validator("http_status", mode="before")
    @classmethod
    def strict_http_status(cls, value: object) -> int:
        if type(value) is not int or value not in {401, 403}:
            raise ValueError("HTTP status must be integer 401 or 403")
        return value

    @field_validator("rejected_at")
    @classmethod
    def utc_rejected_at(cls, value: str) -> str:
        _utc_timestamp(value)
        return value


class ProvisioningConfigPreparationRecord(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt
    domain_id: StrictStr
    expected_config_revision: _Revision
    expected_anchor_revision: _Revision
    previous_credential_ref: StrictStr | None
    previous_credential_revision: Literal[2] | None
    planned_credential_ref: StrictStr
    planned_active_revision: Literal[2]
    state: Literal["PREPARING", "COMMITTED", "ABORTED"]
    staged_cleanup: Literal["NOT_REQUIRED", "PENDING", "RELEASED", "UNRESOLVED"]
    created_at: StrictStr
    updated_at: StrictStr

    @field_validator("domain_id", "planned_credential_ref")
    @classmethod
    def canonical_ids(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("previous_credential_ref")
    @classmethod
    def canonical_previous_ref(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None

    @field_validator("previous_credential_revision", "planned_active_revision", mode="before")
    @classmethod
    def strict_credential_revisions(cls, value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value != 2:
            raise ValueError("credential revision must be integer 2")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamps(cls, value: str) -> str:
        _utc_timestamp(value)
        return value

    @model_validator(mode="after")
    def consistent_lifecycle(self) -> Self:
        if (self.previous_credential_ref is None) != (self.previous_credential_revision is None):
            raise ValueError("previous credential fields must be both null or present")
        if (self.expected_config_revision == 0) != (self.previous_credential_ref is None):
            raise ValueError("initial config revision requires no previous credential")
        if self.previous_credential_ref == self.planned_credential_ref:
            raise ValueError("planned credential must be new")
        expected_cleanup = {
            "PREPARING": "PENDING",
            "COMMITTED": "NOT_REQUIRED",
        }
        required = expected_cleanup.get(self.state)
        if required is not None and self.staged_cleanup != required:
            raise ValueError("preparation state and cleanup status disagree")
        if self.state == "ABORTED" and self.staged_cleanup not in {"RELEASED", "UNRESOLVED"}:
            raise ValueError("aborted preparation must retain cleanup outcome")
        return self


class ProvisioningIntentHeader(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt
    domain_id: StrictStr
    local_user_id: _PositiveUser
    resource_kind: Literal["USER", "KB", "KEY"]
    phase: Literal["PREPARED", "WRITE_PENDING", "UNKNOWN", "VERIFIED", "REJECTED"]
    receipt_id: StrictStr | None

    @field_validator("domain_id", "receipt_id")
    @classmethod
    def canonical_ids(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None


class ProvisioningReceiptHeader(_ProvisioningOutput):
    schema_id: Literal["octop.enterprise.provisioning.v1"]
    version: StrictInt
    intent_id: StrictStr
    domain_id: StrictStr
    local_user_id: _PositiveUser
    resource_kind: Literal["USER", "KB", "KEY"]
    proof_kind: Literal["CREATED_201", "ADMIN_ATTESTED", "OWNER_MATCH"]

    @field_validator("intent_id", "domain_id")
    @classmethod
    def canonical_ids(cls, value: str) -> str:
        return _canonical_uuid4(value)
