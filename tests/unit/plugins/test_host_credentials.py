from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.plugin_credentials import PluginCredentialRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def test_credentials_stage_activate_and_restrict_plugin_and_owner(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="owner", role="user")
    other_owner = UserRepo(db).create(username="other", role="user")
    repo = PluginCredentialRepo(db)
    ciphertext = b"already-encrypted\x00bytes"
    staged = repo.stage("plugin-a", owner_id, ciphertext)

    assert staged.state == "staged"
    assert staged.revision == 1
    assert staged.ciphertext == ciphertext
    assert staged.pk > 0
    assert staged.pk != staged.id
    assert repo.get("plugin-b", staged.id, owner_id) is None
    assert repo.get("plugin-a", staged.id, other_owner) is None
    assert (
        repo.activate(
            "plugin-a",
            staged.id,
            owner_id,
            expected_revision=2,
        )
        is None
    )

    active = repo.activate(
        "plugin-a",
        staged.id,
        owner_id,
        expected_revision=1,
    )
    assert active is not None
    assert active.state == "active"
    assert active.revision == 2
    assert active.ciphertext == ciphertext
    assert repo.delete("plugin-b", staged.id, owner_id) is False
    assert repo.delete("plugin-a", staged.id, other_owner) is False
    assert repo.delete("plugin-a", staged.id, owner_id) is True
    assert repo.get("plugin-a", staged.id, owner_id) is None


def test_credentials_can_stage_preallocated_id_and_reject_collision(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="preallocated-owner", role="user")
    other_owner = UserRepo(db).create(username="preallocated-other", role="user")
    repo = PluginCredentialRepo(db)
    credential_id = str(uuid4())
    staged = repo.stage("plugin-a", owner_id, b"encrypted-one", credential_id=credential_id)
    assert staged.id == credential_id and staged.state == "staged"
    for plugin_id, owner_user_id in (
        ("plugin-a", owner_id),
        ("plugin-a", other_owner),
        ("plugin-b", owner_id),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            repo.stage(
                plugin_id,
                owner_user_id,
                b"encrypted-two",
                credential_id=credential_id,
            )
        current = repo.get("plugin-a", credential_id, owner_id)
        assert current is not None and current.ciphertext == b"encrypted-one"


def test_delete_active_requires_exact_plugin_owner_revision_and_state(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="active-delete-owner", role="user")
    other_owner = UserRepo(db).create(username="active-delete-other", role="user")
    repo = PluginCredentialRepo(db)
    staged = repo.stage("plugin-a", owner_id, b"staged")
    active = repo.activate("plugin-a", staged.id, owner_id, expected_revision=staged.revision)
    assert active is not None and active.revision == 2

    assert repo.delete_active("plugin-a", active.id, owner_id, expected_revision=1) is False
    assert repo.delete_active("plugin-a", active.id, other_owner, expected_revision=2) is False
    assert repo.delete_active("plugin-b", active.id, owner_id, expected_revision=2) is False
    assert repo.delete_active("plugin-a", "missing", owner_id, expected_revision=2) is False
    current = repo.get("plugin-a", active.id, owner_id)
    assert current is not None and current.state == "active"
    assert repo.delete_active("plugin-a", active.id, owner_id, expected_revision=2) is True
    assert repo.get("plugin-a", active.id, owner_id) is None

    still_staged = repo.stage("plugin-a", owner_id, b"staged-only")
    assert repo.delete_active("plugin-a", still_staged.id, owner_id, expected_revision=1) is False
    assert repo.get("plugin-a", still_staged.id, owner_id) is not None


def test_credential_schema_rejects_unknown_state(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="owner", role="user")
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plugin_credentials("
            "credential_id, plugin_id, owner_user_id, ciphertext, state, revision, "
            "created_at, updated_at"
            ") VALUES ('credential-1', 'plugin-a', ?, ?, 'unknown', 1, 1, 1)",
            (owner_id, b"ciphertext"),
        )
