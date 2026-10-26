"""Stable, transport-independent contracts for the plugin host facade."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

HostErrorCode = Literal["unavailable", "conflict", "denied", "invalid", "not_found"]


class PluginHostError(Exception):
    """A safe host error whose public text never includes operation data."""

    def __init__(self, code: HostErrorCode, _detail: str | None = None) -> None:
        if code not in {"unavailable", "conflict", "denied", "invalid", "not_found"}:
            raise ValueError("unsupported plugin host error code")
        self.code = code
        super().__init__(code)


class PluginActionError(Exception):
    """Explicit plugin-owned, already-localized error safe to return over HTTP."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not code or not code.replace("_", "").isalnum() or not message:
            raise ValueError("plugin action errors require a code and safe message")
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
            raise ValueError("plugin action error status must be between 400 and 599")
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        super().__init__(code)


class PluginHostExecutionError(Exception):
    """Safe marker for an unexpected plugin callback failure."""

    def __init__(self) -> None:
        super().__init__("plugin callback failed")


@dataclass(frozen=True)
class PluginResource:
    """Constrained resource output; arbitrary Response headers and redirects are excluded."""

    body: bytes | AsyncIterator[bytes]
    media_type: str = "application/octet-stream"
    filename: str | None = None


@dataclass(frozen=True)
class PluginActor:
    user_id: int
    is_admin: bool
    permissions: frozenset[str] = field(default_factory=frozenset)
    agent_id: str | None = None
    thread_id: str | None = None
    operation_id: str | None = None
    locale: str = "en"

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        object.__setattr__(self, "permissions", frozenset(self.permissions))


@dataclass(frozen=True)
class HostRecord:
    id: str
    namespace: str
    logical_key: str
    owner_user_id: int
    revision: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecordWrite:
    namespace: str
    logical_key: str
    owner_user_id: int
    payload: dict[str, Any]
    expected_revision: int


@dataclass(frozen=True)
class RecordCheck:
    """A read-only revision precondition for an atomic Host commit."""

    namespace: str
    logical_key: str
    expected_revision: int


@dataclass(frozen=True)
class RetainedRecord:
    namespace: str
    logical_key: str
    revision: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class RetainedRecordWrite:
    namespace: str
    logical_key: str
    payload: dict[str, Any]
    expected_revision: int


@dataclass(frozen=True)
class RetainedRecordCheck:
    namespace: str
    logical_key: str
    expected_revision: int


@dataclass(frozen=True)
class RetainedRecordPage:
    items: tuple[RetainedRecord, ...]
    next_logical_key: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class CredentialActivation:
    credential_id: str
    owner_user_id: int
    expected_revision: int


@dataclass(frozen=True)
class PluginUserIdentity:
    user_id: int
    email: str | None
    disabled: bool
    snapshot_token: str


@dataclass(frozen=True)
class PluginUserIdentityPage:
    items: tuple[PluginUserIdentity, ...]
    next_user_id: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class UserIdentityCheck:
    expected: PluginUserIdentity


@dataclass(frozen=True)
class CredentialDeletion:
    credential_id: str
    owner_user_id: int
    expected_revision: int


@dataclass(frozen=True)
class RequiredExtensionWrite:
    actor: PluginActor
    agent_id: str
    required_plugins: Mapping[str, str]
    expected_revision: int
    thread_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_plugins", MappingProxyType(dict(self.required_plugins)))


@dataclass(frozen=True)
class RequiredExtensionState:
    revision: int
    required_plugins: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_plugins", MappingProxyType(dict(self.required_plugins)))


@dataclass(frozen=True)
class PluginRequirementReadiness:
    """Generic, immutable view of persisted requirements and local availability."""

    required_plugins: Mapping[str, str]
    unavailable_plugins: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_plugins", MappingProxyType(dict(self.required_plugins)))
        object.__setattr__(self, "unavailable_plugins", tuple(self.unavailable_plugins))


@dataclass(frozen=True)
class PluginExecution:
    actor: PluginActor
    context: dict[str, Any]
    execution_kind: Literal["turn", "resume", "continuation"]
    required_plugins: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_plugins", MappingProxyType(dict(self.required_plugins)))
