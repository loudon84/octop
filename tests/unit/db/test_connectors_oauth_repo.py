from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.utils.ulid import new_ulid


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "connectors.db")
    run_migrations(pool)
    return pool


def _state(repo: ConnectorRepo, user_id: int, *, state_id: str | None = None) -> str:
    identifier = state_id or new_ulid()
    repo.create_oauth_state(
        state_id=identifier,
        state=f"state-{identifier}",
        user_id=user_id,
        kind="composio-connect",
        code_verifier="verifier",
    )
    return identifier


def test_oauth_state_consume_is_one_time_and_rejects_expired_or_future(db: SqlitePool) -> None:
    repo = ConnectorRepo(db)
    user_id = UserRepo(db).create(username="oauth_owner", password_hash="hash", role="user")
    replay_state_id = _state(repo, user_id)
    expired_state_id = _state(repo, user_id)
    future_state_id = _state(repo, user_id)

    with db.transaction() as conn:
        conn.execute(
            "UPDATE connector_oauth_states SET created_at = ? WHERE state_id = ?",
            (1_000, replay_state_id),
        )
        conn.execute(
            "UPDATE connector_oauth_states SET created_at = ? WHERE state_id = ?",
            (900, expired_state_id),
        )
        conn.execute(
            "UPDATE connector_oauth_states SET created_at = ? WHERE state_id = ?",
            (1_001, future_state_id),
        )

    replay_state = repo.get_oauth_state_by_id(replay_state_id)
    assert replay_state is not None
    expired_state = repo.get_oauth_state_by_id(expired_state_id)
    future_state = repo.get_oauth_state_by_id(future_state_id)
    assert expired_state is not None
    assert future_state is not None
    assert repo.consume_oauth_state(expired_state.state, max_age_sec=10, now=1_000) is None
    assert repo.consume_oauth_state(future_state.state, now=1_000) is None
    assert repo.get_oauth_state_by_id(expired_state_id) is None
    assert repo.get_oauth_state_by_id(future_state_id) is None
    assert repo.consume_oauth_state(replay_state.state, now=1_000) is not None
    assert repo.consume_oauth_state(replay_state.state, now=1_000) is None


def test_cancel_oauth_state_requires_owner_and_kind(db: SqlitePool) -> None:
    repo = ConnectorRepo(db)
    owner_id = UserRepo(db).create(username="oauth_owner", password_hash="hash", role="user")
    other_id = UserRepo(db).create(username="oauth_other", password_hash="hash", role="user")
    state_id = _state(repo, owner_id)

    assert (
        repo.cancel_oauth_state(state_id=state_id, user_id=other_id, kind="composio-connect")
        is None
    )
    assert repo.get_oauth_state_by_id(state_id) is not None
    assert repo.cancel_oauth_state(state_id=state_id, user_id=owner_id, kind="notion") is None
    assert (
        repo.cancel_oauth_state(state_id=state_id, user_id=owner_id, kind="composio-connect")
        is not None
    )
    assert repo.get_oauth_state_by_id(state_id) is None


def test_composio_connection_has_durable_per_user_uniqueness(db: SqlitePool) -> None:
    repo = ConnectorRepo(db)
    user_id = UserRepo(db).create(username="composio_singleton", password_hash="hash", role="user")

    def create(instance_id: str, kind: str = "composio-connect") -> None:
        repo.create(
            instance_id=instance_id,
            user_id=user_id,
            kind=kind,
            display_name=instance_id,
            mcp_server_name=f"mcp-{instance_id}",
        )

    create("composio-one")
    assert (
        repo.create(
            instance_id="composio-two",
            user_id=user_id,
            kind="composio-connect",
            display_name="composio-two",
            mcp_server_name="mcp-composio-two",
        )
        == "composio-one"
    )
    create("github-one", kind="github")
    create("github-two", kind="github")
