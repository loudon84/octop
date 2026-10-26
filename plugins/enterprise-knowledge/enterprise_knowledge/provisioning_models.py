"""Public and input DTOs for enterprise user provisioning."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, SecretStr, StrictBool, StrictInt, StrictStr, field_validator

from .models import StrictDto, _validate_uuid4, _version, normalize_base_url

ProvisioningState = Literal[
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
ProvisioningErrorCode = Literal[
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
_UTC_SUFFIX = re.compile(r"(?:Z|\+00:00)$")
_ISO_DATETIME_PREFIX = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4}-W\d{2}(?:-\d)?|\d{4}W\d{2}(?:\d)?)[Tt]"
)


class _ProvisioningOutput(StrictDto):
    schema_id: Literal["octop.enterprise.provisioning.v1"] = "octop.enterprise.provisioning.v1"
    version: StrictInt = 1

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


def _canonical_uuid4(value: str) -> str:
    normalized = _validate_uuid4(value)
    if value != normalized:
        raise ValueError("identifier must be a canonical UUIDv4")
    return value


def _utc_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    if not _ISO_DATETIME_PREFIX.match(value) or not _UTC_SUFFIX.search(value):
        raise ValueError("timestamp must use explicit UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError("timestamp must be a real ISO8601 UTC timestamp") from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must use UTC")
    return value


def _trimmed_bounded(value: str, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= maximum:
        raise ValueError(f"{field_name} must contain 1..{maximum} characters")
    return normalized


class ProvisioningStatus(_ProvisioningOutput):
    revision: Annotated[StrictInt, Field(ge=0)]
    state: ProvisioningState
    error_code: ProvisioningErrorCode | None
    updated_at: StrictStr | None
    last_attempt_at: StrictStr | None
    next_retry_at: StrictStr | None
    can_retry: StrictBool

    @field_validator("updated_at", "last_attempt_at", "next_retry_at")
    @classmethod
    def timestamps_are_utc(cls, value: str | None) -> str | None:
        return _utc_timestamp(value)


class ProvisioningConfig(_ProvisioningOutput):
    revision: Annotated[StrictInt, Field(ge=0)]
    anchor_revision: Annotated[StrictInt, Field(ge=0)]
    name: StrictStr | None
    base_url: StrictStr | None
    enabled: StrictBool
    credential_configured: StrictBool
    updated_at: StrictStr | None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return _trimmed_bounded(value, "name", 80) if value is not None else None

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value: str | None) -> str | None:
        return normalize_base_url(value) if value is not None else None

    @field_validator("updated_at")
    @classmethod
    def timestamp_is_utc(cls, value: str | None) -> str | None:
        return _utc_timestamp(value)


class ProvisioningConfigSaveInput(StrictDto):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    expected_anchor_revision: Annotated[StrictInt, Field(ge=0)]
    name: StrictStr
    base_url: StrictStr
    enabled: StrictBool
    system_admin_jwt: SecretStr | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _trimmed_bounded(value, "name", 80)

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @field_validator("system_admin_jwt", mode="before")
    @classmethod
    def validate_jwt_text(cls, value: object) -> SecretStr | None:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
        elif type(value) is str:
            secret = value
        else:
            raise ValueError("system_admin_jwt must be a string or null")
        if not secret or secret != secret.strip():
            raise ValueError("system_admin_jwt must be non-empty without surrounding whitespace")
        return SecretStr(secret)


class ProvisioningRetryInput(StrictDto):
    expected_revision: Annotated[StrictInt, Field(ge=0)]


class ProvisioningTaskSummary(_ProvisioningOutput):
    task_id: StrictStr
    user_id: Annotated[StrictInt, Field(gt=0)]
    email: StrictStr | None
    status: ProvisioningStatus

    @field_validator("task_id")
    @classmethod
    def canonical_task_id(cls, value: str) -> str:
        return _canonical_uuid4(value)


class ProvisioningTasksInput(StrictDto):
    after_user_id: Annotated[StrictInt, Field(ge=0)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 50


class ProvisioningTasksOutput(_ProvisioningOutput):
    items: list[ProvisioningTaskSummary]
    next_user_id: Annotated[StrictInt, Field(gt=0)] | None


class ProvisioningTaskRetryInput(StrictDto):
    task_id: StrictStr
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    reason: StrictStr

    @field_validator("task_id")
    @classmethod
    def canonical_task_id(cls, value: str) -> str:
        return _canonical_uuid4(value)

    @field_validator("reason")
    @classmethod
    def normalized_reason(cls, value: str) -> str:
        return _trimmed_bounded(value, "reason", 500)


class ProvisioningRetainedSummary(_ProvisioningOutput):
    intent_id: StrictStr
    local_user_id: Annotated[StrictInt, Field(gt=0)]
    resource_kind: Literal["USER", "KB", "KEY"]
    phase: Literal["PREPARED", "WRITE_PENDING", "UNKNOWN", "VERIFIED", "REJECTED"]
    proof_kind: Literal["CREATED_201", "ADMIN_ATTESTED", "OWNER_MATCH"] | None
    has_receipt: StrictBool
    review_required: StrictBool
    revision: Annotated[StrictInt, Field(gt=0)]

    @field_validator("intent_id")
    @classmethod
    def canonical_intent_id(cls, value: str) -> str:
        return _canonical_uuid4(value)


class ProvisioningRetainedListInput(StrictDto):
    after_key: StrictStr | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 50

    @field_validator("after_key")
    @classmethod
    def canonical_after_key(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None


class ProvisioningRetainedListOutput(_ProvisioningOutput):
    items: list[ProvisioningRetainedSummary]
    next_key: StrictStr | None

    @field_validator("next_key")
    @classmethod
    def canonical_next_key(cls, value: str | None) -> str | None:
        return _canonical_uuid4(value) if value is not None else None
