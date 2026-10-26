from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError, register_host_setup
from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    PluginUserIdentity,
    PluginUserIdentityPage,
    RecordWrite,
    UserIdentityCheck,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def services(db: SqlitePool, tmp_path: Path) -> Any:
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


def _setup_identity_consumer(host: PluginHost, plugin_id: str = "identity-plugin") -> Any:
    context = PluginContext(
        manifest=PluginManifest(
            id=plugin_id,
            version="1",
            name=plugin_id,
            kind="skill",
            entry="main.py",
        ),
        source_path=Path(".").resolve(),
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    return host.bind_context(context, "1")


def _identity(user_id: int, email: str | None, disabled: bool) -> PluginUserIdentity:
    import hashlib
    import json

    token_input = "octop.plugin-user-identity.v1:" + json.dumps(
        [user_id, email, disabled], ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    token = hashlib.sha256(token_input.encode("utf-8")).hexdigest()
    return PluginUserIdentity(user_id, email, disabled, token)


def test_identity_page_copies_mutable_input_items() -> None:
    first = _identity(1, "first@example.test", False)
    second = _identity(2, "second@example.test", False)
    source = [first]
    page = PluginUserIdentityPage(source, None)  # type: ignore[arg-type]

    source.append(second)

    assert page.items == (first,)
    assert isinstance(page.items, tuple)


@pytest.mark.asyncio
async def test_setup_capability_reads_narrow_identities_and_pages_stably(services: Any) -> None:
    repo = UserRepo(services.db)
    first = repo.create(username="identity-a", role="user", email="  A@Example.com ")
    second = repo.create(username="identity-b", role="user")
    third = repo.create(username="identity-c", role="user", email="c@example.com")
    repo.set_disabled(second, True)

    host = PluginHost(lambda: services)
    facade = _setup_identity_consumer(host)
    page = await facade.list_user_identities(limit=2)
    assert isinstance(page, PluginUserIdentityPage)
    assert isinstance(page.items, tuple)
    assert [(item.user_id, item.email, item.disabled) for item in page.items] == [
        (first, "a@example.com", False),
        (second, None, True),
    ]
    assert page.next_user_id == second
    assert page.items[0].snapshot_token == _identity(first, "a@example.com", False).snapshot_token
    with pytest.raises((AttributeError, TypeError)):
        page.items += (page.items[0],)  # type: ignore[misc]

    final_page = await facade.list_user_identities(after_user_id=page.next_user_id, limit=2)
    assert [item.user_id for item in final_page.items] == [third]
    assert final_page.next_user_id is None
    assert (await facade.list_user_identities(after_user_id=third)).items == ()
    assert await facade.read_user_identity(second) == _identity(second, None, True)
    with pytest.raises(PluginHostError) as exc:
        await facade.read_user_identity(999999)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_identity_reads_require_setup_registration_and_current_generation(
    services: Any,
) -> None:
    user_id = UserRepo(services.db).create(username="identity-capability", role="user")
    host = PluginHost(lambda: services)
    bare = host.bind("bare-identity", "1")
    for operation in (
        bare.register_user_identity_consumer,
        lambda: bare.read_user_identity(user_id),
    ):
        with pytest.raises(PluginHostError) as exc:
            result = operation()
            if asyncio.iscoroutine(result):
                await result
        assert exc.value.code == "denied"

    facade = _setup_identity_consumer(host, "stale-identity")
    with pytest.raises(PluginHostError) as exc:
        facade.register_user_identity_consumer()
    assert exc.value.code == "denied"
    replacement = host.bind("stale-identity", "2")
    with pytest.raises(PluginHostError) as exc:
        await facade.read_user_identity(user_id)
    assert exc.value.code == "unavailable"
    assert replacement.plugin_id == "stale-identity"

    for lifecycle in ("deactivate", "close"):
        old = _setup_identity_consumer(host, f"stale-{lifecycle}")
        if lifecycle == "deactivate":
            host.deactivate(old.plugin_id)
        else:
            host.close()
        with pytest.raises(PluginHostError) as exc:
            await old.read_user_identity(user_id)
        assert exc.value.code == "unavailable"


@pytest.mark.asyncio
async def test_own_active_credential_deletion_does_not_require_identity_capability(
    services: Any,
) -> None:
    owner = UserRepo(services.db).create(username="identity-free-delete", role="user")
    facade = PluginHost(lambda: services).bind("identity-free-delete", "1")
    credential_id = await facade.stage_credential(owner, {"key": "own"})
    await facade.commit([], [CredentialActivation(credential_id, owner, 1)])

    with pytest.raises(PluginHostError) as exc:
        await facade.read_user_identity(owner)
    assert exc.value.code == "denied"

    await facade.commit([], credential_deletions=[CredentialDeletion(credential_id, owner, 2)])
    assert services.plugin_credential_repo.get("identity-free-delete", credential_id, owner) is None


def test_identity_consumer_registration_is_once_per_real_setup_window() -> None:
    host = PluginHost(lambda: None)
    context = PluginContext(
        manifest=PluginManifest("duplicate-identity", "1", "Duplicate", "skill", "main.py"),
        source_path=Path(".").resolve(),
    )
    duplicate_codes: list[str] = []

    def setup(facade: Any) -> None:
        facade.register_user_identity_consumer()
        with pytest.raises(PluginHostError) as exc:
            facade.register_user_identity_consumer()
        duplicate_codes.append(exc.value.code)

    register_host_setup(context, setup)
    host.bind_context(context, "1")
    assert duplicate_codes == ["conflict"]


@pytest.mark.asyncio
async def test_identity_inputs_and_checks_are_strict_and_atomic(services: Any) -> None:
    repo = UserRepo(services.db)
    owner = repo.create(username="identity-owner", role="user", email="owner@example.com")
    other = repo.create(username="identity-other", role="user", email="other@example.com")
    host = PluginHost(lambda: services)
    facade = _setup_identity_consumer(host)
    snapshot = await facade.read_user_identity(owner)
    credential_id = await facade.stage_credential(owner, {"key": "new"})

    for user_id in (True, 1.0, 0, -1):
        with pytest.raises(PluginHostError) as exc:
            await facade.read_user_identity(user_id)  # type: ignore[arg-type]
        assert exc.value.code == "invalid"
    for after_user_id, limit in (
        (True, 5),
        (1.0, 5),
        (-1, 5),
        (0, True),
        (0, 1.0),
        (0, 0),
        (0, 101),
    ):
        with pytest.raises(PluginHostError) as exc:
            await facade.list_user_identities(after_user_id=after_user_id, limit=limit)  # type: ignore[arg-type]
        assert exc.value.code == "invalid"

    await facade.commit(
        [RecordWrite("identity", "ready", owner, {"ok": True}, 0)],
        [CredentialActivation(credential_id, owner, 1)],
        identity_checks=[UserIdentityCheck(snapshot)],
    )
    assert await facade.resolve_credential(credential_id, owner) == {"key": "new"}

    changed_credential = await facade.stage_credential(owner, {"key": "changed"})
    repo.set_email(owner, "changed@example.com")
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "changed", owner, {}, 0)],
            [CredentialActivation(changed_credential, owner, 1)],
            identity_checks=[UserIdentityCheck(snapshot)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("identity", "changed") is None
    assert (
        services.plugin_credential_repo.get("identity-plugin", changed_credential, owner).state
        == "staged"
    )

    for suffix in ("disabled-change", "deleted-change"):
        target = repo.create(
            username=f"identity-{suffix}", role="user", email=f"{suffix}@example.com"
        )
        target_snapshot = await facade.read_user_identity(target)
        staged = await facade.stage_credential(target, {"key": suffix})
        if suffix == "disabled-change":
            repo.set_disabled(target, True)
        else:
            repo.delete(target)
        with pytest.raises(PluginHostError) as exc:
            await facade.commit(
                [RecordWrite("identity", suffix, target, {}, 0)],
                [CredentialActivation(staged, target, 1)],
                identity_checks=[UserIdentityCheck(target_snapshot)],
            )
        assert exc.value.code == "conflict"
        assert await facade.get_record("identity", suffix) is None
        if suffix == "disabled-change":
            assert (
                services.plugin_credential_repo.get("identity-plugin", staged, target).state
                == "staged"
            )
        else:
            assert services.plugin_credential_repo.get("identity-plugin", staged, target) is None

    # A disabled snapshot is valid for generic suspension records.
    repo.set_disabled(owner, True)
    disabled_snapshot = await facade.read_user_identity(owner)
    await facade.commit(
        [RecordWrite("identity", "suspended", owner, {"disabled": True}, 0)],
        identity_checks=[UserIdentityCheck(disabled_snapshot)],
    )
    assert await facade.get_record("identity", "suspended") is not None

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "bad-owner", other, {}, 0)],
            identity_checks=[
                UserIdentityCheck(snapshot),
                UserIdentityCheck(_identity(owner, "else@example.com", False)),
            ],
        )
    assert exc.value.code == "invalid"
    assert await facade.get_record("identity", "bad-owner") is None

    # Identical checks coalesce; malformed/forged snapshots fail before writes.
    await facade.commit(
        [RecordWrite("identity", "coalesced", other, {}, 0)],
        identity_checks=[
            UserIdentityCheck(await facade.read_user_identity(other)),
            UserIdentityCheck(await facade.read_user_identity(other)),
        ],
    )
    current = await facade.read_user_identity(other)
    bad = PluginUserIdentity(current.user_id, current.email, current.disabled, "0" * 64)
    malformed = (
        bad,
        PluginUserIdentity(True, None, False, current.snapshot_token),  # type: ignore[arg-type]
        PluginUserIdentity(other, 7, False, current.snapshot_token),  # type: ignore[arg-type]
        PluginUserIdentity(other, None, 0, current.snapshot_token),  # type: ignore[arg-type]
    )
    for index, identity in enumerate(malformed):
        with pytest.raises(PluginHostError) as exc:
            await facade.commit(
                [RecordWrite("identity", f"forged-{index}", other, {}, 0)],
                identity_checks=[UserIdentityCheck(identity)],
            )
        assert exc.value.code == "invalid"
        assert await facade.get_record("identity", f"forged-{index}") is None


