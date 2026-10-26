from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError, register_host_setup
from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    PluginActor,
    RecordCheck,
    RecordWrite,
    RequiredExtensionWrite,
    RetainedRecordCheck,
    RetainedRecordWrite,
    UserIdentityCheck,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.plugin_retained_records import PluginRetainedRecordRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def services(db: SqlitePool, tmp_path: Path):
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


def _facade(services, *, identity: bool = False):
    plugin_id = f"retained-{uuid4().hex}"
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest(plugin_id, "1", plugin_id, "skill", "main.py"),
        source_path=Path(".").resolve(),
    )
    if identity:
        register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    return host.bind_context(context, "1")


@pytest.mark.asyncio
async def test_retained_facade_cas_pages_copy_payload_and_keep_ordinary_order(services) -> None:
    facade = _facade(services, identity=True)
    owner = UserRepo(services.db).create(username=f"ordinary-{uuid4().hex}", role="user")
    identity = await facade.read_user_identity(owner)
    ordinary = await facade.commit(
        [
            RecordWrite("state", "first", owner, {"n": 1}, 0),
            RecordWrite("state", "second", owner, {"n": 2}, 0),
        ],
        identity_checks=[UserIdentityCheck(identity)],
        retained_writes=[RetainedRecordWrite("proof", "a%_1", {"step": "intent"}, 0)],
    )
    assert [row.logical_key for row in ordinary] == ["first", "second"]
    assert [row.revision for row in ordinary] == [1, 1]

    record = await facade.get_retained_record("proof", "a%_1")
    assert record is not None and record.revision == 1
    record.payload["step"] = "caller mutation"
    assert (await facade.get_retained_record("proof", "a%_1")).payload == {"step": "intent"}

    page = await facade.list_retained_records("proof", prefix="a%_", limit=1)
    assert [row.logical_key for row in page.items] == ["a%_1"]
    assert page.next_logical_key is None
    with pytest.raises(PluginHostError) as exc:
        await facade.list_retained_records("proof", limit=True)
    assert exc.value.code == "invalid"


@pytest.mark.asyncio
async def test_retained_checks_and_owner_coverage_fail_before_writes(services) -> None:
    owner = UserRepo(services.db).create(username=f"owner-{uuid4().hex}", role="user")
    facade = _facade(services, identity=True)
    snapshot = await facade.read_user_identity(owner)
    await facade.commit([], retained_writes=[RetainedRecordWrite("proof", "key", {}, 0)])

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("state", "uncovered", owner, {}, 0)],
            retained_writes=[RetainedRecordWrite("proof", "second", {}, 0)],
        )
    assert exc.value.code == "invalid"
    assert await facade.get_record("state", "uncovered") is None
    assert await facade.get_retained_record("proof", "second") is None

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("state", "covered", owner, {}, 0)],
            identity_checks=[UserIdentityCheck(snapshot)],
            retained_writes=[RetainedRecordWrite("proof", "third", {}, 0)],
            retained_checks=[RetainedRecordCheck("proof", "missing", 1)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("state", "covered") is None
    assert await facade.get_retained_record("proof", "third") is None

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            retained_writes=[
                RetainedRecordWrite("proof", "would-rollback", {"saved": True}, 0),
                RetainedRecordWrite("proof", "key", {"lost": True}, 9),
            ],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_retained_record("proof", "would-rollback") is None


@pytest.mark.asyncio
async def test_retained_path_rejects_requirements_and_mismatched_checks(services) -> None:
    facade = _facade(services, identity=True)
    actor = PluginActor(1, False, agent_id="agent-1")
    requirement = RequiredExtensionWrite(actor, "agent-1", {}, 0)
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            requirements=[requirement],
            retained_writes=[RetainedRecordWrite("proof", "key", {}, 0)],
        )
    assert exc.value.code == "invalid"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            retained_writes=[RetainedRecordWrite("proof", "key", {}, 0)],
            retained_checks=[RetainedRecordCheck("proof", "key", 1)],
        )
    assert exc.value.code == "invalid"
    assert await facade.get_retained_record("proof", "key") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "raw_value"),
    [
        ("get", "{"),
        ("list", '{"format_version":99,"revision":1,"payload":{}}'),
        ("check", '{"format_version":1,"revision":1,"payload":[]}'),
        ("create", '{"format_version":1,"revision":0,"payload":{}}'),
        (
            "update",
            '{"format_version":1,"revision":1,"revision":2,"payload":{}}',
        ),
    ],
)
async def test_corrupt_retained_envelopes_are_unavailable_on_all_host_paths(
    services, operation: str, raw_value: str
) -> None:
    facade = _facade(services)
    logical_key = "corrupt"
    storage_key = "octop.plugin-retained.v1:" + ":".join(
        part.encode("utf-8").hex() for part in (facade.plugin_id, "proof", logical_key)
    )
    with services.db.transaction() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (storage_key, raw_value))

    with pytest.raises(PluginHostError) as exc:
        if operation == "get":
            await facade.get_retained_record("proof", logical_key)
        elif operation == "list":
            await facade.list_retained_records("proof")
        elif operation == "check":
            await facade.commit([], retained_checks=[RetainedRecordCheck("proof", logical_key, 1)])
        elif operation == "create":
            await facade.commit(
                [], retained_writes=[RetainedRecordWrite("proof", logical_key, {}, 0)]
            )
        else:
            await facade.commit(
                [], retained_writes=[RetainedRecordWrite("proof", logical_key, {}, 1)]
            )
    assert exc.value.code == "unavailable"
    with services.db.connect() as conn:
        stored = conn.execute("SELECT value FROM settings WHERE key = ?", (storage_key,)).fetchone()
    assert stored is not None and stored["value"] == raw_value


