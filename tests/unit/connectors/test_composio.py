"""Unit tests for personal Composio Connect rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from octop.config import OctopConfig
from octop.infra.connectors.composio import (
    COMPOSIO_KIND,
    ComposioService,
    account_email_from_tokens,
    reject_composio_default_open,
    reject_composio_share,
)
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.errors import ErrorCode, OctopError


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def composio_svc(db: SqlitePool) -> tuple[ComposioService, ConnectorRepo]:
    repo = ConnectorRepo(db)
    connector_svc = ConnectorService(
        repo=repo,
        secret_repo=SecretRepo(db),
        settings_repo=SettingsRepo(db),
        config=OctopConfig(),
    )
    return ComposioService(repo=repo, connector_svc=connector_svc), repo


def _ensure_user(db: SqlitePool, username: str = "u1") -> int:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES (?, 'x', 'user', 1)",
            (username,),
        )
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    assert row is not None
    return int(row["id"])


def test_reject_composio_share() -> None:
    with pytest.raises(OctopError) as exc:
        reject_composio_share(kind=COMPOSIO_KIND, shared=True)
    assert exc.value.code == ErrorCode.COMPOSIO_SHARE_FORBIDDEN
    reject_composio_share(kind=COMPOSIO_KIND, shared=False)
    reject_composio_share(kind="notion", shared=True)


def test_reject_composio_default_open() -> None:
    with pytest.raises(OctopError) as exc:
        reject_composio_default_open(kind=COMPOSIO_KIND, default_open=True)
    assert exc.value.code == ErrorCode.COMPOSIO_DEFAULT_OPEN_FORBIDDEN


def test_account_email_from_unverified_tokens_is_unknown() -> None:
    assert (
        account_email_from_tokens(
            {
                "id_token": "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6ImFAZXhhbXBsZS5jb20ifQ.x",
                "email": "a@example.com",
            }
        )
        is None
    )


def test_apply_oauth_replaces_single_instance(
    db: SqlitePool, composio_svc: tuple[ComposioService, ConnectorRepo]
) -> None:
    svc, repo = composio_svc
    uid = _ensure_user(db)
    first = svc.apply_oauth_tokens(
        user_id=uid,
        tokens={
            "access_token": "tok-a",
            "refresh_token": "ref-a",
            "oauth_client_id": "cid",
        },
        display_name="My Composio",
    )
    assert first["kind"] == COMPOSIO_KIND
    assert first["shared"] is False
    assert first["default_open"] is False
    second = svc.apply_oauth_tokens(
        user_id=uid,
        tokens={
            "access_token": "tok-b",
            "refresh_token": "ref-b",
            "oauth_client_id": "cid",
        },
    )
    rows = [r for r in repo.list_by_user(uid) if r.kind == COMPOSIO_KIND]
    assert len(rows) == 1
    assert rows[0].instance_id == second["instance_id"]
    assert int(second["credential_gen"]) >= 2
    assert second["account_email"] is None
    assert second["capabilities"] == {
        "full_catalog": False,
        "list_connected_apps": False,
        "account_identity": False,
        "check_app_auth": False,
        "search_apps": False,
    }


def test_apply_oauth_recovers_concurrent_first_insert(
    db: SqlitePool, composio_svc: tuple[ComposioService, ConnectorRepo]
) -> None:
    svc, repo = composio_svc
    uid = _ensure_user(db, "composio_race")
    original_create = repo.create
    injected = False

    def create_with_race(**kwargs: object) -> str:
        nonlocal injected
        if kwargs.get("kind") == COMPOSIO_KIND and not injected:
            injected = True
            original_create(
                instance_id="concurrent-winner",
                user_id=uid,
                kind=COMPOSIO_KIND,
                display_name="Concurrent winner",
                mcp_server_name="composio-concurrent-winner",
                config_json=json.dumps({"credential_gen": 5}),
            )
        return original_create(**kwargs)  # type: ignore[arg-type]

    repo.create = create_with_race  # type: ignore[method-assign]
    connection = svc.apply_oauth_tokens(
        user_id=uid,
        tokens={"access_token": "candidate-token", "oauth_client_id": "cid"},
        validation={"health": "reachable", "capabilities": {"search_apps": True}},
    )

    assert connection["instance_id"] == "concurrent-winner"
    assert connection["credential_gen"] == 6
    saved = repo.get("concurrent-winner")
    assert saved is not None and saved.has_credentials
    assert len([row for row in repo.list_by_user(uid) if row.kind == COMPOSIO_KIND]) == 1


def test_apply_oauth_clears_legacy_account_identity(
    db: SqlitePool, composio_svc: tuple[ComposioService, ConnectorRepo]
) -> None:
    svc, repo = composio_svc
    uid = _ensure_user(db)
    first = svc.apply_oauth_tokens(
        user_id=uid, tokens={"access_token": "tok-a", "oauth_client_id": "cid"}
    )
    row = repo.get(str(first["instance_id"]))
    assert row is not None
    cfg = ConnectorRepo.parse_config_json(row)
    cfg["account_email"] = "previous@example.com"
    repo.update_config_json(row.instance_id, json.dumps(cfg))

    refreshed = svc.apply_oauth_tokens(
        user_id=uid,
        tokens={
            "access_token": "tok-b",
            "oauth_client_id": "cid",
            "id_token": "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6Im5ldy5leGFtcGxlQGV4YW1wbGUuY29tIn0.x",
        },
    )

    assert refreshed["account_email"] is None
    saved = repo.get(row.instance_id)
    assert saved is not None
    assert "account_email" not in ConnectorRepo.parse_config_json(saved)


def test_apply_oauth_persists_only_validated_mcp_status(
    db: SqlitePool, composio_svc: tuple[ComposioService, ConnectorRepo]
) -> None:
    svc, _ = composio_svc
    uid = _ensure_user(db)
    connection = svc.apply_oauth_tokens(
        user_id=uid,
        tokens={"access_token": "tok", "oauth_client_id": "cid"},
        validation={
            "health": "reachable",
            "capabilities": {
                "search_apps": True,
                "full_catalog": "yes",
                "account_identity": True,
            },
            "extra": "must-not-persist",
        },
    )
    assert connection["health"] == "reachable"
    assert connection["capabilities"]["search_apps"] is True
    assert connection["capabilities"]["full_catalog"] is False
    assert connection["capabilities"]["account_identity"] is False
    assert set(connection["capabilities"]) == {
        "full_catalog",
        "list_connected_apps",
        "account_identity",
        "check_app_auth",
        "search_apps",
    }


def test_list_visible_excludes_shared_composio(
    db: SqlitePool, composio_svc: tuple[ComposioService, ConnectorRepo]
) -> None:
    svc, repo = composio_svc
    uid = _ensure_user(db)
    other = _ensure_user(db, "u2")
    applied = svc.apply_oauth_tokens(
        user_id=uid,
        tokens={"access_token": "tok", "oauth_client_id": "c"},
    )
    repo.update_metadata(str(applied["instance_id"]), shared=True)
    visible = repo.list_visible(other)
    assert all(r.instance_id != applied["instance_id"] for r in visible)


def test_consume_oauth_state_respects_expiry(db: SqlitePool) -> None:
    uid = _ensure_user(db)
    repo = ConnectorRepo(db)
    repo.create_oauth_state(
        state_id="s1",
        state="state-value",
        user_id=uid,
        kind=COMPOSIO_KIND,
        code_verifier="v",
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE connector_oauth_states SET created_at = ? WHERE state = ?",
            (1, "state-value"),
        )
    assert repo.consume_oauth_state("state-value", max_age_sec=10, now=1000) is None
    assert repo.consume_oauth_state("missing", max_age_sec=10) is None


@pytest.mark.asyncio
async def test_connector_uid_contextvar_is_task_local() -> None:
    """Concurrent shared-agent turns must not share connector_user_id via agent dict."""
    import asyncio

    from octop.infra.agents import manager as mgr_mod

    seen: list[int | None] = []

    async def _run(uid: int) -> None:
        token = mgr_mod._connector_user_id_var.set(uid)
        await asyncio.sleep(0.01)
        seen.append(mgr_mod._connector_user_id_var.get())
        mgr_mod._connector_user_id_var.reset(token)

    await asyncio.gather(_run(11), _run(22))
    assert sorted(x for x in seen if x is not None) == [11, 22]