@pytest.mark.asyncio
async def test_identity_check_and_active_credential_deletion_commit_together(services: Any) -> None:
    repo = UserRepo(services.db)
    owner = repo.create(username="cleanup-owner", role="user", email="cleanup@example.com")
    other = repo.create(username="cleanup-other", role="user")
    host = PluginHost(lambda: services)
    facade = _setup_identity_consumer(host)
    old_id = await facade.stage_credential(owner, {"key": "old"})
    new_id = await facade.stage_credential(owner, {"key": "new"})
    await facade.commit([], [CredentialActivation(old_id, owner, 1)])
    snapshot = await facade.read_user_identity(owner)
    await facade.commit(
        [RecordWrite("identity", "ready", owner, {"new": new_id}, 0)],
        [CredentialActivation(new_id, owner, 1)],
        identity_checks=[UserIdentityCheck(snapshot)],
        credential_deletions=[CredentialDeletion(old_id, owner, 2)],
    )
    assert services.plugin_credential_repo.get("identity-plugin", old_id, owner) is None
    assert await facade.resolve_credential(new_id, owner) == {"key": "new"}

    await facade.stage_credential(owner, {"key": "doomed"})
    staged_to_activate = await facade.stage_credential(owner, {"key": "will-not-activate"})
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "rollback", owner, {}, 0)],
            [CredentialActivation(staged_to_activate, owner, 1)],
            credential_deletions=[CredentialDeletion(new_id, owner, 1)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("identity", "rollback") is None
    assert (
        services.plugin_credential_repo.get("identity-plugin", staged_to_activate, owner).state
        == "staged"
    )
    assert services.plugin_credential_repo.get("identity-plugin", new_id, owner).state == "active"

    for deletions, activations in (
        ([CredentialDeletion(new_id, owner, 2), CredentialDeletion(new_id, owner, 2)], ()),
        ([CredentialDeletion(new_id, owner, 2)], (CredentialActivation(new_id, owner, 2),)),
    ):
        with pytest.raises(PluginHostError) as exc:
            await facade.commit([], activations, credential_deletions=deletions)
        assert exc.value.code == "invalid"

    first_old_id = await facade.stage_credential(owner, {"key": "first-old"})
    second_old_id = await facade.stage_credential(owner, {"key": "second-old"})
    await facade.commit(
        [],
        [
            CredentialActivation(first_old_id, owner, 1),
            CredentialActivation(second_old_id, owner, 1),
        ],
    )
    pending_id = await facade.stage_credential(owner, {"key": "pending"})
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "deletion-rollback", owner, {}, 0)],
            [CredentialActivation(pending_id, owner, 1)],
            credential_deletions=[
                CredentialDeletion(first_old_id, owner, 2),
                CredentialDeletion(second_old_id, owner, 999),
            ],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("identity", "deletion-rollback") is None
    assert (
        services.plugin_credential_repo.get("identity-plugin", pending_id, owner).state == "staged"
    )
    assert (
        services.plugin_credential_repo.get("identity-plugin", first_old_id, owner).state
        == "active"
    )
    assert (
        services.plugin_credential_repo.get("identity-plugin", second_old_id, owner).state
        == "active"
    )

    for deletion in (
        CredentialDeletion(new_id, other, 2),
        CredentialDeletion("missing-credential", owner, 1),
        CredentialDeletion("", owner, 1),
        CredentialDeletion(new_id, owner, True),  # type: ignore[arg-type]
    ):
        with pytest.raises(PluginHostError) as exc:
            await facade.commit([], credential_deletions=[deletion])
        assert exc.value.code in {"invalid", "conflict"}