@pytest.mark.asyncio
async def test_retained_create_against_valid_existing_row_is_conflict(services) -> None:
    facade = _facade(services)
    await facade.commit([], retained_writes=[RetainedRecordWrite("proof", "existing", {}, 0)])
    with pytest.raises(PluginHostError) as exc:
        await facade.commit([], retained_writes=[RetainedRecordWrite("proof", "existing", {}, 0)])
    assert exc.value.code == "conflict"
    row = await facade.get_retained_record("proof", "existing")
    assert row is not None and row.revision == 1 and row.payload == {}


@pytest.mark.asyncio
async def test_retained_record_survives_plugin_rebind_and_old_facade_is_stale(services) -> None:
    plugin_id = f"retained-rebind-{uuid4().hex}"
    host = PluginHost(lambda: services)
    old = host.bind(plugin_id, "1")
    await old.commit([], retained_writes=[RetainedRecordWrite("proof", "receipt", {"v": 1}, 0)])
    await host.deactivate_and_wait(plugin_id)
    current = host.bind(plugin_id, "2")

    with pytest.raises(PluginHostError) as exc:
        await old.get_retained_record("proof", "receipt")
    assert exc.value.code == "unavailable"
    retained = await current.get_retained_record("proof", "receipt")
    assert retained is not None and retained.payload == {"v": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_retained_proof_survives_user_deletion_while_records_and_credentials_cascade(
    services, role: str
) -> None:
    user_id = UserRepo(services.db).create(username=f"deleted-{role}-{uuid4().hex}", role=role)
    facade = _facade(services, identity=True)
    active_credential_id = await facade.stage_credential(user_id, {"key": "synthetic-active"})
    staged_credential_id = await facade.stage_credential(user_id, {"key": "synthetic-staged"})
    # Retained commits require identity coverage for every ordinary and credential owner.
    identity = await facade.read_user_identity(user_id)
    await facade.commit(
        [RecordWrite("state", "owned", user_id, {"alive": True}, 0)],
        [CredentialActivation(active_credential_id, user_id, 1)],
        identity_checks=[UserIdentityCheck(identity)],
    )
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite("provision_intents", "intent", {"owner": user_id}, 0),
            RetainedRecordWrite("provision_receipts", "receipt", {"owner": user_id}, 0),
            RetainedRecordWrite("provision_claims", "reverse-claim", {"owner": user_id}, 0),
        ],
    )

    UserRepo(services.db).delete(user_id)

    assert await facade.get_record("state", "owned") is None
    assert (
        services.plugin_credential_repo.get(facade.plugin_id, active_credential_id, user_id) is None
    )
    assert (
        services.plugin_credential_repo.get(facade.plugin_id, staged_credential_id, user_id) is None
    )
    assert (await facade.get_retained_record("provision_intents", "intent")).payload == {
        "owner": user_id
    }
    assert (await facade.get_retained_record("provision_receipts", "receipt")).payload == {
        "owner": user_id
    }
    assert (await facade.get_retained_record("provision_claims", "reverse-claim")).payload == {
        "owner": user_id
    }
    assert isinstance(services.plugin_retained_record_repo, PluginRetainedRecordRepo)


