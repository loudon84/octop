"""Strict, immutable DTOs for enterprise knowledge configuration."""

from __future__ import annotations

import ipaddress
import os
import re
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

SCHEMA_ID = "octop.enterprise.knowledge.v1"
_ID_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


class StrictDto(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _version(value: object) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("version must be integer 1")
    return value


def _validate_id(value: str) -> str:
    if value != value.strip() or not _ID_RE.fullmatch(value):
        raise ValueError("identifier must be non-empty and path/control-character free")
    return value


def _validate_uuid4(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("identifier must be UUIDv4") from exc
    if parsed.version != 4:
        raise ValueError("identifier must be UUIDv4")
    return str(parsed)


def _unique_ids(value: list[str]) -> list[str]:
    result: list[str] = []
    for item in value:
        _validate_id(item)
        if item not in result:
            result.append(item)
    return result


def _origin(value: str) -> tuple[str, bytes, int]:
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.raw_host:
        raise ValueError("base_url must use HTTP or HTTPS and include a host")
    if urlsplit(value).netloc.endswith(":"):
        raise ValueError("URL port must not be empty")
    port = url.port
    return (
        url.scheme,
        url.raw_host,
        port if port is not None else (443 if url.scheme == "https" else 80),
    )


def normalize_base_url(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("base_url must be a non-empty URL")
    split = urlsplit(value)
    if split.username is not None or split.password is not None:
        raise ValueError("base_url must not contain user information")
    if "?" in value or "#" in value or split.query or split.fragment:
        raise ValueError("base_url must not contain query or fragment")
    try:
        url = httpx.URL(value)
        origin = _origin(value)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError("base_url is invalid") from exc
    host = url.raw_host.decode("ascii").lower()
    port = url.port
    is_loopback = host == "localhost"
    with suppress(ValueError):
        is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
    if url.scheme == "http" and not is_loopback:
        allowed = os.environ.get("OCTOP_WEKNORA_TEST_HTTP_ORIGIN", "")
        try:
            allowed_url = httpx.URL(allowed)
            allowed_split = urlsplit(allowed)
            pure_origin = (
                allowed
                and allowed_url.path in {"", "/"}
                and allowed_split.username is None
                and allowed_split.password is None
                and not allowed_url.query
                and not allowed_url.fragment
                and "?" not in allowed
                and "#" not in allowed
                and _origin(allowed) == origin
            )
        except (ValueError, httpx.InvalidURL):
            pure_origin = False
        if not pure_origin:
            raise ValueError("remote HTTP requires the exact configured test origin")
    # Inspect the URL's escaped path. HTTPX .path decodes escapes such as %23
    # and %3F, which would change URL structure when the value is persisted.
    path = split.path.rstrip("/") or ""
    while path.endswith("/api/v1"):
        path = path[: -len("/api/v1")].rstrip("/")
    if path:
        raise ValueError("base_url path must be root or a repeated /api/v1 suffix")
    normalized_path = f"{path}/api/v1"
    authority = host
    if ":" in host:
        authority = f"[{host}]"
    if port is not None and port != (443 if url.scheme == "https" else 80):
        authority = f"{authority}:{port}"
    return f"{url.scheme}://{authority}{normalized_path}"


class ProbeComponentSummary(StrictDto):
    status: Literal["SUCCESS", "BLOCKED", "EMPTY", "FAILED"]
    error_code: StrictStr | None = None
    observed_revision: Annotated[StrictInt, Field(ge=0)]
    checked_at: StrictStr


class ConnectionConfig(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: Literal[1] = 1
    connection_id: StrictStr
    revision: Annotated[StrictInt, Field(ge=0)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    base_url: StrictStr
    tenant_id: StrictStr | None = None
    allowed_kb_ids: list[StrictStr] = Field(default_factory=list)
    enabled: StrictBool
    credential_configured: StrictBool
    recovery_blocked: StrictBool = False
    probe_status: Literal[
        "CONFIGURED", "DISABLED", "READY", "AUTH_FAILED", "UNREACHABLE", "DEGRADED"
    ]
    probe_components: dict[str, ProbeComponentSummary] = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("connection_id")
    @classmethod
    def require_uuid4(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("base_url")
    @classmethod
    def canonical_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @field_validator("tenant_id")
    @classmethod
    def valid_tenant_id(cls, value: str | None) -> str | None:
        return _validate_id(value) if value is not None else None

    @field_validator("allowed_kb_ids")
    @classmethod
    def unique_allowed_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("name must not be blank")
        return value


class GrantConfig(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: Literal[1] = 1
    connection_id: StrictStr
    revision: Annotated[StrictInt, Field(ge=0)]
    user_id: Annotated[StrictInt, Field(gt=0)]
    user_allowed_kb_ids: list[StrictStr] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("connection_id")
    @classmethod
    def require_uuid4(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("user_allowed_kb_ids")
    @classmethod
    def unique_allowed_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)


class BindingConfig(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: Literal[1] = 1
    agent_id: StrictStr
    revision: Annotated[StrictInt, Field(ge=0)] = 0
    connection_ref: StrictStr | None = None
    expert_allowed_kb_ids: list[StrictStr] = Field(default_factory=list)
    default_kb_ids: list[StrictStr] = Field(default_factory=list)
    web_allowed: StrictBool = False
    enabled: StrictBool = False

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("agent_id")
    @classmethod
    def valid_agent_id(cls, value: str) -> str:
        return _validate_id(value)

    @field_validator("connection_ref")
    @classmethod
    def require_uuid4(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("expert_allowed_kb_ids", "default_kb_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)

    @model_validator(mode="after")
    def defaults_are_allowed(self) -> Self:
        if not set(self.default_kb_ids).issubset(self.expert_allowed_kb_ids):
            raise ValueError("default_kb_ids must be a subset of expert_allowed_kb_ids")
        return self


class ConnectionSaveInput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: Literal[1] = 1
    connection_id: StrictStr | None = None
    expected_revision: Annotated[StrictInt, Field(ge=0)] = 0
    name: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    base_url: StrictStr
    tenant_id: StrictStr | None = None
    allowed_kb_ids: list[StrictStr] = Field(default_factory=list)
    enabled: StrictBool = False
    api_key: SecretStr | None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("connection_id")
    @classmethod
    def require_uuid4(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("base_url")
    @classmethod
    def canonical_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @field_validator("tenant_id")
    @classmethod
    def valid_tenant_id(cls, value: str | None) -> str | None:
        return _validate_id(value) if value is not None else None

    @field_validator("allowed_kb_ids")
    @classmethod
    def unique_allowed_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def create_requires_key(self) -> Self:
        if self.connection_id is None and (
            self.api_key is None or not self.api_key.get_secret_value()
        ):
            raise ValueError("api_key is required for a new connection")
        return self


class GrantSaveInput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: Literal[1] = 1
    connection_id: StrictStr
    expected_revision: Annotated[StrictInt, Field(ge=0)] = 0
    user_id: Annotated[StrictInt, Field(gt=0)]
    user_allowed_kb_ids: list[StrictStr] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("connection_id")
    @classmethod
    def require_uuid4(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("user_allowed_kb_ids")
    @classmethod
    def unique_allowed_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)


class BindingSaveInput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: Literal[1] = 1
    expected_revision: Annotated[StrictInt, Field(ge=0)] = 0
    connection_ref: StrictStr | None = None
    expert_allowed_kb_ids: list[StrictStr] = Field(default_factory=list)
    default_kb_ids: list[StrictStr] = Field(default_factory=list)
    web_allowed: StrictBool = False
    enabled: StrictBool = False

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("connection_ref")
    @classmethod
    def require_uuid4(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("expert_allowed_kb_ids", "default_kb_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)

    @model_validator(mode="after")
    def defaults_are_allowed(self) -> Self:
        if not set(self.default_kb_ids).issubset(self.expert_allowed_kb_ids):
            raise ValueError("default_kb_ids must be a subset of expert_allowed_kb_ids")
        if self.enabled and self.connection_ref is None:
            raise ValueError("enabled binding requires a connection")
        return self


class EmptyInput(StrictDto):
    pass


class ConnectionCatalogInput(StrictDto):
    connection_id: StrictStr
    expected_revision: Annotated[StrictInt, Field(ge=0)]

    @field_validator("connection_id")
    @classmethod
    def require_uuid4(cls, value: str) -> str:
        return _validate_uuid4(value)


class ConnectionProbeInput(StrictDto):
    connection_id: StrictStr
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    component: Literal["LIST", "SEARCH", "READ"]
    query: StrictStr | None = None
    knowledge_base_ids: list[StrictStr] | None = None
    knowledge_id: StrictStr | None = None

    @field_validator("connection_id")
    @classmethod
    def require_uuid4(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("knowledge_base_ids")
    @classmethod
    def unique_kb_ids(cls, value: list[str] | None) -> list[str] | None:
        return _unique_ids(value) if value is not None else None

    @field_validator("knowledge_id")
    @classmethod
    def valid_knowledge_id(cls, value: str | None) -> str | None:
        return _validate_id(value) if value is not None else None


class ProbeOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    connection_id: StrictStr
    observed_revision: Annotated[StrictInt, Field(ge=0)]
    component: Literal["LIST", "SEARCH", "READ"]
    status: Literal["SUCCESS", "BLOCKED", "EMPTY", "FAILED"]
    error_code: StrictStr | None = None


class ConnectionCatalogOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    connection_id: StrictStr
    observed_revision: Annotated[StrictInt, Field(ge=0)]
    knowledge_bases: list[dict[str, StrictStr | StrictInt]]


class SettingsOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    capabilities: dict[str, StrictBool]
    connections: list[ConnectionConfig] = Field(default_factory=list)
    grants: list[GrantConfig] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class ConnectionSaveOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    connection: ConnectionConfig

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class GrantSaveOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    grant: GrantConfig

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class BindingOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    binding: BindingConfig

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class SessionCapabilities(StrictDto):
    can_save_selection: StrictBool
    can_select_knowledge: StrictBool
    knowledge_ready: StrictBool
    web_ready: StrictBool


class SessionSnapshot(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    agent_id: StrictStr
    thread_id: StrictStr
    revision: Annotated[StrictInt, Field(ge=0)]
    selection_present: StrictBool
    selected_kb_ids: list[StrictStr] = Field(default_factory=list)
    mode: Literal["KB_ONLY", "WEB_ONLY", "HYBRID"] = "KB_ONLY"
    connection_id: StrictStr | None = None
    config_revision: Annotated[StrictInt, Field(ge=0)] = 0
    binding_revision: Annotated[StrictInt, Field(ge=0)] = 0
    grant_revision: Annotated[StrictInt, Field(ge=0)] = 0
    invalid: StrictBool = False
    invalid_reason: StrictStr | None = None
    capabilities: SessionCapabilities = SessionCapabilities(
        can_save_selection=False,
        can_select_knowledge=False,
        knowledge_ready=False,
        web_ready=False,
    )
    search_ready: StrictBool = False
    weknora_query_count: Annotated[StrictInt, Field(ge=0)] = 0

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("agent_id", "thread_id")
    @classmethod
    def valid_scope_id(cls, value: str) -> str:
        return _validate_id(value)

    @field_validator("connection_id")
    @classmethod
    def valid_connection_id(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("selected_kb_ids")
    @classmethod
    def unique_selected_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)


class SessionSourceRevisions(StrictDto):
    connection_id: StrictStr | None
    config_revision: Annotated[StrictInt, Field(ge=0)]
    binding_revision: Annotated[StrictInt, Field(ge=0)]
    grant_revision: Annotated[StrictInt, Field(ge=0)]

    @field_validator("connection_id")
    @classmethod
    def canonical_connection_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _validate_uuid4(value)
        if normalized != value:
            raise ValueError("identifier must be a canonical UUIDv4")
        return normalized


class ScopeDecision(StrictDto):
    effective_kb_ids: list[StrictStr]
    source_revisions: SessionSourceRevisions
    session_revision: Annotated[StrictInt, Field(ge=0)]

    @field_validator("effective_kb_ids")
    @classmethod
    def unique_effective_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)


class SessionSaveInput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    selected_kb_ids: list[StrictStr] = Field(default_factory=list)
    mode: Literal["KB_ONLY", "WEB_ONLY", "HYBRID"] = "KB_ONLY"
    source_revisions: SessionSourceRevisions

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("selected_kb_ids")
    @classmethod
    def unique_selected_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)


class SessionOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    session: SessionSnapshot

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class KnowledgeBaseSummary(StrictDto):
    kb_id: StrictStr
    name: StrictStr | None = None

    @field_validator("kb_id")
    @classmethod
    def valid_kb_id(cls, value: str) -> str:
        return _validate_id(value)


class OperationRecord(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation_id: StrictStr
    revision: Annotated[StrictInt, Field(ge=1)]
    user_id: Annotated[StrictInt, Field(gt=0)]
    agent_id: StrictStr
    thread_id: StrictStr
    session_revision: Annotated[StrictInt, Field(ge=0)]
    config_revision: Annotated[StrictInt, Field(ge=0)]
    binding_revision: Annotated[StrictInt, Field(ge=0)]
    grant_revision: Annotated[StrictInt, Field(ge=0)]
    connection_id: StrictStr | None = None
    status: Literal[
        "VALIDATING",
        "RETRIEVING",
        "GENERATING",
        "COMPLETED",
        "EMPTY",
        "DENIED",
        "FAILED",
        "CANCELLED",
        "INTERRUPTED",
    ]
    observed_knowledge_bases: list[KnowledgeBaseSummary] | None = None
    error_code: StrictStr | None = None
    approval_id: StrictStr | None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("operation_id", "approval_id")
    @classmethod
    def valid_uuid(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("connection_id")
    @classmethod
    def valid_connection_id(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("agent_id", "thread_id")
    @classmethod
    def valid_scope_id(cls, value: str) -> str:
        return _validate_id(value)


class QueryApproval(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    approval_id: StrictStr
    operation_id: StrictStr
    user_id: Annotated[StrictInt, Field(gt=0)]
    agent_id: StrictStr
    thread_id: StrictStr
    public_query: StrictStr
    session_revision: Annotated[StrictInt, Field(ge=0)]
    config_revision: Annotated[StrictInt, Field(ge=0)]
    binding_revision: Annotated[StrictInt, Field(ge=0)]
    grant_revision: Annotated[StrictInt, Field(ge=0)]
    state: Literal["PENDING", "CONSUMED", "CANCELLED", "INVALID"] = "PENDING"

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("approval_id", "operation_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("agent_id", "thread_id")
    @classmethod
    def valid_scope_id(cls, value: str) -> str:
        return _validate_id(value)


class EvidenceRecord(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    evidence_id: StrictStr
    operation_id: StrictStr
    user_id: Annotated[StrictInt, Field(gt=0)]
    agent_id: StrictStr
    thread_id: StrictStr
    source_kind: Literal["KNOWLEDGE", "WEB"]
    connection_id: StrictStr | None = None
    kb_id: StrictStr | None = None
    knowledge_id: StrictStr | None = None
    title: StrictStr = ""
    content: StrictStr
    truncated: StrictBool
    retrieved_at: StrictStr
    source_version: StrictStr | None = None
    chunk_index: Annotated[StrictInt, Field(ge=0)] | None = None
    source_page: Annotated[StrictInt, Field(ge=0)] | None = None
    locator: StrictStr | None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("evidence_id", "operation_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("connection_id")
    @classmethod
    def valid_connection_id(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @field_validator("retrieved_at")
    @classmethod
    def utc_retrieved_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("retrieved_at must be an ISO8601 UTC timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("retrieved_at must use UTC")
        return value

    @field_validator("agent_id", "thread_id", "kb_id", "knowledge_id")
    @classmethod
    def valid_source_id(cls, value: str | None) -> str | None:
        return _validate_id(value) if value is not None else None

    @model_validator(mode="after")
    def require_knowledge_source_ids(self) -> Self:
        if self.source_kind == "KNOWLEDGE" and not all(
            (self.connection_id, self.kb_id, self.knowledge_id)
        ):
            raise ValueError("knowledge evidence requires connection_id, kb_id, and knowledge_id")
        if self.source_kind == "WEB" and self.connection_id is not None:
            raise ValueError("web evidence cannot claim a knowledge connection")
        return self


class OperationPrepareInput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    expected_session_revision: Annotated[StrictInt, Field(ge=0)]

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class OperationOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation: OperationRecord

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class EvidenceListInput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation_id: StrictStr

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("operation_id")
    @classmethod
    def valid_operation_id(cls, value: str) -> str:
        return _validate_uuid4(value)


class EvidenceListOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation: OperationRecord
    evidence: list[EvidenceRecord]

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class RetrievalOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation: OperationRecord
    evidence: list[EvidenceRecord]

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class KnowledgeListOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation: OperationRecord
    knowledge_bases: list[KnowledgeBaseSummary]

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)


class ReadInput(StrictDto):
    operation_id: StrictStr
    evidence_id: StrictStr
    continuation: StrictStr | None = None

    @field_validator("operation_id", "evidence_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("continuation")
    @classmethod
    def valid_continuation(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None


class OriginalInput(StrictDto):
    operation_id: StrictStr
    evidence_id: StrictStr

    @field_validator("operation_id", "evidence_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _validate_uuid4(value)


class ReadPage(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation_id: StrictStr
    evidence_id: StrictStr
    knowledge_id: StrictStr
    status: Literal["SUCCESS", "EMPTY"]
    content: Annotated[StrictStr, Field(max_length=8000)]
    truncated: StrictBool
    continuation: StrictStr | None = None
    source_version: None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("operation_id", "evidence_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("continuation")
    @classmethod
    def valid_continuation(cls, value: str | None) -> str | None:
        return _validate_uuid4(value) if value is not None else None

    @model_validator(mode="after")
    def consistent_page(self) -> Self:
        if self.status == "EMPTY" and (self.content or self.truncated or self.continuation):
            raise ValueError("empty page cannot contain text or continuation")
        if self.truncated != (self.continuation is not None):
            raise ValueError("continuation must match truncation state")
        return self


class OriginalOutput(StrictDto):
    schema_id: Literal["octop.enterprise.knowledge.v1"] = "octop.enterprise.knowledge.v1"
    version: StrictInt = 1
    operation_id: StrictStr
    evidence_id: StrictStr
    available: Literal[False] = False
    reason: Literal["CAPABILITY_UNVERIFIED"] = "CAPABILITY_UNVERIFIED"
    resource_token: None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> int:
        return _version(value)

    @field_validator("operation_id", "evidence_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _validate_uuid4(value)
