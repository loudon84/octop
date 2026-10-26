"""Real PostgreSQL final-index races for registration entry points."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.services import SharedServices, build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout
from tests.support.postgresql import requires_postgresql


@pytest.fixture
def pg_registration_services(tmp_path: Path):
    conninfo = os.environ["OCTOP_TEST_DATABASE_URL"]
    db = PostgresPool(conninfo, min_size=1, max_size=4)
    run_migrations(db)
    services = build_shared_services(
        db=db, paths=PathLayout(tmp_path / "primary"), config=OctopConfig()
    )
    admin_id = services.user_repo.create(
        username=f"registration-admin-{uuid.uuid4().hex}",
        password_hash=None,
        role="admin",
    )

    def make_pair() -> tuple[tuple[PostgresPool, SharedServices], ...]:
        pair: list[tuple[PostgresPool, SharedServices]] = []
        for index in range(2):
            pool = PostgresPool(conninfo, min_size=1, max_size=4)
            shared = build_shared_services(
                db=pool,
                paths=PathLayout(tmp_path / f"pair-{uuid.uuid4().hex}-{index}"),
                config=OctopConfig(),
            )
            pair.append((pool, shared))
        for _pool, shared in pair:
            shared.settings_repo.set("user_registration.email_required", "true")
        return tuple(pair)

    try:
        yield services, admin_id, make_pair
    finally:
        db.close()


class _InsertBarrierConnection:
    def __init__(self, connection: Any, barrier: threading.Barrier) -> None:
        self._connection = connection
        self._barrier = barrier

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.lstrip().upper().startswith("INSERT INTO USERS"):
            self._barrier.wait(timeout=15)
        return self._connection.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _pause_before_user_insert(pool: PostgresPool, barrier: threading.Barrier) -> None:
    original_transaction = pool.transaction

    @contextmanager
    def transaction():
        with original_transaction() as connection:
            yield _InsertBarrierConnection(connection, barrier)

    pool.transaction = transaction  # type: ignore[method-assign]


def _race_after_prechecks(
    pair: tuple[tuple[PostgresPool, SharedServices], ...],
    first: Any,
    second: Any,
) -> list[Any]:
    barrier = threading.Barrier(2)
    for pool, _services in pair:
        _pause_before_user_insert(pool, barrier)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(first), executor.submit(second)]
            results: list[Any] = []
            for future in futures:
                try:
                    results.append(future.result(timeout=30))
                except BaseException as exc:
                    results.append(exc)
            return results
    finally:
        barrier.abort()
        for pool, _services in pair:
            pool.close()


def _assert_one_email_winner(results: list[Any]) -> tuple[Any, OctopError]:
    from psycopg.errors import UniqueViolation

    winners = [result for result in results if not isinstance(result, BaseException)]
    losers = [result for result in results if isinstance(result, BaseException)]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert isinstance(losers[0], OctopError)
    assert losers[0].code is ErrorCode.EMAIL_TAKEN
    assert isinstance(losers[0].__cause__, UniqueViolation)
    assert losers[0].__cause__.diag.constraint_name == "idx_users_email"
    return winners[0], losers[0]


@requires_postgresql
@pytest.mark.postgresql
def test_postgresql_sso_distinct_subject_email_race_uses_final_index(
    pg_registration_services,
) -> None:
    primary, _admin_id, make_pair = pg_registration_services
    pair = make_pair()
    provider = primary.sso_repo.upsert_provider(
        enabled=True,
        display_name="registration test",
        issuer="https://registration.invalid",
        client_id=f"registration-{uuid.uuid4().hex}",
        client_secret_enc=None,
        scopes="openid",
        dashboard_origin=None,
    )
    from octop.infra.users.manager import UserManager

    managers = [UserManager(services) for _pool, services in pair]
    users = [f"sso-{uuid.uuid4().hex[:12]}-{i}" for i in range(2)]
    subjects = [f"subject-{uuid.uuid4().hex}-{i}" for i in range(2)]
    email = f"race-{uuid.uuid4().hex}@example.com"

    def resolve(index: int) -> Any:
        return asyncio.run(
            managers[index].resolve_or_create_sso_user(
                provider_id=provider.id,
                subject=subjects[index],
                claims={
                    "preferred_username": users[index],
                    "email": f" {email.upper()} " if index == 0 else email.swapcase(),
                },
            )
        )

    results = _race_after_prechecks(pair, lambda: resolve(0), lambda: resolve(1))
    winner, _loser = _assert_one_email_winner(results)
    row = primary.user_repo.get_by_email(email)
    assert row is not None and row.id == winner.id
    assert (
        sum(primary.user_repo.get_by_sso(provider.id, subject) is not None for subject in subjects)
        == 1
    )
    assert sum(manager.get(users[index]) is not None for index, manager in enumerate(managers)) == 1


@requires_postgresql
@pytest.mark.postgresql
def test_postgresql_invite_redemption_rolls_back_losing_invite_on_email_race(
    pg_registration_services,
) -> None:
    primary, admin_id, make_pair = pg_registration_services
    pair = make_pair()
    codes = [f"invite-{uuid.uuid4().hex}" for _ in range(2)]
    email = f"invite-{uuid.uuid4().hex}@example.com"
    from octop.infra.db.repos._base import now_ts
    from octop.infra.users.invites import InviteService

    for code in codes:
        primary.invite_repo.create(code=code, created_by=admin_id, expires_at=now_ts() + 3600)
    usernames = [f"invite-user-{uuid.uuid4().hex[:12]}-{i}" for i in range(2)]

    def redeem(index: int) -> Any:
        return InviteService(pair[index][1]).redeem(
            code=codes[index],
            username=usernames[index],
            password="TestPass12",
            display_name=None,
            locale="en",
            email=f" {email.upper()} " if index == 0 else email.swapcase(),
            register_user=lambda _user: None,
        )

    results = _race_after_prechecks(pair, lambda: redeem(0), lambda: redeem(1))
    winner, _loser = _assert_one_email_winner(results)
    assert primary.user_repo.get_by_email(email).username == winner.username
    invite_rows = [primary.invite_repo.get_by_code(code) for code in codes]
    assert sum(row is not None and row.used_at is not None for row in invite_rows) == 1
    assert sum(row is not None and row.used_at is None for row in invite_rows) == 1


@requires_postgresql
@pytest.mark.postgresql
def test_postgresql_ldap_provisioning_maps_final_email_index_race(
    pg_registration_services,
) -> None:
    primary, _admin_id, make_pair = pg_registration_services
    pair = make_pair()
    ldap_provider = primary.sso_repo.upsert_by_kind(
        "ldap",
        enabled=True,
        display_name="registration test LDAP",
        issuer="",
        client_id="",
        client_secret_enc=None,
        scopes="",
        dashboard_origin=None,
        extra={"auto_provision": True},
    )
    from octop.infra.auth.ldap.client import LdapIdentity
    from octop.infra.auth.ldap.service import LdapAuthService
    from octop.infra.users.manager import UserManager

    managers = [UserManager(services) for _pool, services in pair]
    auth_services = [
        LdapAuthService(services, managers[index]) for index, (_pool, services) in enumerate(pair)
    ]
    subjects = [f"ldap-subject-{uuid.uuid4().hex}-{i}" for i in range(2)]
    usernames = [f"ldap-user-{uuid.uuid4().hex[:12]}-{i}" for i in range(2)]
    email = f"ldap-{uuid.uuid4().hex}@example.com"

    def resolve(index: int) -> Any:
        identity = LdapIdentity(
            dn=f"uid=ldap-{index},ou=users,dc=example,dc=org",
            username=usernames[index],
            email=f" {email.upper()} " if index == 0 else email.swapcase(),
            display_name=f"LDAP {index}",
            groups=(),
            subject=subjects[index],
        )
        return asyncio.run(auth_services[index].resolve_user(identity))

    results = _race_after_prechecks(pair, lambda: resolve(0), lambda: resolve(1))
    winner, _loser = _assert_one_email_winner(results)
    row = primary.user_repo.get_by_email(email)
    assert row is not None and row.id == winner.id
    assert (
        sum(
            primary.user_repo.get_by_sso(ldap_provider.id, subject) is not None
            for subject in subjects
        )
        == 1
    )
    assert (
        sum(manager.get(usernames[index]) is not None for index, manager in enumerate(managers))
        == 1
    )