async def _mixed_fixture(services):
    owner = UserRepo(services.db).create(
        username=f"mixed-{uuid4().hex}", role="user", email=f"{uuid4().hex}@example.test"
    )
    facade = _facade(services, identity=True)
    old_active = await facade.stage_credential(owner, {"key": "old-active"})
    new_staged = await facade.stage_credential(owner, {"key": "new-staged"})
    staged_to_delete = await facade.stage_credential(owner, {"key": "state-mismatch"})
    identity = await facade.read_user_identity(owner)
    await facade.commit(
        [RecordWrite("state", "guard", owner, {"baseline": True}, 0)],
        [CredentialActivation(old_active, owner, 1)],
        identity_checks=[UserIdentityCheck(identity)],
        retained_writes=[RetainedRecordWrite("proof", "intent", {"phase": "intent"}, 0)],
    )
    return owner, facade, old_active, new_staged, staged_to_delete, identity


def _credential_snapshot(services, facade, owner, *credential_ids: str):
    return {
        credential_id: services.plugin_credential_repo.get(facade.plugin_id, credential_id, owner)
        for credential_id in credential_ids
    }


@pytest.mark.asyncio
async def test_mixed_retained_ordinary_identity_and_credential_commit_succeeds(services) -> None:
    owner, facade, old_active, new_staged, _, identity = await _mixed_fixture(services)
    result = await facade.commit(
        [RecordWrite("state", "ready", owner, {"ready": True}, 0)],
        [CredentialActivation(new_staged, owner, 1)],
        identity_checks=[UserIdentityCheck(identity)],
        checks=[RecordCheck("state", "guard", 1)],
        credential_deletions=[CredentialDeletion(old_active, owner, 2)],
        retained_writes=[RetainedRecordWrite("proof", "intent", {"phase": "receipt"}, 1)],
        retained_checks=[RetainedRecordCheck("proof", "intent", 1)],
    )
    assert [(row.logical_key, row.revision) for row in result] == [("ready", 1)]
    assert (await facade.get_record("state", "ready")).payload == {"ready": True}
    retained = await facade.get_retained_record("proof", "intent")
    assert retained is not None and retained.revision == 2
    assert retained.payload == {"phase": "receipt"}
    assert services.plugin_credential_repo.get(facade.plugin_id, old_active, owner) is None
    activated = services.plugin_credential_repo.get(facade.plugin_id, new_staged, owner)
    assert activated is not None and activated.state == "active" and activated.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "ordinary_check",
        "ordinary_cas",
        "retained_check",
        "retained_cas_after_first_retained_write",
        "stale_identity_email",
        "stale_identity_disabled",
        "activation_revision",
        "deletion_revision",
        "deletion_state",
    ],
)
async def test_mixed_commit_failures_leave_ordinary_retained_and_credentials_unchanged(
    services, failure: str
) -> None:
    owner, facade, old_active, new_staged, staged_to_delete, identity = await _mixed_fixture(
        services
    )
    ordinary_writes = [RecordWrite("state", "ready", owner, {"ready": True}, 0)]
    ordinary_checks = [RecordCheck("state", "guard", 1)]
    retained_writes = [RetainedRecordWrite("proof", "intent", {"phase": "receipt"}, 1)]
    retained_checks = [RetainedRecordCheck("proof", "intent", 1)]
    activation_revision = 1
    deletion = CredentialDeletion(old_active, owner, 2)
    if failure == "ordinary_check":
        ordinary_checks = [RecordCheck("state", "guard", 99)]
    elif failure == "ordinary_cas":
        ordinary_writes.append(RecordWrite("state", "guard", owner, {"lost": True}, 99))
    elif failure == "retained_check":
        retained_checks = [RetainedRecordCheck("proof", "missing-check", 1)]
    elif failure == "retained_cas_after_first_retained_write":
        retained_checks = []
        retained_writes = [
            RetainedRecordWrite("proof", "receipt", {"phase": "receipt"}, 0),
            RetainedRecordWrite("proof", "intent", {"lost": True}, 99),
        ]
    elif failure == "activation_revision":
        activation_revision = 99
    elif failure == "deletion_revision":
        deletion = CredentialDeletion(old_active, owner, 99)
    elif failure == "deletion_state":
        deletion = CredentialDeletion(staged_to_delete, owner, 1)
    elif failure == "stale_identity_email":
        with services.db.transaction() as conn:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", ("changed@example.test", owner))
    elif failure == "stale_identity_disabled":
        UserRepo(services.db).set_disabled(owner, True)

    before_credentials = _credential_snapshot(
        services, facade, owner, old_active, new_staged, staged_to_delete
    )
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            ordinary_writes,
            [CredentialActivation(new_staged, owner, activation_revision)],
            identity_checks=[UserIdentityCheck(identity)],
            checks=ordinary_checks,
            credential_deletions=[deletion],
            retained_writes=retained_writes,
            retained_checks=retained_checks,
        )
    assert exc.value.code == "conflict"
    guard = await facade.get_record("state", "guard")
    assert guard is not None and guard.revision == 1 and guard.payload == {"baseline": True}
    assert await facade.get_record("state", "ready") is None
    intent = await facade.get_retained_record("proof", "intent")
    assert intent is not None and intent.revision == 1 and intent.payload == {"phase": "intent"}
    assert await facade.get_retained_record("proof", "receipt") is None
    assert (
        _credential_snapshot(services, facade, owner, old_active, new_staged, staged_to_delete)
        == before_credentials
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_change", ["disabled", "deleted"])
async def test_stale_or_deleted_user_cannot_activate_ready_but_can_save_receipt(
    services, identity_change: str
) -> None:
    owner, facade, old_active, new_staged, _, identity = await _mixed_fixture(services)
    if identity_change == "disabled":
        UserRepo(services.db).set_disabled(owner, True)
    else:
        UserRepo(services.db).delete(owner)

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("state", "ready", owner, {"ready": True}, 0)],
            [CredentialActivation(new_staged, owner, 1)],
            identity_checks=[UserIdentityCheck(identity)],
            retained_writes=[RetainedRecordWrite("proof", "would-be-ready", {}, 0)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("state", "ready") is None
    assert await facade.get_retained_record("proof", "would-be-ready") is None
    result = await facade.commit(
        [], retained_writes=[RetainedRecordWrite("proof", "receipt", {"verified": True}, 0)]
    )
    assert result == []
    receipt = await facade.get_retained_record("proof", "receipt")
    assert receipt is not None and receipt.payload == {"verified": True}
    if identity_change == "deleted":
        assert services.plugin_credential_repo.get(facade.plugin_id, old_active, owner) is None
        assert services.plugin_credential_repo.get(facade.plugin_id, new_staged, owner) is None
    else:
        old = services.plugin_credential_repo.get(facade.plugin_id, old_active, owner)
        new = services.plugin_credential_repo.get(facade.plugin_id, new_staged, owner)
        assert old is not None and old.state == "active" and old.revision == 2
        assert new is not None and new.state == "staged" and new.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["activation", "deletion"])
