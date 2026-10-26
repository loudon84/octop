"""tests/unit/test_repo_users.py"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserIdentityRow, UserRepo, UserRow


@pytest.fixture
def repo(tmp_path: Path) -> UserRepo:
    db = SqlitePool(tmp_path / "x.db")
    run_migrations(db)
    return UserRepo(db)


def test_create_and_get(repo: UserRepo):
    uid = repo.create(username="alice", password_hash="h", role="admin", display_name="Alice")
    row = repo.get(uid)
    assert isinstance(row, UserRow)
    assert row.username == "alice"
    assert row.role == "admin"
    assert row.disabled == 0
    assert row.created_at <= int(time.time())


def test_get_by_username(repo: UserRepo):
    repo.create(username="bob", password_hash="h", role="user")
    assert repo.get_by_username("bob").username == "bob"
    assert repo.get_by_username("nobody") is None


def test_unique_username(repo: UserRepo):
    repo.create(username="a", password_hash="h", role="user")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(username="a", password_hash="h2", role="user")


def test_list_filters_disabled(repo: UserRepo):
    a = repo.create(username="a", password_hash="h", role="user")
    repo.create(username="b", password_hash="h", role="user")
    repo.set_disabled(a, True)
    rows = repo.list(include_disabled=False)
    assert [r.username for r in rows] == ["b"]
    assert len(repo.list(include_disabled=True)) == 2


def test_set_role(repo: UserRepo):
    uid = repo.create(username="a", password_hash="h", role="user")
    repo.set_role(uid, "admin")
    assert repo.get(uid).role == "admin"


def test_set_password_hash(repo: UserRepo):
    uid = repo.create(username="a", password_hash="h1", role="user")
    repo.set_password_hash(uid, "h2")
    assert repo.get(uid).password_hash == "h2"


def test_delete(repo: UserRepo):
    uid = repo.create(username="a", password_hash="h", role="user")
    repo.delete(uid)
    assert repo.get(uid) is None


def test_count(repo: UserRepo):
    assert repo.count() == 0
    repo.create(username="a", password_hash="h", role="user")
    assert repo.count() == 1


def test_identity_projection_is_narrow_sorted_and_reuses_connection(repo: UserRepo) -> None:
    first = repo.create(
        username="identity-a", password_hash="secret-a", role="user", email="A@EXAMPLE.COM"
    )
    second = repo.create(username="identity-b", password_hash="secret-b", role="user")
    repo.set_disabled(first, True)
    with repo._db.connect() as conn:
        rows = repo.list_identity_page(0, 1, conn=conn)
        row = repo.get_identity(second, conn=conn)
    with repo._db.transaction() as conn:
        locked = repo.lock_identity_rows(conn, [second, first])

        class RecordingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append(sql)
                return self.wrapped.execute(sql, params)

        recording = RecordingConnection(conn)
        repo.get_identity(first, conn=recording)
        repo.list_identity_page(0, 2, conn=recording)

    assert rows == [UserIdentityRow(first, "A@EXAMPLE.COM", True)]
    assert row == UserIdentityRow(second, None, False)
    assert set(UserIdentityRow.__dataclass_fields__) == {"user_id", "email", "disabled"}
    assert [item.user_id for item in repo.list_identity_page(0, 10)] == [first, second]
    assert [item.user_id for item in locked] == [first, second]
    assert len(recording.statements) == 2
    assert all(
        "SELECT id, email, disabled" in sql and "SELECT *" not in sql
        for sql in recording.statements
    )
