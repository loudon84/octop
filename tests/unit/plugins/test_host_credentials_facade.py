from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError
from octop.infra.agents.plugins.host_contracts import CredentialActivation, RecordWrite
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
def services(db: SqlitePool, tmp_path: Path):
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


@pytest.mark.asyncio
async def test_facade_records_use_live_provider_and_return_independent_json(
    services: object,
) -> None:
    current: list[object | None] = [None]
    host = PluginHost(lambda: current[0])
    facade = host.bind("demo-plugin", "1")
    with pytest.raises(PluginHostError) as exc:
        await facade.get_record("prefs", "theme")
    assert exc.value.code == "unavailable"

    current[0] = services
    owner_id = UserRepo(services.db).create(username="owner", role="user")  # type: ignore[attr-defined]
    writes = [RecordWrite("prefs", "theme", owner_id, {"nested": [1]}, 0)]
    rows = await facade.commit(writes)
    assert rows[0].revision == 1
    rows[0].payload["nested"].append(2)
    got = await facade.get_record("prefs", "theme")
    assert got is not None and got.payload == {"nested": [1]}
    listed = await facade.list_records("prefs", "th")
    assert [row.logical_key for row in listed] == ["theme"]


@pytest.mark.asyncio
async def test_commit_cas_and_credential_activation_are_atomic(services: object) -> None:
    owner_id = UserRepo(services.db).create(username="owner", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    facade = host.bind("demo-plugin", "1")
    credential_id = await facade.stage_credential(owner_id, {"api_key": "secret-value"})
    with pytest.raises(PluginHostError) as exc:
        await facade.resolve_credential(credential_id, owner_id)
    assert exc.value.code == "not_found"

    ciphertext = services.plugin_credential_repo.get(
        "demo-plugin", credential_id, owner_id
    ).ciphertext  # type: ignore[attr-defined]
    assert b"secret-value" not in ciphertext

    await facade.commit(
        [RecordWrite("settings", "binding", owner_id, {"credential_id": credential_id}, 0)],
        [CredentialActivation(credential_id, owner_id, 1)],
    )
    assert await facade.resolve_credential(credential_id, owner_id) == {"api_key": "secret-value"}

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("settings", "new", owner_id, {}, 0)],
            [CredentialActivation(credential_id, owner_id, 1)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("settings", "new") is None


@pytest.mark.asyncio
async def test_preallocated_credential_ids_are_validated_and_never_overwritten(
    services: object,
) -> None:
    owner_id = UserRepo(services.db).create(username="preallocated-owner", role="user")  # type: ignore[attr-defined]
    other_id = UserRepo(services.db).create(username="preallocated-other", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    facade = host.bind("preallocated-plugin", "1")
    other_plugin = host.bind("preallocated-other-plugin", "1")

    credential_id = str(uuid4())
    staged_id = await facade.stage_credential(
        owner_id, {"token": "preallocated"}, credential_id=credential_id
    )
    assert staged_id == credential_id
    staged = services.plugin_credential_repo.get(  # type: ignore[attr-defined]
        "preallocated-plugin", credential_id, owner_id
    )
    assert staged is not None and staged.state == "staged"
    ciphertext = staged.ciphertext
    assert b"preallocated" not in ciphertext
    with pytest.raises(PluginHostError) as exc:
        await facade.resolve_credential(credential_id, owner_id)
    assert exc.value.code == "not_found"

    for invalid_id in (True, "", "not-a-uuid", credential_id.upper()):
        with pytest.raises(PluginHostError) as exc:
            await facade.stage_credential(
                owner_id,
                {"token": "must-not-write"},
                credential_id=invalid_id,  # type: ignore[arg-type]
            )
        assert exc.value.code == "invalid"

    for target_facade, target_owner in (
        (facade, owner_id),
        (facade, other_id),
        (other_plugin, owner_id),
    ):
        with pytest.raises(PluginHostError) as exc:
            await target_facade.stage_credential(
                target_owner, {"token": "must-not-overwrite"}, credential_id=credential_id
            )
        assert exc.value.code == "conflict"
    staged = services.plugin_credential_repo.get(  # type: ignore[attr-defined]
        "preallocated-plugin", credential_id, owner_id
    )
    assert staged is not None and staged.state == "staged" and staged.ciphertext == ciphertext

    await facade.commit([], [CredentialActivation(credential_id, owner_id, 1)])
    active_ciphertext = services.plugin_credential_repo.get(  # type: ignore[attr-defined]
        "preallocated-plugin", credential_id, owner_id
    ).ciphertext
    with pytest.raises(PluginHostError) as exc:
        await facade.stage_credential(
            owner_id, {"token": "must-not-overwrite-active"}, credential_id=credential_id
        )
    assert exc.value.code == "conflict"
    active = services.plugin_credential_repo.get(  # type: ignore[attr-defined]
        "preallocated-plugin", credential_id, owner_id
    )
    assert (
        active is not None and active.state == "active" and active.ciphertext == active_ciphertext
    )
    assert await facade.resolve_credential(credential_id, owner_id) == {"token": "preallocated"}


@pytest.mark.asyncio
async def test_record_and_activation_revisions_require_strict_integers(
    services: object,
) -> None:
    owner_id = UserRepo(services.db).create(username="revision-owner", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    facade = host.bind("revision-plugin", "1")
    credential_id = await facade.stage_credential(owner_id, {"token": "strict"})

    for revision in (True, 1.0):
        with pytest.raises(PluginHostError) as exc:
            await facade.commit(
                [],
                [CredentialActivation(credential_id, owner_id, revision)],  # type: ignore[arg-type]
            )
        assert exc.value.code == "invalid"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("settings", "float-revision", owner_id, {}, 0.0)]  # type: ignore[arg-type]
        )
    assert exc.value.code == "invalid"
    assert (
        services.plugin_credential_repo.get(  # type: ignore[attr-defined]
            "revision-plugin", credential_id, owner_id
        ).state
        == "staged"
    )

    await facade.commit([], [CredentialActivation(credential_id, owner_id, 1)])
    assert await facade.resolve_credential(credential_id, owner_id) == {"token": "strict"}


@pytest.mark.asyncio
async def test_deactivate_waits_until_commit_reaches_database_commit_boundary(
    services: object,
) -> None:
    class PausingPool:
        def __init__(self, pool: SqlitePool) -> None:
            self._pool = pool
            self.before_commit = threading.Event()
            self.allow_commit = threading.Event()

        @contextmanager
        def transaction(self) -> Iterator[Any]:
            with self._pool.transaction() as conn:
                yield conn
                self.before_commit.set()
                if not self.allow_commit.wait(3):
                    raise TimeoutError("test did not release transaction")

    owner_id = UserRepo(services.db).create(username="race-owner", role="user")  # type: ignore[attr-defined]
    pause_db = PausingPool(services.db)  # type: ignore[attr-defined]
    pausing_services = SimpleNamespace(
        db=pause_db,
        plugin_record_repo=services.plugin_record_repo,  # type: ignore[attr-defined]
        plugin_credential_repo=services.plugin_credential_repo,  # type: ignore[attr-defined]
        secret_repo=services.secret_repo,  # type: ignore[attr-defined]
    )
    host = PluginHost(lambda: pausing_services)
    facade = host.bind("race-plugin", "1")
    committing = asyncio.create_task(
        facade.commit([RecordWrite("prefs", "linearized", owner_id, {"ok": True}, 0)])
    )
    assert await asyncio.to_thread(pause_db.before_commit.wait, 2)
    _assert_operation_lock_held(host)

    deactivate_started = threading.Event()
    deactivate_finished = threading.Event()

    def deactivate() -> None:
        deactivate_started.set()
        host.deactivate("race-plugin")
        deactivate_finished.set()

    stopping = asyncio.create_task(asyncio.to_thread(deactivate))
    assert await asyncio.to_thread(deactivate_started.wait, 2)
    pause_db.allow_commit.set()
    committed = await committing
    await stopping
    assert deactivate_finished.is_set()
    assert committed[0].logical_key == "linearized"
    assert services.plugin_record_repo.get("race-plugin", "prefs", "linearized") is not None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_database_lease_does_not_block_event_loop_state_checks(
    services: object,
) -> None:
    class PausingPool:
        def __init__(self, pool: SqlitePool) -> None:
            self._pool = pool
            self.before_commit = threading.Event()
            self.allow_commit = threading.Event()

        @contextmanager
        def transaction(self) -> Iterator[Any]:
            with self._pool.transaction() as conn:
                yield conn
                self.before_commit.set()
                if not self.allow_commit.wait(4):
                    raise TimeoutError("test did not release transaction")

    owner_id = UserRepo(services.db).create(username="loop-owner", role="user")  # type: ignore[attr-defined]
    pause_db = PausingPool(services.db)  # type: ignore[attr-defined]
    pausing_services = SimpleNamespace(
        db=pause_db,
        plugin_record_repo=services.plugin_record_repo,  # type: ignore[attr-defined]
        plugin_credential_repo=services.plugin_credential_repo,  # type: ignore[attr-defined]
        secret_repo=services.secret_repo,  # type: ignore[attr-defined]
    )
    host = PluginHost(lambda: pausing_services)
    facade = host.bind("loop-plugin", "1")
    committing = asyncio.create_task(
        facade.commit([RecordWrite("prefs", "heartbeat", owner_id, {}, 0)])
    )
    assert await asyncio.to_thread(pause_db.before_commit.wait, 2)

    loop_progress = threading.Event()
    watcher_done = threading.Event()
    observed: list[bool] = []

    def watch_loop() -> None:
        observed.append(loop_progress.wait(0.25))
        watcher_done.set()

    watcher = threading.Thread(target=watch_loop)
    watcher.start()
    watchdog = threading.Timer(2, pause_db.allow_commit.set)
    watchdog.start()

    async def heartbeat() -> None:
        loop_progress.set()

    try:
        reading = asyncio.create_task(facade.get_record("prefs", "heartbeat"))
        asyncio.create_task(heartbeat())
        assert await asyncio.to_thread(watcher_done.wait, 3)
    finally:
        pause_db.allow_commit.set()
        watchdog.cancel()

    await committing
    assert await reading is not None
    await asyncio.to_thread(watcher.join, 1)
    assert observed == [True]


async def _deactivate_while_paused(
    host: PluginHost,
    plugin_id: str,
    paused: threading.Event,
    resume: threading.Event,
) -> None:
    assert await asyncio.to_thread(paused.wait, 2)
    _assert_operation_lock_held(host)
    started = threading.Event()
    finished = threading.Event()

    def deactivate() -> None:
        started.set()
        host.deactivate(plugin_id)
        finished.set()

    stopping = asyncio.create_task(asyncio.to_thread(deactivate))
    assert await asyncio.to_thread(started.wait, 2)
    resume.set()
    await stopping
    assert finished.is_set()


def _assert_operation_lock_held(host: PluginHost) -> None:
    acquired = host._operation_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    if acquired:
        host._operation_lock.release()  # type: ignore[attr-defined]
    assert not acquired


@pytest.mark.asyncio
async def test_deactivate_waits_for_staged_credential_write(
    services: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = services.plugin_credential_repo  # type: ignore[attr-defined]
    original_stage = repo.stage
    staged = threading.Event()
    resume = threading.Event()

    def stage_then_pause(
        plugin_id: str,
        owner_id: int,
        ciphertext: bytes,
        *,
        credential_id: str | None = None,
        conn: Any = None,
    ) -> Any:
        if conn is not None:
            return original_stage(
                plugin_id, owner_id, ciphertext, credential_id=credential_id, conn=conn
            )
        row = original_stage(plugin_id, owner_id, ciphertext, credential_id=credential_id)
        staged.set()
        if not resume.wait(3):
            raise TimeoutError("test did not resume staged write")
        return row

    monkeypatch.setattr(repo, "stage", stage_then_pause)
    owner_id = UserRepo(services.db).create(username="stage-race", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    facade = host.bind("stage-plugin", "1")
    staging = asyncio.create_task(facade.stage_credential(owner_id, {"token": "race"}))
    await _deactivate_while_paused(host, "stage-plugin", staged, resume)
    credential_id = await staging
    assert repo.get("stage-plugin", credential_id, owner_id) is not None


@pytest.mark.asyncio
async def test_deactivate_waits_for_staged_credential_delete_commit(
    services: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id = UserRepo(services.db).create(username="delete-race", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    facade = host.bind("delete-plugin", "1")
    credential_id = await facade.stage_credential(owner_id, {"token": "race"})
    repo = services.plugin_credential_repo  # type: ignore[attr-defined]
    original_delete = repo.delete
    deleted = threading.Event()
    resume = threading.Event()

    def delete_then_pause(*args: Any, **kwargs: Any) -> bool:
        result = original_delete(*args, **kwargs)
        deleted.set()
        if not resume.wait(3):
            raise TimeoutError("test did not resume staged delete")
        return result

    monkeypatch.setattr(repo, "delete", delete_then_pause)
    releasing = asyncio.create_task(facade.release_staged_credential(credential_id, owner_id))
    await _deactivate_while_paused(host, "delete-plugin", deleted, resume)
    assert await releasing is True
    assert repo.get("delete-plugin", credential_id, owner_id) is None


@pytest.mark.asyncio
async def test_credentials_are_scoped_to_plugin_owner_and_staged_release_only(
    services: object,
) -> None:
    owner_id = UserRepo(services.db).create(username="owner", role="user")  # type: ignore[attr-defined]
    other_id = UserRepo(services.db).create(username="other", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    owner_facade = host.bind("demo-plugin", "1")
    other_plugin = host.bind("other-plugin", "1")
    staged_id = await owner_facade.stage_credential(owner_id, {"token": "staged"})
    assert await owner_facade.release_staged_credential(staged_id, other_id) is False
    with pytest.raises(PluginHostError) as exc:
        await other_plugin.resolve_credential(staged_id, owner_id)
    assert exc.value.code == "not_found"
    await owner_facade.commit([], [CredentialActivation(staged_id, owner_id, 1)])
    with pytest.raises(PluginHostError):
        await owner_facade.resolve_credential(staged_id, other_id)
    assert await owner_facade.resolve_credential(staged_id, owner_id) == {"token": "staged"}
    active_id = await owner_facade.stage_credential(owner_id, {"token": "active"})
    await owner_facade.commit([], [CredentialActivation(active_id, owner_id, 1)])
    assert await owner_facade.release_staged_credential(active_id, owner_id) is False
    with pytest.raises(PluginHostError) as exc:
        await owner_facade.resolve_credential(active_id, other_id)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_record_owner_is_management_metadata_and_cas_owner_cannot_change(
    services: object,
) -> None:
    owner_id = UserRepo(services.db).create(username="owner", role="user")  # type: ignore[attr-defined]
    another_id = UserRepo(services.db).create(username="another", role="user")  # type: ignore[attr-defined]
    host = PluginHost(lambda: services)
    facade = host.bind("demo-plugin", "1")
    await facade.commit([RecordWrite("shared", "connection", owner_id, {"ok": True}, 0)])
    assert (await facade.get_record("shared", "connection")).owner_user_id == owner_id  # type: ignore[union-attr]
    with pytest.raises(PluginHostError) as exc:
        await facade.commit([RecordWrite("shared", "connection", another_id, {}, 1)])
    assert exc.value.code == "conflict"


@pytest.mark.asyncio
async def test_facade_rejects_reserved_namespace_and_old_generation_writes(
    services: object,
) -> None:
    host = PluginHost(lambda: services)
    facade = host.bind("demo-plugin", "1")
    owner_id = UserRepo(services.db).create(username="owner", role="user")  # type: ignore[attr-defined]
    with pytest.raises(PluginHostError) as exc:
        await facade.get_record("@octop.required", "x")
    assert exc.value.code == "denied"
    replacement = host.bind("demo-plugin", "2")
    with pytest.raises(PluginHostError):
        await facade.commit([RecordWrite("prefs", "x", owner_id, {}, 0)])
    assert await replacement.get_record("prefs", "x") is None