async def test_retained_mixed_credential_owners_must_be_identity_covered_before_writes(
    services, operation: str
) -> None:
    owner = UserRepo(services.db).create(username=f"uncovered-{uuid4().hex}", role="user")
    facade = _facade(services, identity=True)
    credential_id = await facade.stage_credential(owner, {"key": operation})
    expected_revision = 1
    if operation == "deletion":
        await facade.commit([], [CredentialActivation(credential_id, owner, 1)])
        expected_revision = 2

    with pytest.raises(PluginHostError) as exc:
        if operation == "activation":
            await facade.commit(
                [],
                [CredentialActivation(credential_id, owner, expected_revision)],
                retained_writes=[RetainedRecordWrite("proof", "uncovered", {}, 0)],
            )
        else:
            await facade.commit(
                [],
                credential_deletions=[CredentialDeletion(credential_id, owner, expected_revision)],
                retained_writes=[RetainedRecordWrite("proof", "uncovered", {}, 0)],
            )
    assert exc.value.code == "invalid"
    assert await facade.get_retained_record("proof", "uncovered") is None
    credential = services.plugin_credential_repo.get(facade.plugin_id, credential_id, owner)
    assert credential is not None
    assert credential.state == ("staged" if operation == "activation" else "active")


@pytest.mark.asyncio
async def test_retained_requirements_are_invalid_even_for_covered_actor_and_active_plugin(
    services,
) -> None:
    facade = _facade(services, identity=True)
    owner = UserRepo(services.db).create(username=f"covered-req-{uuid4().hex}", role="user")
    identity = await facade.read_user_identity(owner)
    requirement = RequiredExtensionWrite(
        PluginActor(owner, False, agent_id="agent-1"),
        "agent-1",
        {facade.plugin_id: "1"},
        0,
    )
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            identity_checks=[UserIdentityCheck(identity)],
            requirements=[requirement],
            retained_writes=[RetainedRecordWrite("proof", "requirement", {}, 0)],
        )
    assert exc.value.code == "invalid"
    assert await facade.get_retained_record("proof", "requirement") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "check"])
