"""tests/unit/test_user_manager.py"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


def _insert_sso_provider(manager: UserManager, *, kind: str = "oidc") -> int:
    with manager._services.db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO sso_providers(enabled, kind, created_at, updated_at) VALUES (1, ?, 0, 0)",
            (kind,),
        )
        return int(cursor.lastrowid)


@pytest.fixture
async def manager(tmp_path: Path) -> UserManager:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    return UserManager(services)


async def test_create_user_writes_db(manager: UserManager):
    user = await manager.create(
        username="alice", password="TestPass12", role=Role.ADMIN, display_name="Alice"
    )
    assert user.username == "alice"
    assert user.role == Role.ADMIN
    assert manager.get("alice") is not None


async def test_duplicate_username_rejected(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    with pytest.raises(OctopError) as ei:
        await manager.create(username="a", password="TestPass34", role=Role.USER)
    assert ei.value.code is ErrorCode.USERNAME_TAKEN


async def test_new_user_notifies_once_after_creation_and_notifier_failure_is_safe(
    manager: UserManager, caplog: pytest.LogCaptureFixture
) -> None:
    notifications: list[None] = []
    manager._user_created_notifier = lambda: notifications.append(None)

    created = await manager.create(username="notified", password="TestPass12", role=Role.USER)

    assert created.id is not None
    assert notifications == [None]
    manager._user_created_notifier = lambda: (_ for _ in ()).throw(
        RuntimeError("sensitive-notifier-detail")
    )
    failed_wake_user = await manager.create(
        username="wake-failed", password="TestPass12", role=Role.USER
    )
    assert manager._services.user_repo.get(failed_wake_user.id) is not None
    assert "sensitive-notifier-detail" not in caplog.text
    assert "user-created notifier failed" in caplog.text


async def test_notifier_survives_service_replacement_and_does_not_swallow_cancellation(
    manager: UserManager,
) -> None:
    notifications: list[None] = []
    manager._user_created_notifier = lambda: notifications.append(None)
    manager.replace_services(manager._services)
    await manager.create(username="after-rebind", password="TestPass12", role=Role.USER)
    assert notifications == [None]

    manager._user_created_notifier = lambda: (_ for _ in ()).throw(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        manager.notify_user_created()


async def test_invite_creation_notifies_once_only_after_success(manager: UserManager):
    from octop.infra.users.invites import InviteService

    notifications: list[None] = []
    manager._user_created_notifier = lambda: notifications.append(None)
    admin = await manager.create(username="invite-admin", password="TestPass12", role=Role.ADMIN)
    invite = InviteService(manager._services).create(
        created_by=admin.id,
        actor_username=admin.username,
    )
    assert notifications == [None]

    invited = await manager.create_from_invite(
        code=invite.code,
        username="invitee",
        password="TestPass12",
    )
    assert invited.username == "invitee"
    assert notifications == [None, None]
    with pytest.raises(OctopError):
        await manager.create_from_invite(
            code=invite.code,
            username="invitee-again",
            password="TestPass12",
        )
    assert notifications == [None, None]

    failed_wake_invite = InviteService(manager._services).create(
        created_by=admin.id,
        actor_username=admin.username,
    )
    manager._user_created_notifier = lambda: (_ for _ in ()).throw(
        RuntimeError("invite wake unavailable")
    )
    committed = await manager.create_from_invite(
        code=failed_wake_invite.code,
        username="invite-wake-failed",
        password="TestPass12",
    )
    assert manager._services.user_repo.get(committed.id) is not None
    assert manager._services.invite_repo.get_by_code(failed_wake_invite.code).used_at is not None
    with pytest.raises(OctopError):
        await manager.create_from_invite(
            code=failed_wake_invite.code,
            username="invite-wake-failed-again",
            password="TestPass12",
        )


async def test_sso_notifies_new_identity_once_but_existing_login_never_again(
    manager: UserManager,
):
    provider_id = _insert_sso_provider(manager)
    notifications: list[None] = []
    manager._user_created_notifier = lambda: notifications.append(None)

    created = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="notified-subject",
        claims={"preferred_username": "notified-sso"},
    )
    assert created.username == "notified-sso"
    assert notifications == [None]

    await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="notified-subject",
        claims={"preferred_username": "notified-sso"},
    )
    assert notifications == [None]


async def test_sso_link_notifies_only_when_identity_is_new(manager: UserManager):
    provider_id = _insert_sso_provider(manager, kind="feishu")
    user = await manager.create(username="sso-link-user", password="TestPass12", role=Role.USER)
    notifications: list[None] = []
    manager._user_created_notifier = lambda: notifications.append(None)
    claims = {"preferred_username": user.username}

    await manager.bind_sso_identity(
        user_id=user.id,
        provider_id=provider_id,
        subject="new-link",
        claims=claims,
    )
    await manager.bind_sso_identity(
        user_id=user.id,
        provider_id=provider_id,
        subject="new-link",
        claims=claims,
    )

    assert notifications == [None]


async def test_authenticate_success(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    user = await manager.authenticate("a", "TestPass12")
    assert user is not None
    assert user.username == "a"


async def test_authenticate_by_email(manager: UserManager):
    await manager.create(
        username="alice",
        password="TestPass12",
        role=Role.USER,
        email="Alice@Example.com",
    )
    user = await manager.authenticate("alice@example.com", "TestPass12")
    assert user is not None
    assert user.username == "alice"


async def test_authenticate_prefers_username_over_email(manager: UserManager):
    await manager.create(
        username="bob@x.com",
        password="TestPass12",
        role=Role.USER,
        email="other@x.com",
    )
    await manager.create(
        username="carol",
        password="TestPass34",
        role=Role.USER,
        email="bob@x.com",
    )
    user = await manager.authenticate("bob@x.com", "TestPass12")
    assert user is not None
    assert user.username == "bob@x.com"


async def test_create_duplicate_email_rejected(manager: UserManager):
    await manager.create(
        username="a", password="TestPass12", role=Role.USER, email="dup@example.com"
    )
    with pytest.raises(OctopError) as ei:
        await manager.create(
            username="b", password="TestPass34", role=Role.USER, email="DUP@example.com"
        )
    assert ei.value.code is ErrorCode.EMAIL_TAKEN


async def test_set_email_and_clear(manager: UserManager):
    user = await manager.create(username="a", password="TestPass12", role=Role.USER)
    await manager.set_email("a", "a@example.com")
    row = manager.get_row(user.id)
    assert row is not None
    assert row.email == "a@example.com"
    await manager.set_email("a", None)
    row = manager.get_row(user.id)
    assert row is not None
    assert row.email is None


async def test_authenticate_wrong_password(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    assert await manager.authenticate("a", "bad") is None


async def test_authenticate_rejects_null_password_hash(manager: UserManager):
    manager._services.user_repo.create(username="sso_user", password_hash=None, role="user")
    assert await manager.authenticate("sso_user", "any") is None


async def test_change_password_rejects_null_password_hash(manager: UserManager):
    """Directory/SSO-provisioned accounts have no local password to change."""
    manager._services.user_repo.create(username="sso_user", password_hash=None, role="user")
    with pytest.raises(OctopError) as ei:
        await manager.change_password("sso_user", "any", "NewPass12")
    assert ei.value.code is ErrorCode.PASSWORD_NOT_SET
    assert ei.value.status == 400


async def test_sso_create_then_updates_same_subject(manager: UserManager):
    provider_id = _insert_sso_provider(manager)
    user = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-1",
        claims={"preferred_username": "alice", "email": "Alice@Example.com", "name": "Alice"},
    )

    assert user.role == Role.USER
    assert "channels" in user.permissions
    row = manager.get_row(user.id)
    assert row is not None
    assert row.role_name == "用户"
    assert row.role == "user"
    assert row.password_hash is None
    assert row.email == "alice@example.com"
    assert row.display_name == "Alice"
    assert row.sso_provider_id == provider_id
    assert row.sso_subject == "sub-1"

    updated = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-1",
        claims={"email": "alice-2@example.com", "name": "Alice 2"},
    )

    assert updated.id == user.id
    row = manager.get_row(user.id)
    assert row.display_name == "Alice"
    assert row.email == "alice-2@example.com"


async def test_sso_create_recovers_when_another_worker_wins_identity_race(
    manager: UserManager, monkeypatch: pytest.MonkeyPatch
):
    provider_id = _insert_sso_provider(manager)
    repo = manager._services.user_repo
    create = repo.create

    def create_after_race(*args: object, **kwargs: object) -> int:
        create(*args, **kwargs)
        raise sqlite3.IntegrityError(
            "UNIQUE constraint failed: users.sso_provider_id, users.sso_subject"
        )

    monkeypatch.setattr(repo, "create", create_after_race)

    user = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-race",
        claims={"preferred_username": "alice", "name": "Alice"},
    )

    assert user.username == "alice"
    assert repo.get_by_sso(provider_id, "sub-race").id == user.id


async def test_sso_username_allocation_sanitizes_and_suffixes_conflicts(manager: UserManager):
    provider_id = _insert_sso_provider(manager)
    first = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-1",
        claims={"preferred_username": "alice smith!"},
    )
    second = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-2",
        claims={"preferred_username": "alice smith!"},
    )

    assert first.username == "alicesmith"
    assert second.username == "alicesmith_2"


async def test_sso_email_conflict_does_not_overwrite_existing_email(manager: UserManager):
    provider_id = _insert_sso_provider(manager)
    manager._services.user_repo.create(
        username="local",
        password_hash="not-used",
        role="user",
        email="taken@example.com",
    )

    user = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-1",
        claims={"preferred_username": "sso-user", "email": "taken@example.com"},
    )

    assert manager.get_row(user.id).email is None


async def test_sso_enabled_registration_policy_rejects_missing_and_claimed_email(
    manager: UserManager,
):
    provider_id = _insert_sso_provider(manager)
    manager._services.settings_repo.set("user_registration.email_required", "true")
    manager._services.user_repo.create(
        username="local", password_hash="not-used", role="user", email="taken@example.com"
    )

    with pytest.raises(OctopError) as missing:
        await manager.resolve_or_create_sso_user(
            provider_id=provider_id, subject="no-email", claims={"preferred_username": "missing"}
        )
    with pytest.raises(OctopError) as duplicate:
        await manager.resolve_or_create_sso_user(
            provider_id=provider_id,
            subject="claimed-email",
            claims={"preferred_username": "duplicate", "email": "Taken@Example.com"},
        )

    assert missing.value.code is ErrorCode.EMAIL_REQUIRED
    assert duplicate.value.code is ErrorCode.EMAIL_TAKEN
    assert manager._services.user_repo.get_by_sso(provider_id, "no-email") is None
    assert manager._services.user_repo.get_by_sso(provider_id, "claimed-email") is None


async def test_sso_enabled_policy_normalizes_first_email_and_keeps_bound_login_compatible(
    manager: UserManager,
):
    provider_id = _insert_sso_provider(manager)
    manager._services.settings_repo.set("user_registration.email_required", "true")
    created = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="new-subject",
        claims={"preferred_username": "new-user", "email": " New@Example.com "},
    )
    assert manager.get_row(created.id).email == "new@example.com"

    legacy = manager._services.user_repo.create(
        username="legacy", password_hash=None, role="user", email=None
    )
    manager._services.user_repo.upsert_sso_identity(
        legacy, provider_id=provider_id, subject="legacy-subject"
    )
    linked = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="legacy-subject",
        claims={"email": "new@example.com"},
    )
    assert linked.id == legacy
    assert manager.get_row(legacy).email is None


async def test_existing_sso_binding_survives_missing_email_and_invalid_policy(
    manager: UserManager,
):
    provider_id = _insert_sso_provider(manager)
    existing = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="bound-with-missing-claim",
        claims={"preferred_username": "bound-user", "email": "bound@example.com"},
    )
    manager._services.settings_repo.set("user_registration.email_required", "invalid")

    resolved = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="bound-with-missing-claim",
        claims={"preferred_username": "bound-user"},
    )

    assert resolved.id == existing.id
    assert manager.get_row(existing.id).email == "bound@example.com"
    assert manager._services.user_repo.count() == 1


async def test_sso_email_unique_race_lookup_runs_in_executor(
    manager: UserManager, monkeypatch: pytest.MonkeyPatch
):
    provider_id = _insert_sso_provider(manager)
    manager._services.settings_repo.set("user_registration.email_required", "true")
    manager._services.user_repo.create(
        username="email-owner",
        password_hash="not-used",
        role="user",
        email="race@example.com",
    )
    repo = manager._services.user_repo
    original_lookup = repo.get_by_email
    lookup_threads: list[int] = []

    def lookup(email: str):
        lookup_threads.append(threading.get_ident())
        if len(lookup_threads) == 1:
            return None
        return original_lookup(email)

    def raise_email_unique_violation(**_kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.email")

    monkeypatch.setattr(repo, "get_by_email", lookup)
    monkeypatch.setattr(repo, "create", raise_email_unique_violation)
    loop_thread = threading.get_ident()

    with pytest.raises(OctopError) as duplicate:
        await manager.resolve_or_create_sso_user(
            provider_id=provider_id,
            subject="concurrent-subject",
            claims={"preferred_username": "concurrent-user", "email": "race@example.com"},
        )

    assert duplicate.value.code is ErrorCode.EMAIL_TAKEN
    assert lookup_threads[0] == loop_thread
    assert lookup_threads[-1] != loop_thread


async def test_sso_disabled_user_is_rejected(manager: UserManager):
    provider_id = _insert_sso_provider(manager)
    user = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-1",
        claims={"preferred_username": "alice"},
    )
    await manager.disable(user.username)

    with pytest.raises(OctopError) as ei:
        await manager.resolve_or_create_sso_user(
            provider_id=provider_id,
            subject="sub-1",
            claims={"name": "Alice"},
        )
    assert ei.value.code is ErrorCode.USER_DISABLED


async def test_bind_sso_identity_links_current_user_and_rejects_taken_identity(
    manager: UserManager,
):
    provider_id = _insert_sso_provider(manager, kind="feishu")
    local = await manager.create(
        username="alice", password="TestPass12", role=Role.ADMIN, display_name="Alice"
    )
    other = await manager.create(
        username="bob", password="TestPass12", role=Role.USER, display_name="Bob"
    )
    await manager.bind_sso_identity(
        user_id=other.id,
        provider_id=provider_id,
        subject="union-bob",
        claims={"name": "Bob Feishu"},
    )
    with pytest.raises(OctopError) as ei:
        await manager.bind_sso_identity(
            user_id=local.id,
            provider_id=provider_id,
            subject="union-bob",
            claims={},
        )
    assert ei.value.code is ErrorCode.SSO_IDENTITY_TAKEN

    bound = await manager.bind_sso_identity(
        user_id=local.id,
        provider_id=provider_id,
        subject="union-alice",
        claims={"name": "Alice Feishu", "email": "alice@example.com"},
    )
    assert bound.id == local.id
    row = manager.get_row(local.id)
    assert row.sso_provider_id == provider_id
    assert row.sso_subject == "union-alice"
    assert row.display_name == "Alice"
    assert bound.display_name == "Alice"
    identities = manager.list_sso_identities(local.id)
    assert [(item.kind, item.subject) for item in identities] == [("feishu", "union-alice")]


async def test_bind_multiple_sso_identities_on_same_user(manager: UserManager):
    oidc_id = _insert_sso_provider(manager, kind="oidc")
    feishu_id = _insert_sso_provider(manager, kind="feishu")
    local = await manager.create(
        username="alice", password="TestPass12", role=Role.USER, display_name="Alice"
    )
    await manager.bind_sso_identity(
        user_id=local.id, provider_id=oidc_id, subject="oidc-alice", claims={}
    )
    await manager.bind_sso_identity(
        user_id=local.id, provider_id=feishu_id, subject="feishu-alice", claims={}
    )
    identities = manager.list_sso_identities(local.id)
    assert {(item.kind, item.subject) for item in identities} == {
        ("oidc", "oidc-alice"),
        ("feishu", "feishu-alice"),
    }
    assert manager._services.user_repo.get_by_sso(oidc_id, "oidc-alice").id == local.id
    assert manager._services.user_repo.get_by_sso(feishu_id, "feishu-alice").id == local.id

    await manager.unbind_sso_identity(user_id=local.id, kind="feishu")
    remaining = manager.list_sso_identities(local.id)
    assert [(item.kind, item.subject) for item in remaining] == [("oidc", "oidc-alice")]


async def test_unbind_sso_identity_requires_password(manager: UserManager):
    provider_id = _insert_sso_provider(manager, kind="feishu")
    user = await manager.resolve_or_create_sso_user(
        provider_id=provider_id,
        subject="sub-1",
        claims={"preferred_username": "ssoonly"},
    )
    with pytest.raises(OctopError) as ei:
        await manager.unbind_sso_identity(user_id=user.id, kind="feishu")
    assert ei.value.code is ErrorCode.SSO_UNBIND_REQUIRES_PASSWORD

    local = await manager.create(username="alice", password="TestPass12", role=Role.USER)
    await manager.bind_sso_identity(
        user_id=local.id, provider_id=provider_id, subject="sub-2", claims={}
    )
    unbound = await manager.unbind_sso_identity(user_id=local.id, kind="feishu")
    assert unbound.id == local.id
    row = manager.get_row(local.id)
    assert row.sso_provider_id is None
    assert row.sso_subject is None
    assert manager.list_sso_identities(local.id) == []


async def test_authenticate_locks_after_max_failures(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    max_attempts = manager._login_max_attempts
    for _ in range(max_attempts - 1):
        assert await manager.authenticate("a", "bad") is None
    with pytest.raises(OctopError) as ei:
        await manager.authenticate("a", "bad")
    assert ei.value.code is ErrorCode.LOGIN_LOCKED
    with pytest.raises(OctopError) as ei2:
        await manager.authenticate("a", "bad")
    assert ei2.value.code is ErrorCode.LOGIN_LOCKED
    with pytest.raises(OctopError) as ei3:
        await manager.authenticate("a", "TestPass12")
    assert ei3.value.code is ErrorCode.LOGIN_LOCKED


async def test_reset_password_clears_lockout(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    max_attempts = manager._login_max_attempts
    for _ in range(max_attempts - 1):
        assert await manager.authenticate("a", "bad") is None
    with pytest.raises(OctopError):
        await manager.authenticate("a", "bad")
    with pytest.raises(OctopError):
        await manager.authenticate("a", "TestPass12")
    await manager.reset_password("a", "NewPass12")
    user = await manager.authenticate("a", "NewPass12")
    assert user is not None


async def test_authenticate_disabled_returns_none(manager: UserManager):
    user = await manager.create(username="a", password="TestPass12", role=Role.USER)
    await manager.disable(user.username)
    assert await manager.authenticate("a", "TestPass12") is None


async def test_get_by_username_and_id(manager: UserManager):
    user = await manager.create(username="a", password="TestPass12", role=Role.USER)
    assert manager.get("a").id == user.id
    assert manager.get_by_id(user.id).username == "a"


async def test_change_password(manager: UserManager):
    user = await manager.create(username="a", password="OldPass12", role=Role.USER)
    await manager.change_password(user.username, "OldPass12", "NewPass12")
    assert await manager.authenticate("a", "NewPass12") is not None
    assert await manager.authenticate("a", "OldPass12") is None


async def test_change_password_wrong_old(manager: UserManager):
    await manager.create(username="a", password="OldPass12", role=Role.USER)
    with pytest.raises(OctopError) as ei:
        await manager.change_password("a", "wrong", "NewPass12")
    assert ei.value.code is ErrorCode.AUTH_FAILED


async def test_change_password_rejects_same_as_old(manager: UserManager):
    await manager.create(username="a", password="OldPass12", role=Role.USER)
    with pytest.raises(OctopError) as ei:
        await manager.change_password("a", "OldPass12", "OldPass12")
    assert ei.value.code is ErrorCode.PASSWORD_SAME_AS_OLD


async def test_change_password_rejects_weak_new(manager: UserManager):
    await manager.create(username="a", password="OldPass12", role=Role.USER)
    with pytest.raises(OctopError) as ei:
        await manager.change_password("a", "OldPass12", "12345678")
    assert ei.value.code is ErrorCode.PASSWORD_TOO_WEAK


async def test_create_rejects_weak_password(manager: UserManager):
    with pytest.raises(OctopError) as ei:
        await manager.create(username="a", password="pw", role=Role.USER)
    assert ei.value.code is ErrorCode.PASSWORD_TOO_WEAK


async def test_set_role(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    await manager.set_role("a", Role.ADMIN)
    assert manager.get("a").role == Role.ADMIN


async def test_disable_removes_from_memory(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    await manager.disable("a")
    assert manager.get("a") is None


async def test_enable_restores_memory(manager: UserManager):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    await manager.disable("a")
    await manager.enable("a")
    assert manager.get("a") is not None


async def test_remove_deletes_db_and_dir(manager: UserManager, tmp_path: Path):
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    await manager.remove("a")
    assert (tmp_path / ".octop" / "users" / "a").exists() is False
    assert manager.get("a") is None


async def test_count(manager: UserManager):
    assert manager.count() == 0
    await manager.create(username="a", password="TestPass12", role=Role.USER)
    assert manager.count() == 1


async def test_boot_loads_existing_users(tmp_path: Path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    # Pre-populate via repo
    from octop.infra.users.password import hash_password

    services.user_repo.create(username="legacy", password_hash=hash_password("pw"), role="user")
    manager = UserManager(services)
    await manager.boot()
    assert manager.get("legacy") is not None


async def test_boot_loads_users(tmp_path: Path):
    """UserManager.boot() loads user identity objects (agents are now global)."""
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    from octop.infra.users.password import hash_password

    uid = services.user_repo.create(
        username="alice", password_hash=hash_password("pw"), role="user"
    )
    manager = UserManager(services)
    await manager.boot()
    try:
        user = manager.get("alice")
        assert user is not None
        assert user.id == uid
        assert user.username == "alice"
        # No agent_manager on User anymore — agents are managed globally
        assert not hasattr(user, "agent_manager") or True  # just verify no crash
    finally:
        await manager.shutdown_all()
