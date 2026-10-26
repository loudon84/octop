from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.password import hash_password
from octop.infra.users.registration_policy import (
    UserRegistrationPolicy,
    is_email_unique_violation,
    load_registration_policy,
    update_registration_policy,
    validate_registration_email,
)
from octop.infra.utils.paths import PathLayout


class Settings:
    def __init__(self, value: Any = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    def get(self, key: str) -> str | None:
        assert key == "user_registration.email_required"
        if self.error:
            raise self.error
        return self.value


@pytest.fixture
def services(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    result = build_shared_services(db=db, paths=paths, config=OctopConfig())
    yield result
    db.close()


@pytest.mark.parametrize(("raw", "expected"), [(None, False), ("true", True), ("false", False)])
def test_load_policy_values(raw: str | None, expected: bool) -> None:
    assert load_registration_policy(Settings(raw)).email_required is expected


@pytest.mark.parametrize("raw", ["TRUE", "yes", "", "1"])
def test_load_policy_rejects_corrupt_values(raw: str) -> None:
    with pytest.raises(OctopError) as caught:
        load_registration_policy(Settings(raw))
    assert caught.value.code == ErrorCode.REGISTRATION_POLICY_INVALID
    assert caught.value.status == 503


def test_load_policy_fails_closed_on_store_error() -> None:
    with pytest.raises(OctopError) as caught:
        load_registration_policy(Settings(error=OSError("offline")))
    assert caught.value.code == ErrorCode.REGISTRATION_POLICY_INVALID


def test_policy_type_is_strict() -> None:
    with pytest.raises(TypeError):
        UserRegistrationPolicy(email_required=1)  # type: ignore[arg-type]


def test_email_validation_normalizes_and_requires_only_when_enabled() -> None:
    disabled = UserRegistrationPolicy()
    enabled = UserRegistrationPolicy(email_required=True)
    assert validate_registration_email(None, disabled) is None
    assert validate_registration_email("  Person@Example.com ", enabled) == "person@example.com"
    for missing in (None, "", "  "):
        with pytest.raises(OctopError) as caught:
            validate_registration_email(missing, enabled)
        assert caught.value.code == ErrorCode.EMAIL_REQUIRED
    with pytest.raises(OctopError) as caught:
        validate_registration_email("bad", enabled)
    assert caught.value.code == ErrorCode.EMAIL_INVALID


def test_email_unique_violation_matches_only_email_index() -> None:
    assert is_email_unique_violation(
        sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
    )
    assert not is_email_unique_violation(
        sqlite3.IntegrityError("UNIQUE constraint failed: users.username")
    )
    assert not is_email_unique_violation(sqlite3.IntegrityError("FOREIGN KEY constraint failed"))


def test_enable_accepts_legacy_missing_email_and_records_safe_audit(services) -> None:
    actor_id = services.user_repo.create(username="admin", role="admin")
    services.user_repo.create(username="legacy", role="user")

    result = update_registration_policy(
        services, actor_id, UserRegistrationPolicy(email_required=True)
    )

    assert result.email_required is True
    assert load_registration_policy(services.settings_repo).email_required is True
    audit = services.audit_repo.query(action="user.registration_policy.update")
    assert len(audit) == 1
    assert audit[0].actor == "admin"
    assert audit[0].target == "user_registration.email_required"
    assert audit[0].payload == "email_required=true"


@pytest.mark.parametrize(
    "emails", [[" Person@Example.com "], ["bad"], ["same@example.com", "same@example.com"]]
)
def test_enable_rejects_noncanonical_or_invalid_or_duplicate_emails_without_rewriting(
    services, emails: list[str]
) -> None:
    actor_id = services.user_repo.create(username="admin", role="admin")
    if len(emails) > 1:
        # Simulate duplicate historical data that predates the email index.
        with services.db.transaction() as conn:
            conn.execute("DROP INDEX idx_users_email")
            for index, email in enumerate(emails):
                conn.execute(
                    "INSERT INTO users(username, password_hash, role, email, disabled, created_at, locale, permissions) "
                    "VALUES (?, ?, 'user', ?, 0, 1, 'en', '[]')",
                    (f"legacy{index}", hash_password("TestPass12"), email),
                )
    else:
        for index, email in enumerate(emails):
            services.user_repo.create(
                username=f"legacy{index}",
                password_hash=hash_password("TestPass12"),
                role="user",
                email=email,
            )
    before = [row.email for row in services.user_repo.list(include_disabled=True)]

    with pytest.raises(OctopError) as caught:
        update_registration_policy(services, actor_id, UserRegistrationPolicy(email_required=True))

    assert caught.value.code == ErrorCode.REGISTRATION_POLICY_CONFLICT
    assert [row.email for row in services.user_repo.list(include_disabled=True)] == before
    assert load_registration_policy(services.settings_repo).email_required is False


def test_update_requires_fresh_database_admin_role(services) -> None:
    actor_id = services.user_repo.create(username="admin", role="user")
    with pytest.raises(OctopError) as caught:
        update_registration_policy(services, actor_id, UserRegistrationPolicy(True))
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert services.settings_repo.get("user_registration.email_required") is None