@pytest.mark.parametrize("revision", [True, -1, "0"])
async def test_retained_revisions_reject_bool_negative_and_wrong_type_before_writes(
    services, operation: str, revision: object
) -> None:
    facade = _facade(services)
    with pytest.raises(PluginHostError) as exc:
        if operation == "write":
            await facade.commit(
                [],
                retained_writes=[RetainedRecordWrite("proof", "bad-revision", {}, revision)],
            )
        else:
            await facade.commit(
                [], retained_checks=[RetainedRecordCheck("proof", "bad-revision", revision)]
            )
    assert exc.value.code == "invalid"
    assert await facade.get_retained_record("proof", "bad-revision") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"after_key": 3},
        {"after_key": True},
    ],
)
async def test_retained_page_rejects_invalid_limit_and_cursor_primitives(services, kwargs) -> None:
    facade = _facade(services)
    with pytest.raises(PluginHostError) as exc:
        await facade.list_retained_records("proof", **kwargs)
    assert exc.value.code == "invalid"


@pytest.mark.asyncio
async def test_retained_duplicate_writes_and_disagreeing_checks_reject_without_side_effects(
    services,
) -> None:
    facade = _facade(services)
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            retained_writes=[
                RetainedRecordWrite("proof", "duplicate", {"n": 1}, 0),
                RetainedRecordWrite("proof", "duplicate", {"n": 2}, 0),
            ],
        )
    assert exc.value.code == "invalid"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            retained_checks=[
                RetainedRecordCheck("proof", "duplicate", 0),
                RetainedRecordCheck("proof", "duplicate", 1),
            ],
        )
    assert exc.value.code == "invalid"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            retained_writes=[RetainedRecordWrite("proof", "duplicate", {}, 0)],
            retained_checks=[RetainedRecordCheck("proof", "duplicate", 1)],
        )
    assert exc.value.code == "invalid"
    assert await facade.get_retained_record("proof", "duplicate") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
