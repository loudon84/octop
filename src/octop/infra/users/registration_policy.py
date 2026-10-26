"""Persisted policy for requiring unique email addresses on new accounts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol

try:
    from psycopg import errors as pg_errors
except ImportError:  # pragma: no cover - optional PostgreSQL driver
    pg_errors = None  # type: ignore[assignment]

from octop.i18n.domains.errors import error_message
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.email import normalize_email, parse_optional_email

_SETTING_KEY = "user_registration.email_required"
_EMAIL_UNIQUE_INDEX = "idx_users_email"


def is_email_unique_violation(exc: BaseException) -> bool:
    """Match only the users email unique index, not other integrity failures."""
    if isinstance(exc, sqlite3.IntegrityError):
        return str(exc).lower() == "unique constraint failed: users.email"
    if pg_errors is not None and isinstance(exc, pg_errors.UniqueViolation):
        return getattr(getattr(exc, "diag", None), "constraint_name", None) == (_EMAIL_UNIQUE_INDEX)
    return False


@dataclass(frozen=True)
class UserRegistrationPolicy:
    email_required: bool = False

    def __post_init__(self) -> None:
        if type(self.email_required) is not bool:
            raise TypeError("email_required must be a bool")


class _SettingsStore(Protocol):
    def get(self, key: str) -> str | None: ...


def load_registration_policy(settings_repo: _SettingsStore) -> UserRegistrationPolicy:
    try:
        raw = settings_repo.get(_SETTING_KEY)
    except Exception as exc:
        raise OctopError(
            ErrorCode.REGISTRATION_POLICY_INVALID,
            error_message("REGISTRATION_POLICY_INVALID"),
        ) from exc
    if raw is None:
        return UserRegistrationPolicy()
    if raw not in ("true", "false"):
        raise OctopError(
            ErrorCode.REGISTRATION_POLICY_INVALID,
            error_message("REGISTRATION_POLICY_INVALID"),
        )
    return UserRegistrationPolicy(email_required=raw == "true")


def validate_registration_email(
    raw_email: str | None,
    policy: UserRegistrationPolicy,
) -> str | None:
    email = parse_optional_email(raw_email)
    if policy.email_required and email is None:
        raise OctopError(ErrorCode.EMAIL_REQUIRED, error_message("EMAIL_REQUIRED"))
    return email


def update_registration_policy(
    services: SharedServices,
    actor_user_id: int,
    policy: UserRegistrationPolicy,
) -> UserRegistrationPolicy:
    actor = services.user_repo.get(actor_user_id)
    if actor is None or actor.disabled or actor.role != "admin":
        raise OctopError(ErrorCode.FORBIDDEN, "admin required")

    current = load_registration_policy(services.settings_repo)
    if policy.email_required and not current.email_required:
        seen: set[str] = set()
        for row in services.user_repo.list(include_disabled=True):
            if not row.email:
                continue
            normalized = normalize_email(row.email)
            try:
                valid = parse_optional_email(row.email)
            except OctopError:
                valid = None
            if normalized != row.email or valid is None or valid in seen:
                raise OctopError(
                    ErrorCode.REGISTRATION_POLICY_CONFLICT,
                    error_message("REGISTRATION_POLICY_CONFLICT"),
                )
            seen.add(valid)

    services.settings_repo.set(_SETTING_KEY, "true" if policy.email_required else "false")
    services.audit_repo.write(
        actor=actor.username,
        action="user.registration_policy.update",
        target=_SETTING_KEY,
        payload="email_required=true" if policy.email_required else "email_required=false",
    )
    return policy