@pytest.mark.asyncio
async def test_sqlite_identity_cleanup_holds_real_lease_through_cancelled_commit(
    services: Any, tmp_path: Path
) -> None:
    user_repo = UserRepo(services.db)
    owner = user_repo.create(username="identity-race", role="user", email="race@example.com")
    writer_db = SqlitePool(services.db.path)
    writer_repo = UserRepo(writer_db)

    class PausingPool:
        def __init__(self, pool: SqlitePool) -> None:
            self.pool = pool
            self.at_boundary = threading.Event()
            self.allow_commit = threading.Event()
            self.pause = False

        @contextmanager
        def transaction(self):
            with self.pool.transaction() as conn:
                yield conn
                if self.pause:
                    self.at_boundary.set()
                    if not self.allow_commit.wait(3):
                        raise TimeoutError("test did not release commit")

    pause_db = PausingPool(services.db)
    from types import SimpleNamespace

    pausing_services = SimpleNamespace(
        db=pause_db,
        user_repo=services.user_repo,
        plugin_record_repo=services.plugin_record_repo,
        plugin_credential_repo=services.plugin_credential_repo,
        secret_repo=services.secret_repo,
    )
    host = PluginHost(lambda: pausing_services)
    facade = _setup_identity_consumer(host)
    snapshot = await facade.read_user_identity(owner)
    old_id = await facade.stage_credential(owner, {"key": "old"})
    await facade.commit([], [CredentialActivation(old_id, owner, 1)])
    new_id = await facade.stage_credential(owner, {"key": "new"})
    stale_generation_id = await facade.stage_credential(owner, {"key": "stale-generation"})
    pause_db.pause = True
    commit_task = asyncio.create_task(
        facade.commit(
            [RecordWrite("identity", "race", owner, {"ready": True}, 0)],
            [CredentialActivation(new_id, owner, 1)],
            identity_checks=[UserIdentityCheck(snapshot)],
            credential_deletions=[CredentialDeletion(old_id, owner, 2)],
        )
    )
    writer_started = threading.Event()
    writer_done = threading.Event()

    def update_email() -> None:
        writer_started.set()
        writer_repo.set_email(owner, "after@example.com")
        writer_done.set()

    writer: asyncio.Task[None] | None = None
    stopping: asyncio.Task[None] | None = None
    deactivated = threading.Event()
    deactivate_started = threading.Event()

    def deactivate() -> None:
        deactivate_started.set()
        host.deactivate("identity-plugin")
        deactivated.set()

    try:
        assert await asyncio.to_thread(pause_db.at_boundary.wait, 2)
        commit_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await commit_task

        writer = asyncio.create_task(asyncio.to_thread(update_email))
        assert await asyncio.to_thread(writer_started.wait, 2)
        assert not await asyncio.to_thread(writer_done.wait, 0.2)
        acquired = host._operation_lock.acquire(blocking=False)
        if acquired:
            host._operation_lock.release()
        assert not acquired

        stopping = asyncio.create_task(asyncio.to_thread(deactivate))
        assert await asyncio.to_thread(deactivate_started.wait, 2)
        assert not await asyncio.to_thread(deactivated.wait, 0.2)
    finally:
        pause_db.allow_commit.set()
        if not commit_task.done():
            commit_task.cancel()
        if stopping is None:
            stopping = asyncio.create_task(
                asyncio.to_thread(lambda: host.deactivate("identity-plugin"))
            )
        pending: list[asyncio.Future[Any] | asyncio.Task[Any]] = [commit_task, stopping]
        if writer is not None:
            pending.append(writer)
        await asyncio.gather(*pending, return_exceptions=True)
        writer_db.close()

    assert writer_done.is_set()
    assert deactivated.is_set()
    assert services.plugin_record_repo.get("identity-plugin", "identity", "race") is not None
    assert services.plugin_credential_repo.get("identity-plugin", old_id, owner) is None
    assert services.plugin_credential_repo.get("identity-plugin", new_id, owner).state == "active"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "old-generation", owner, {}, 0)],
            [CredentialActivation(stale_generation_id, owner, 1)],
            credential_deletions=[CredentialDeletion(new_id, owner, 2)],
        )
    assert exc.value.code == "unavailable"
    assert (
        services.plugin_credential_repo.get("identity-plugin", stale_generation_id, owner).state
        == "staged"
    )
    assert services.plugin_credential_repo.get("identity-plugin", new_id, owner).state == "active"
    writer_db.close()


def test_current_value_token_does_not_claim_to_detect_a_to_b_to_a() -> None:
    original = _identity(7, "a@example.com", False)
    changed_away = _identity(7, "b@example.com", False)
    changed_back = _identity(7, "a@example.com", False)
    assert original.snapshot_token == changed_back.snapshot_token
    assert original.snapshot_token != changed_away.snapshot_token