async def test_cancelled_worker_retained_commit_holds_sqlite_lease_through_finalization(
    services, monkeypatch: pytest.MonkeyPatch, rollback: bool
) -> None:
    owner = UserRepo(services.db).create(
        username=f"worker-retained-{uuid4().hex}", role="user", email="worker@example.test"
    )
    host = PluginHost(lambda: services)
    transaction_body_finished = threading.Event()
    release_finalization = threading.Event()
    transaction_finalized = threading.Event()
    deactivate_entered = threading.Event()
    identity_loaded = asyncio.Event()
    worker_cancelled = asyncio.Event()
    plugin_id = f"worker-retained-{uuid4().hex}"

    async def worker(wake: asyncio.Event, facade) -> None:
        assert wake.is_set()
        identity = await facade.read_user_identity(owner)
        identity_loaded.set()
        try:
            await facade.commit(
                [RecordWrite("worker", "ready", owner, {"ready": True}, 0)],
                identity_checks=[UserIdentityCheck(identity)],
                retained_writes=[RetainedRecordWrite("proof", "receipt", {"ready": True}, 0)],
            )
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    context = PluginContext(
        manifest=PluginManifest(plugin_id, "1", plugin_id, "skill", "main.py"),
        source_path=Path(".").resolve(),
    )

    def setup(facade) -> None:
        facade.register_user_identity_consumer()

        async def registered_worker(wake: asyncio.Event) -> None:
            await worker(wake, facade)

        facade.register_background_worker("commit", registered_worker)

    register_host_setup(context, setup)
    facade = host.bind_context(context, "1")
    original_transaction = services.db.transaction

    @contextmanager
    def controlled_transaction():
        try:
            with original_transaction() as conn:
                yield conn
                transaction_body_finished.set()
                if not release_finalization.wait(timeout=5):
                    raise TimeoutError("transaction finalization release was not signaled")
                if rollback:
                    raise RuntimeError("force transaction rollback")
        finally:
            transaction_finalized.set()

    monkeypatch.setattr(services.db, "transaction", controlled_transaction)
    await host.start_background_workers()
    await asyncio.wait_for(identity_loaded.wait(), 2)
    worker_task = host._plugins[plugin_id].background_workers["commit"].task
    assert worker_task is not None
    lifecycle_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(transaction_body_finished.wait, 2), 3)
        worker_task.cancel()
        await asyncio.wait_for(worker_cancelled.wait(), 2)
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker_task, 2)
        assert not transaction_finalized.is_set()

        original_deactivate = host.deactivate

        def observed_deactivate(current_plugin_id: str):
            deactivate_entered.set()
            return original_deactivate(current_plugin_id)

        monkeypatch.setattr(host, "deactivate", observed_deactivate)
        lifecycle_task = asyncio.create_task(host.deactivate_and_wait(plugin_id))
        assert await asyncio.wait_for(asyncio.to_thread(deactivate_entered.wait, 2), 3)
        lease_acquired = host._operation_lock.acquire(blocking=False)
        if lease_acquired:
            host._operation_lock.release()
        assert not lease_acquired
        assert not transaction_finalized.is_set()
        assert not lifecycle_task.done()

        release_finalization.set()
        assert await asyncio.wait_for(asyncio.to_thread(transaction_finalized.wait, 2), 3)
        await asyncio.wait_for(lifecycle_task, 3)
        lifecycle_task = None

        retained = services.plugin_retained_record_repo.get(plugin_id, "proof", "receipt")
        ordinary = services.plugin_record_repo.get(plugin_id, "worker", "ready")
        if rollback:
            assert retained is None and ordinary is None
        else:
            assert retained is not None and retained.revision == 1
            assert ordinary is not None and ordinary.owner_user_id == owner
        with pytest.raises(PluginHostError) as exc:
            await facade.commit([], retained_writes=[RetainedRecordWrite("proof", "late", {}, 0)])
        assert exc.value.code == "unavailable"
    finally:
        release_finalization.set()
        if transaction_body_finished.is_set() and not transaction_finalized.is_set():
            assert await asyncio.wait_for(asyncio.to_thread(transaction_finalized.wait, 2), 3)
        if lifecycle_task is not None:
            await asyncio.wait_for(asyncio.gather(lifecycle_task, return_exceptions=True), 3)
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.wait_for(asyncio.gather(worker_task, return_exceptions=True), 3)
        await host.close_and_wait()
