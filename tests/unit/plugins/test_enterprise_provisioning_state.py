from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost
from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    RecordWrite,
    RetainedRecordWrite,
    UserIdentityCheck,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(PLUGIN_ROOT.resolve()))


async def _seed_task(
    facade: Any,
    user_id: int,
    *,
    state: str = "RETRY_WAIT",
    **changes: Any,
) -> tuple[str, int]:
    from enterprise_knowledge.provisioning_records import (
        ProvisioningIdentity,
        ProvisioningTaskRecord,
    )
    from enterprise_knowledge.provisioning_state import TASK_INDEX_NAMESPACE, TASK_NAMESPACE

    identity = await facade.read_user_identity(user_id)
    task_id = str(uuid4())
    task_data: dict[str, Any] = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "task_id": task_id,
        "identity": ProvisioningIdentity(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            user_id=identity.user_id,
            email=identity.email,
            disabled=identity.disabled,
            snapshot_token=identity.snapshot_token,
        ),
        "state": state,
        "error_code": None,
        "checkpoint": "NONE",
        "domain_id": None,
        "base_url": None,
        "user_intent_id": None,
        "kb_intent_id": None,
        "key_intent_id": None,
        "bootstrap_ref": None,
        "bootstrap_revision": None,
        "bootstrap_created_at": None,
        "bootstrap_expires_at": None,
        "final_key_ref": None,
        "final_key_revision": None,
        "remote_user_id": None,
        "tenant_id": None,
        "kb_id": None,
        "key_id": None,
        "attempt": None,
        "consecutive_failures": 0,
        "updated_at": "2026-10-25T12:34:56Z",
        "last_attempt_at": None,
        "next_retry_at": None,
        "last_retry": None,
    }
    task_data.update(changes)
    task = ProvisioningTaskRecord.model_validate(task_data)
    await facade.commit(
        [
            RecordWrite(TASK_NAMESPACE, str(user_id), user_id, task.model_dump(mode="python"), 0),
            RecordWrite(
                TASK_INDEX_NAMESPACE,
                task_id,
                user_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "local_user_id": user_id,
                },
                0,
            ),
        ],
        identity_checks=[UserIdentityCheck(identity)],
    )
    return task_id, 1


async def _save_config(facade: Any, admin_id: int, *, name: str = "Shared KB") -> dict[str, Any]:
    return await facade.invoke_action(
        "provisioning.config.save",
        PluginActor(admin_id, True),
        {
            "expected_revision": 0,
            "expected_anchor_revision": 0,
            "name": name,
            "base_url": "https://weknora.example/api/v1",
            "enabled": True,
            "system_admin_jwt": "synthetic-test-token",
        },
    )


@pytest.fixture
def plugin_host(tmp_path: Path) -> tuple[Any, Any, int]:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    user_id = UserRepo(db).create(username="provision-user", role="user", email="a@example.test")
    spec = importlib.util.spec_from_file_location(
        "enterprise_knowledge_state_plugin",
        PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "0.1.0", "EK", "tool", "main.py"),
        source_path=PLUGIN_ROOT,
    )
    module.setup(context)
    facade = PluginHost(lambda: services).bind_context(context, "0.1.0")
    return db, facade, user_id


@pytest.mark.asyncio
async def test_status_without_task_is_read_only_and_does_not_resolve_admin_credentials(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, facade, user_id = plugin_host

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("status must not resolve an administrator credential")

    monkeypatch.setattr(facade, "resolve_credential", forbidden)
    monkeypatch.setattr(facade, "commit", forbidden)
    output = await facade.invoke_action("provisioning.status", PluginActor(user_id, False), {})
    assert output["revision"] == 0
    assert output["state"] == "CONFIG_REQUIRED"
    assert output["can_retry"] is False
    assert output["updated_at"] is None
    assert await facade.get_record("provision_tasks", str(user_id)) is None


@pytest.mark.asyncio
async def test_configured_status_and_list_are_read_only_and_allowlist_outputs(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="configured-read-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="configured-read-employee", role="user", email="visible@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state="RETRY_WAIT")
    before = await facade.get_record("provision_tasks", str(employee_id))

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("status/list must not resolve credentials, write, or scan retained records")

    monkeypatch.setattr(facade, "resolve_credential", forbidden)
    monkeypatch.setattr(facade, "commit", forbidden)
    monkeypatch.setattr(facade, "list_retained_records", forbidden)
    status = await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    listed = await facade.invoke_action(
        "provisioning.tasks.list",
        PluginActor(admin_id, True),
        {"after_user_id": 0, "limit": 10},
    )
    item = next(item for item in listed["items"] if item["task_id"] == task_id)
    assert set(status) == {
        "schema_id",
        "version",
        "revision",
        "state",
        "error_code",
        "updated_at",
        "last_attempt_at",
        "next_retry_at",
        "can_retry",
    }
    assert set(item) == {"schema_id", "version", "task_id", "user_id", "email", "status"}
    assert set(item["status"]) == set(status)
    assert item["email"] == "visible@example.test"
    after = await facade.get_record("provision_tasks", str(employee_id))
    assert before is not None and after is not None and before.revision == after.revision == 1


@pytest.mark.asyncio
async def test_email_recovery_without_config_and_post_intent_identity_rules(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _user_id = plugin_host
    no_email_id = UserRepo(db).create(username="email-no-config", role="user")
    task_id, _ = await _seed_task(facade, no_email_id, state="EMAIL_REQUIRED")
    before = await facade.get_record("provision_tasks", str(no_email_id))
    UserRepo(db).set_email(no_email_id, "recovered-no-config@example.test")
    recovered = await facade.invoke_action(
        "provisioning.status", PluginActor(no_email_id, False), {}
    )
    after = await facade.get_record("provision_tasks", str(no_email_id))
    assert recovered["state"] == "CONFIG_REQUIRED"
    assert before is not None and after is not None
    assert after.revision == before.revision == 1
    assert after.payload["task_id"] == task_id and after.payload["state"] == "EMAIL_REQUIRED"

    employee_id = UserRepo(db).create(
        username="intent-email-change", role="user", email="original@example.test"
    )
    intent_id = str(uuid4())
    task_id, _ = await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        domain_id=str(uuid4()),
        base_url="https://weknora.example/api/v1",
        user_intent_id=intent_id,
    )
    task = await facade.get_record("provision_tasks", str(employee_id))
    assert task is not None
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                intent_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": task.payload["domain_id"],
                    "local_user_id": employee_id,
                    "resource_kind": "USER",
                    "phase": "UNKNOWN",
                    "receipt_id": None,
                    "task_id": task_id,
                    "created_by_attempt_id": str(uuid4()),
                    "last_send_attempt_id": str(uuid4()),
                    "created_at": "2026-10-25T12:34:56Z",
                    "updated_at": "2026-10-25T12:34:56Z",
                    "parent_intent_id": None,
                    "remote_user_id": None,
                    "tenant_id": None,
                    "kb_id": None,
                    "absence_proof": None,
                },
                0,
            )
        ],
    )
    UserRepo(db).set_email(employee_id, "changed@example.test")
    changed = await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert changed["state"] == "IDENTITY_CONFLICT"
    UserRepo(db).set_disabled(employee_id, True)
    admin_id = UserRepo(db).create(username="disabled-target-admin", role="admin")
    disabled = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "disabled employee"},
    )
    assert disabled["state"] == "SUSPENDED" and disabled["error_code"] == "USER_DISABLED"


@pytest.mark.asyncio
async def test_identity_snapshot_token_does_not_detect_reverted_email_aba(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, user_id = plugin_host
    before = await facade.read_user_identity(user_id)
    UserRepo(db).set_email(user_id, "temporary@example.test")
    changed = await facade.read_user_identity(user_id)
    UserRepo(db).set_email(user_id, before.email)
    restored = await facade.read_user_identity(user_id)
    assert changed.snapshot_token != before.snapshot_token
    assert restored.snapshot_token == before.snapshot_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("anchor-only", "CONFIG_REQUIRED"),
        ("config-no-anchor", "PROVISIONING_UNAVAILABLE"),
        ("anchor-mismatch", "PROVISIONING_UNAVAILABLE"),
        ("disabled-config", "CONFIG_REQUIRED"),
        ("valid-block", "CONFIG_REQUIRED"),
        ("block-owner", "PROVISIONING_UNAVAILABLE"),
        ("block-domain", "PROVISIONING_UNAVAILABLE"),
        ("block-revision", "PROVISIONING_UNAVAILABLE"),
    ],
)
async def test_config_anchor_and_auth_block_lineage_matrix(
    plugin_host: tuple[Any, Any, int], shape: str, expected: str
) -> None:
    db, facade, employee_id = plugin_host
    admin_id = UserRepo(db).create(username=f"config-matrix-admin-{shape}", role="admin")
    other_id = UserRepo(db).create(username=f"config-matrix-other-{shape}", role="user")
    domain_id = str(uuid4())
    base_url = "https://matrix.example/api/v1"
    now = "2026-10-25T12:34:56Z"
    if shape == "anchor-only":
        await facade.commit(
            [],
            retained_writes=[
                RetainedRecordWrite(
                    "provision_domains",
                    "primary",
                    {
                        "schema_id": "octop.enterprise.provisioning.v1",
                        "version": 1,
                        "domain_id": domain_id,
                        "canonical_base_url": base_url,
                    },
                    0,
                )
            ],
        )
    elif shape == "config-no-anchor":
        await facade.commit(
            [
                RecordWrite(
                    "provision_config",
                    "primary",
                    admin_id,
                    {
                        "schema_id": "octop.enterprise.provisioning.v1",
                        "version": 1,
                        "domain_id": domain_id,
                        "name": "Matrix",
                        "base_url": base_url,
                        "enabled": True,
                        "admin_credential_ref": str(uuid4()),
                        "admin_credential_revision": 2,
                        "updated_at": now,
                    },
                    0,
                )
            ]
        )
    else:
        await _save_config(facade, admin_id)
        config = await facade.get_record("provision_config", "primary")
        anchor = await facade.get_retained_record("provision_domains", "primary")
        assert config is not None and anchor is not None
        if shape == "anchor-mismatch":
            await facade.commit(
                [],
                retained_writes=[
                    RetainedRecordWrite(
                        "provision_domains",
                        "primary",
                        {**anchor.payload, "domain_id": str(uuid4())},
                        anchor.revision,
                    )
                ],
            )
        elif shape == "disabled-config":
            await facade.commit(
                [
                    RecordWrite(
                        "provision_config",
                        "primary",
                        admin_id,
                        {**config.payload, "enabled": False},
                        config.revision,
                    )
                ]
            )
        elif shape.startswith("block-") or shape == "valid-block":
            credential_ref = config.payload["admin_credential_ref"]
            block_domain = str(uuid4()) if shape == "block-domain" else config.payload["domain_id"]
            block_revision = 1 if shape == "block-revision" else 2
            owner = other_id if shape == "block-owner" else admin_id
            await facade.commit(
                [
                    RecordWrite(
                        "provision_auth_blocks",
                        credential_ref,
                        owner,
                        {
                            "schema_id": "octop.enterprise.provisioning.v1",
                            "version": 1,
                            "domain_id": block_domain,
                            "credential_revision": block_revision,
                            "http_status": 403,
                            "rejected_at": now,
                        },
                        0,
                    )
                ]
            )
    try:
        status = await facade.invoke_action(
            "provisioning.status", PluginActor(employee_id, False), {}
        )
        actual = status["state"]
    except PluginActionError as exc:
        actual = exc.code
    assert actual == expected


@pytest.mark.asyncio
async def test_self_retry_seeds_one_bound_task_and_index_in_real_host_transaction(
    plugin_host: tuple[Any, Any, int],
) -> None:
    _db, facade, user_id = plugin_host
    result = await facade.invoke_action(
        "provisioning.retry", PluginActor(user_id, False), {"expected_revision": 0}
    )
    task = await facade.get_record("provision_tasks", str(user_id))
    assert task is not None
    index = await facade.get_record("provision_task_index", task.payload["task_id"])
    assert task is not None and index is not None
    assert task.revision == index.revision == 1
    assert task.owner_user_id == index.owner_user_id == user_id
    assert task.payload["task_id"] == index.logical_key
    assert index.payload["local_user_id"] == user_id
    assert result["state"] == "CONFIG_REQUIRED"


@pytest.mark.asyncio
async def test_concurrent_self_retry_creates_one_task_index_and_returns_winner(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, user_id = plugin_host
    admin_id = UserRepo(db).create(username="concurrent-retry-admin", role="admin")
    await _save_config(facade, admin_id)

    results = await asyncio.gather(
        facade.invoke_action(
            "provisioning.retry", PluginActor(user_id, False), {"expected_revision": 0}
        ),
        facade.invoke_action(
            "provisioning.retry", PluginActor(user_id, False), {"expected_revision": 0}
        ),
    )

    task = await facade.get_record("provision_tasks", str(user_id))
    assert task is not None and task.revision == 1 and task.payload["state"] == "PENDING"
    assert results[0]["revision"] == results[1]["revision"] == 1
    assert results[0]["schema_id"] == results[1]["schema_id"]
    index = await facade.get_record("provision_task_index", task.payload["task_id"])
    assert index is not None and index.revision == 1


@pytest.mark.asyncio
async def test_email_recovery_projection_is_not_a_persisted_pending_winner(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="email-race-admin", role="admin")
    target_id = UserRepo(db).create(username="email-race-target", role="user")
    await _save_config(facade, admin_id)
    original_get = facade.get_record
    task_reads = 0

    async def seed_competing_email_required_task(namespace: str, logical_key: str) -> Any:
        nonlocal task_reads
        if namespace == "provision_tasks" and logical_key == str(target_id):
            task_reads += 1
            if task_reads == 2:
                await _seed_task(facade, target_id, state="EMAIL_REQUIRED")
                UserRepo(db).set_email(target_id, "recovered@example.test")
        return await original_get(namespace, logical_key)

    monkeypatch.setattr(facade, "get_record", seed_competing_email_required_task)

    with pytest.raises(PluginActionError) as raised:
        await facade.invoke_action(
            "provisioning.retry", PluginActor(target_id, False), {"expected_revision": 0}
        )

    winner = await original_get("provision_tasks", str(target_id))
    assert raised.value.code == "PROVISIONING_CONFLICT"
    assert winner is not None and winner.revision == 1
    assert winner.payload["state"] == "EMAIL_REQUIRED"
    assert winner.payload["identity"]["email"] is None
    assert await original_get("provision_task_index", winner.payload["task_id"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt_kind", "first_read"),
    [(kind, first) for kind in ("live", "expired", "config-mismatch") for first in (False, True)],
)
async def test_provisioning_race_returns_only_a_live_config_pinned_attempt(
    plugin_host: tuple[Any, Any, int],
    monkeypatch: pytest.MonkeyPatch,
    attempt_kind: str,
    first_read: bool,
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="attempt-race-admin", role="admin")
    target_id = UserRepo(db).create(
        username="attempt-race-target", role="user", email="attempt@example.test"
    )
    await _save_config(facade, admin_id)
    from enterprise_knowledge.provisioning import DOMAIN_NAMESPACE

    config_row = await facade.get_record("provision_config", "primary")
    anchor_row = await facade.get_retained_record(DOMAIN_NAMESPACE, "primary")
    identity = await facade.read_user_identity(target_id)
    assert config_row is not None and anchor_row is not None
    now = datetime.now(UTC)
    started = now - timedelta(seconds=200 if attempt_kind == "expired" else 10)
    config_pin = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "owner_user_id": admin_id,
        "config_revision": config_row.revision,
        "admin_credential_ref": config_row.payload["admin_credential_ref"],
        "admin_credential_revision": config_row.payload["admin_credential_revision"],
        "domain_id": (
            str(uuid4()) if attempt_kind == "config-mismatch" else config_row.payload["domain_id"]
        ),
        "base_url": config_row.payload["base_url"],
        "anchor_revision": anchor_row.revision,
    }
    attempt = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "attempt_id": str(uuid4()),
        "mode": "ADVANCE",
        "config": config_pin,
        "employee": {
            "schema_id": "octop.enterprise.provisioning.v1",
            "version": 1,
            "user_id": identity.user_id,
            "email": identity.email,
            "disabled": identity.disabled,
            "snapshot_token": identity.snapshot_token,
        },
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "deadline_at": (started + timedelta(seconds=90)).isoformat().replace("+00:00", "Z"),
        "lease_expires_at": (started + timedelta(seconds=45)).isoformat().replace("+00:00", "Z"),
        "finished_at": None,
    }
    original_get = facade.get_record
    if first_read:
        await _seed_task(facade, target_id, state="PROVISIONING", attempt=attempt)
    else:
        task_reads = 0

        async def seed_competing_attempt(namespace: str, logical_key: str) -> Any:
            nonlocal task_reads
            if namespace == "provision_tasks" and logical_key == str(target_id):
                task_reads += 1
                if task_reads == 2:
                    await _seed_task(facade, target_id, state="PROVISIONING", attempt=attempt)
            return await original_get(namespace, logical_key)

        monkeypatch.setattr(facade, "get_record", seed_competing_attempt)

    returns_winner = attempt_kind == "live"
    if returns_winner:
        result = await facade.invoke_action(
            "provisioning.retry", PluginActor(target_id, False), {"expected_revision": 0}
        )
        assert result["state"] == "PROVISIONING" and result["revision"] == 1
    else:
        with pytest.raises(PluginActionError) as raised:
            await facade.invoke_action(
                "provisioning.retry", PluginActor(target_id, False), {"expected_revision": 0}
            )
        assert raised.value.code == "PROVISIONING_CONFLICT"
    winner = await original_get("provision_tasks", str(target_id))
    assert winner is not None and winner.revision == 1
    assert winner.payload["state"] == "PROVISIONING"


@pytest.mark.asyncio
async def test_admin_task_retry_records_reason_and_keeps_task_index(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="task-retry-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="task-retry-employee", role="user", email="task-retry@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _revision = await _seed_task(facade, employee_id, state="RETRY_WAIT")

    result = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "operator retry"},
    )

    task = await facade.get_record("provision_tasks", str(employee_id))
    index = await facade.get_record("provision_task_index", task_id)
    assert result["state"] == "PENDING" and result["revision"] == 2
    assert task is not None and task.revision == 2 and task.payload["state"] == "PENDING"
    assert task.payload["last_retry"]["kind"] == "ADMIN"
    assert task.payload["last_retry"]["reason"] == "operator retry"
    assert task.payload["last_retry"]["actor_user_id"] == admin_id
    assert index is not None and index.revision == 1 and index.owner_user_id == employee_id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["MANUAL_REVIEW", "SUSPENDED", "IDENTITY_CONFLICT"])
async def test_stale_expected_revision_does_not_bypass_blocked_task_states(
    plugin_host: tuple[Any, Any, int], state: str
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username=f"stale-{state}-admin", role="admin")
    employee_id = UserRepo(db).create(
        username=f"stale-{state}-employee", role="user", email="stale@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state=state)
    row = await facade.get_record("provision_tasks", str(employee_id))
    assert row is not None
    await facade.commit(
        [RecordWrite("provision_tasks", str(employee_id), employee_id, row.payload, row.revision)]
    )
    with pytest.raises(PluginActionError) as stale:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "stale blocked"},
        )
    task = await facade.get_record("provision_tasks", str(employee_id))
    assert stale.value.code == "PROVISIONING_CONFLICT"
    assert task is not None and task.revision == 2 and task.payload["state"] == state


@pytest.mark.asyncio
@pytest.mark.parametrize("winner_kind", ["ADMIN", "SELF"])
async def test_existing_task_precommit_race_returns_valid_admin_or_self_winner(
    plugin_host: tuple[Any, Any, int],
    monkeypatch: pytest.MonkeyPatch,
    winner_kind: str,
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username=f"precommit-admin-{winner_kind}", role="admin")
    competitor_id = (
        UserRepo(db).create(username="precommit-admin-other", role="admin")
        if winner_kind == "ADMIN"
        else admin_id
    )
    employee_id = UserRepo(db).create(
        username=f"precommit-employee-{winner_kind}", role="user", email="race@example.test"
    )
    winner_actor_id = employee_id if winner_kind == "SELF" else competitor_id
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state="RETRY_WAIT")
    index_before = await facade.get_record("provision_task_index", task_id)
    assert index_before is not None and index_before.revision == 1
    original_get = facade.get_record
    triggered = False
    task_reads = 0
    competitor_result: dict[str, Any] | None = None

    async def trigger_competitor(namespace: str, logical_key: str) -> Any:
        nonlocal triggered, task_reads, competitor_result
        row = await original_get(namespace, logical_key)
        if namespace != "provision_tasks" or logical_key != str(employee_id):
            return row
        task_reads += 1
        if task_reads == 1:
            assert row is not None and row.revision == 1
            return row
        if task_reads == 2 and not triggered:
            assert row is not None and row.revision == 1
            triggered = True
            if winner_kind == "SELF":
                competitor_result = await facade.invoke_action(
                    "provisioning.retry",
                    PluginActor(employee_id, False),
                    {"expected_revision": 1},
                )
            else:
                competitor_result = await facade.invoke_action(
                    "provisioning.task.retry",
                    PluginActor(competitor_id, True),
                    {
                        "task_id": task_id,
                        "expected_revision": 1,
                        "reason": "competing admin",
                    },
                )
            return await original_get(namespace, logical_key)
        return row

    monkeypatch.setattr(facade, "get_record", trigger_competitor)
    result = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "outer admin"},
    )
    task = await original_get("provision_tasks", str(employee_id))
    index = await original_get("provision_task_index", task_id)
    assert triggered and task_reads >= 2 and competitor_result is not None
    assert result["state"] == competitor_result["state"] == "PENDING"
    assert result["revision"] == competitor_result["revision"] == 2
    assert task is not None and task.revision == 2
    assert task.payload["last_retry"]["kind"] == winner_kind
    assert task.payload["last_retry"]["actor_user_id"] == winner_actor_id
    assert task.payload["last_retry"]["reason"] == (
        "competing admin" if winner_kind == "ADMIN" else None
    )
    assert index is not None and index.revision == index_before.revision
    assert index.owner_user_id == index_before.owner_user_id == employee_id
    assert index.logical_key == index_before.logical_key == task_id
    assert index.payload == index_before.payload


@pytest.mark.asyncio
async def test_existing_task_commit_cas_race_returns_one_durable_winner(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    first_admin = UserRepo(db).create(username="cas-admin-first", role="admin")
    second_admin = UserRepo(db).create(username="cas-admin-second", role="admin")
    employee_id = UserRepo(db).create(
        username="cas-race-employee", role="user", email="cas-race@example.test"
    )
    await _save_config(facade, first_admin)
    task_id, _ = await _seed_task(facade, employee_id, state="RETRY_WAIT")
    original_commit = facade.commit
    ready = asyncio.Event()
    callers = 0

    async def race_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal callers
        if any(write.namespace == "provision_tasks" for write in writes):
            callers += 1
            if callers == 2:
                ready.set()
            await ready.wait()
        return await original_commit(writes, *args, **kwargs)

    monkeypatch.setattr(facade, "commit", race_commit)
    results = await asyncio.gather(
        facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(first_admin, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "first admin"},
        ),
        facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(second_admin, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "second admin"},
        ),
    )
    task = await original_commit.__self__.get_record("provision_tasks", str(employee_id))
    index = await original_commit.__self__.get_record("provision_task_index", task_id)
    assert callers == 2
    assert [result["revision"] for result in results] == [2, 2]
    assert task is not None and task.revision == 2 and task.payload["state"] == "PENDING"
    assert task.payload["last_retry"]["actor_user_id"] in {first_admin, second_admin}
    assert index is not None and index.revision == 1


@pytest.mark.asyncio
async def test_real_host_executor_unknown_after_retry_commit_recovers_exact_pair(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="executor-unknown-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="executor-unknown-employee", role="user", email="executor@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state="RETRY_WAIT")
    original_run = facade._run
    original_commit = facade.commit
    armed = False
    injected = False

    async def arm_task_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal armed
        if any(write.namespace == "provision_tasks" for write in writes):
            armed = True
        return await original_commit(writes, *args, **kwargs)

    async def unknown_in_actual_executor(operation: Any, **kwargs: Any) -> Any:
        nonlocal armed, injected
        if not armed:
            return await original_run(operation, **kwargs)
        armed = False
        injected = True

        def commit_then_lose_result() -> Any:
            operation()
            raise RuntimeError("executor result lost after durable commit")

        return await original_run(commit_then_lose_result, **kwargs)

    monkeypatch.setattr(facade, "commit", arm_task_commit)
    monkeypatch.setattr(facade, "_run", unknown_in_actual_executor)
    result = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "executor unknown"},
    )
    task = await facade.get_record("provision_tasks", str(employee_id))
    index = await facade.get_record("provision_task_index", task_id)
    assert injected
    assert result["state"] == "PENDING" and result["revision"] == 2
    assert task is not None and task.revision == 2 and task.payload["task_id"] == task_id
    assert task.payload["last_retry"]["reason"] == "executor unknown"
    assert index is not None and index.revision == 1 and index.owner_user_id == employee_id


@pytest.mark.asyncio
async def test_real_host_executor_unknown_without_write_fails_closed_without_replay(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="executor-no-write-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="executor-no-write-employee", role="user", email="no-write@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state="RETRY_WAIT")
    original_run = facade._run
    original_commit = facade.commit
    armed = False
    executor_calls = 0

    async def arm_task_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal armed
        if any(write.namespace == "provision_tasks" for write in writes):
            armed = True
        return await original_commit(writes, *args, **kwargs)

    async def fail_before_operation_in_executor(operation: Any, **kwargs: Any) -> Any:
        nonlocal armed, executor_calls
        if not armed:
            return await original_run(operation, **kwargs)
        armed = False
        executor_calls += 1

        def no_effect() -> Any:
            raise RuntimeError("executor failed before transaction")

        return await original_run(no_effect, **kwargs)

    monkeypatch.setattr(facade, "commit", arm_task_commit)
    monkeypatch.setattr(facade, "_run", fail_before_operation_in_executor)
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "unknown no effect"},
        )
    task = await facade.get_record("provision_tasks", str(employee_id))
    assert executor_calls == 1
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"
    assert task is not None and task.revision == 1 and task.payload["state"] == "RETRY_WAIT"


@pytest.mark.asyncio
async def test_real_executor_cancellation_settles_blocked_retry_transaction(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from contextlib import contextmanager

    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="executor-cancel-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="executor-cancel-employee", role="user", email="cancel@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state="RETRY_WAIT")
    original_commit = facade.commit
    entered = asyncio.Event()
    exited = asyncio.Event()
    release = threading.Event()
    commit_calls = 0

    async def count_task_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal commit_calls
        if any(write.namespace == "provision_tasks" for write in writes):
            commit_calls += 1
        return await original_commit(writes, *args, **kwargs)

    original_transaction = db.transaction

    @contextmanager
    def block_inside_real_transaction() -> Any:
        try:
            with original_transaction() as connection:
                loop.call_soon_threadsafe(entered.set)
                release.wait(timeout=10)
                yield connection
        finally:
            loop.call_soon_threadsafe(exited.set)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(facade, "commit", count_task_commit)
    monkeypatch.setattr(db, "transaction", block_inside_real_transaction)
    action = asyncio.create_task(
        facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "cancel at executor"},
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=3)
    action.cancel()
    await asyncio.sleep(0)
    assert not action.done()
    assert not exited.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await action
    await asyncio.wait_for(exited.wait(), timeout=3)
    task = await facade.get_record("provision_tasks", str(employee_id))
    index = await facade.get_record("provision_task_index", task_id)
    assert commit_calls == 1
    assert task is not None and task.revision == 2 and task.payload["state"] == "PENDING"
    assert index is not None and index.revision == 1


@pytest.mark.asyncio
async def test_unknown_proof_prevents_pending_race_winner_shortcut(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="unknown-winner-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="unknown-winner-employee", role="user", email="unknown@example.test"
    )
    await _save_config(facade, admin_id)
    from enterprise_knowledge.provisioning import INTENT_NAMESPACE

    config = await facade.get_record("provision_config", "primary")
    assert config is not None
    intent_id = str(uuid4())
    intent_row = None
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    original_get = facade.get_record
    task_reads = 0

    async def seed_unknown_pending_winner(namespace: str, logical_key: str) -> Any:
        nonlocal task_reads, intent_row
        if namespace == "provision_tasks" and logical_key == str(employee_id):
            task_reads += 1
            if task_reads == 2:
                task_id, _ = await _seed_task(
                    facade,
                    employee_id,
                    state="PENDING",
                    domain_id=config.payload["domain_id"],
                    base_url=config.payload["base_url"],
                    user_intent_id=intent_id,
                )
                intent_row = {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": config.payload["domain_id"],
                    "local_user_id": employee_id,
                    "resource_kind": "USER",
                    "phase": "UNKNOWN",
                    "receipt_id": None,
                    "task_id": task_id,
                    "created_by_attempt_id": task_id,
                    "last_send_attempt_id": task_id,
                    "created_at": now,
                    "updated_at": now,
                    "parent_intent_id": None,
                    "remote_user_id": None,
                    "tenant_id": None,
                    "kb_id": None,
                    "absence_proof": None,
                }
                await facade.commit(
                    [],
                    retained_writes=[
                        RetainedRecordWrite(INTENT_NAMESPACE, intent_id, intent_row, 0)
                    ],
                )
        return await original_get(namespace, logical_key)

    monkeypatch.setattr(facade, "get_record", seed_unknown_pending_winner)
    with pytest.raises(PluginActionError) as raised:
        await facade.invoke_action(
            "provisioning.retry", PluginActor(employee_id, False), {"expected_revision": 0}
        )

    winner = await original_get("provision_tasks", str(employee_id))
    proof = await facade.get_retained_record(INTENT_NAMESPACE, intent_id)
    assert raised.value.code == "PROVISIONING_CONFLICT"
    assert winner is not None and winner.revision == 1 and winner.payload["state"] == "PENDING"
    assert proof is not None and proof.payload["phase"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_status_rejects_identity_change_during_projection(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, user_id = plugin_host
    original_get = facade.get_record
    changed = False

    async def change_email_on_task_read(namespace: str, logical_key: str) -> Any:
        nonlocal changed
        if not changed and namespace == "provision_tasks":
            changed = True
            UserRepo(db).set_email(user_id, "changed-during-status@example.test")
        return await original_get(namespace, logical_key)

    monkeypatch.setattr(facade, "get_record", change_email_on_task_read)
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action("provisioning.status", PluginActor(user_id, False), {})
    assert changed is True
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_intent", [False, True])
async def test_self_retry_rejects_employee_snapshot_change_before_identity_checks(
    plugin_host: tuple[Any, Any, int],
    monkeypatch: pytest.MonkeyPatch,
    has_intent: bool,
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="employee-snapshot-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="employee-snapshot-user", role="user", email="before@example.test"
    )
    await _save_config(facade, admin_id)
    config = await facade.get_record("provision_config", "primary")
    assert config is not None
    task_id, _ = await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        **(
            {
                "domain_id": config.payload["domain_id"],
                "base_url": config.payload["base_url"],
                "user_intent_id": str(uuid4()),
            }
            if has_intent
            else {}
        ),
    )
    if has_intent:
        from enterprise_knowledge.provisioning import INTENT_NAMESPACE

        intent_id = (await facade.get_record("provision_tasks", str(employee_id))).payload[
            "user_intent_id"
        ]
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        await facade.commit(
            [],
            retained_writes=[
                RetainedRecordWrite(
                    INTENT_NAMESPACE,
                    intent_id,
                    {
                        "schema_id": "octop.enterprise.provisioning.v1",
                        "version": 1,
                        "domain_id": config.payload["domain_id"],
                        "local_user_id": employee_id,
                        "resource_kind": "USER",
                        "phase": "PREPARED",
                        "receipt_id": None,
                        "task_id": task_id,
                        "created_by_attempt_id": str(uuid4()),
                        "last_send_attempt_id": None,
                        "created_at": now,
                        "updated_at": now,
                        "parent_intent_id": None,
                        "remote_user_id": None,
                        "tenant_id": None,
                        "kb_id": None,
                        "absence_proof": "NOT_SENT",
                    },
                    0,
                )
            ],
        )

    original_read = facade.read_user_identity
    employee_reads = 0

    async def change_identity_at_final_read(user_id: int) -> Any:
        nonlocal employee_reads
        if user_id == employee_id:
            employee_reads += 1
            if employee_reads == 2:
                UserRepo(db).set_email(employee_id, "after@example.test")
        return await original_read(user_id)

    monkeypatch.setattr(facade, "read_user_identity", change_identity_at_final_read)
    with pytest.raises(PluginActionError) as changed:
        await facade.invoke_action(
            "provisioning.retry", PluginActor(employee_id, False), {"expected_revision": 1}
        )

    task = await facade.get_record("provision_tasks", str(employee_id))
    index = await facade.get_record("provision_task_index", task_id)
    assert changed.value.code == "PROVISIONING_CONFLICT"
    assert employee_reads == 2
    assert task is not None and task.revision == 1 and task.payload["state"] == "RETRY_WAIT"
    assert task.payload["identity"]["email"] == "before@example.test"
    assert index is not None and index.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "disabled"])
async def test_admin_task_retry_reauthorizes_before_early_status_return(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username=f"retry-recheck-{change}", role="admin")
    employee_id = UserRepo(db).create(
        username=f"retry-recheck-employee-{change}", role="user", email="recheck@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id, state="PENDING")
    original_get = facade.get_record
    changed = False

    async def change_admin_after_config_read(namespace: str, logical_key: str) -> Any:
        nonlocal changed
        row = await original_get(namespace, logical_key)
        if not changed and namespace == "provision_config":
            changed = True
            if change == "role":
                UserRepo(db).set_role(admin_id, "user")
            else:
                UserRepo(db).set_disabled(admin_id, True)
        return row

    monkeypatch.setattr(facade, "get_record", change_admin_after_config_read)
    with pytest.raises(PluginActionError) as denied:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "recheck"},
        )

    row = await original_get("provision_tasks", str(employee_id))
    assert changed is True
    assert denied.value.code == "ACCESS_DENIED"
    assert row is not None and row.revision == 1 and row.payload["state"] == "PENDING"


@pytest.mark.asyncio
async def test_self_retry_rechecks_employee_identity_before_read_only_return(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="self-recheck-config-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="self-recheck-employee", role="user", email="before@example.test"
    )
    await _save_config(facade, admin_id)
    await _seed_task(facade, employee_id, state="PENDING")
    original_get = facade.get_record
    changed = False

    async def change_employee_after_config_read(namespace: str, logical_key: str) -> Any:
        nonlocal changed
        row = await original_get(namespace, logical_key)
        if not changed and namespace == "provision_config":
            changed = True
            UserRepo(db).set_email(employee_id, "after@example.test")
        return row

    monkeypatch.setattr(facade, "get_record", change_employee_after_config_read)
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action(
            "provisioning.retry", PluginActor(employee_id, False), {"expected_revision": 1}
        )
    assert changed is True
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_admin_task_list_reauthorizes_after_identity_page_read(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="list-recheck-admin", role="admin")
    await _save_config(facade, admin_id)
    original_list = facade.list_user_identities
    changed = False

    async def demote_admin_after_page(*, after_user_id: int, limit: int) -> Any:
        nonlocal changed
        page = await original_list(after_user_id=after_user_id, limit=limit)
        if not changed:
            changed = True
            UserRepo(db).set_role(admin_id, "user")
        return page

    monkeypatch.setattr(facade, "list_user_identities", demote_admin_after_page)
    with pytest.raises(PluginActionError) as denied:
        await facade.invoke_action(
            "provisioning.tasks.list",
            PluginActor(admin_id, True),
            {"after_user_id": 0, "limit": 10},
        )
    assert changed is True
    assert denied.value.code == "ACCESS_DENIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list", "task.retry"])
async def test_admin_task_reads_reject_deleted_distinct_actor_without_writing(
    plugin_host: tuple[Any, Any, int],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    db, facade, _user_id = plugin_host
    owner_id = UserRepo(db).create(username=f"delete-actor-owner-{action}", role="admin")
    actor_id = UserRepo(db).create(username=f"delete-actor-admin-{action}", role="admin")
    employee_id = UserRepo(db).create(
        username=f"delete-actor-employee-{action}", role="user", email="delete@example.test"
    )
    await _save_config(facade, owner_id)
    task_id, _ = await _seed_task(facade, employee_id, state="PENDING")
    before = await facade.get_record("provision_tasks", str(employee_id))
    assert before is not None
    deleted = False

    if action == "list":
        original_list = facade.list_user_identities

        async def delete_actor_after_identity_page(*, after_user_id: int, limit: int) -> Any:
            nonlocal deleted
            page = await original_list(after_user_id=after_user_id, limit=limit)
            if not deleted:
                deleted = True
                UserRepo(db).delete(actor_id)
            return page

        monkeypatch.setattr(facade, "list_user_identities", delete_actor_after_identity_page)
        invocation = facade.invoke_action(
            "provisioning.tasks.list",
            PluginActor(actor_id, True),
            {"after_user_id": 0, "limit": 10},
        )
    else:
        original_get = facade.get_record

        async def delete_actor_after_task_read(namespace: str, logical_key: str) -> Any:
            nonlocal deleted
            row = await original_get(namespace, logical_key)
            if not deleted and namespace == "provision_tasks" and logical_key == str(employee_id):
                deleted = True
                UserRepo(db).delete(actor_id)
            return row

        monkeypatch.setattr(facade, "get_record", delete_actor_after_task_read)
        invocation = facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(actor_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "deleted actor"},
        )

    with pytest.raises(PluginActionError) as denied:
        await invocation

    after = (
        await original_get("provision_tasks", str(employee_id))
        if action == "task.retry"
        else await facade.get_record("provision_tasks", str(employee_id))
    )
    index = await facade.get_record("provision_task_index", task_id)
    assert deleted and denied.value.code == "ACCESS_DENIED"
    assert UserRepo(db).get(actor_id) is None
    assert UserRepo(db).get(owner_id) is not None
    assert UserRepo(db).get(employee_id) is not None
    assert before is not None and after is not None
    assert after.revision == before.revision == 1 and after.payload == before.payload
    assert index is not None and index.revision == 1


@pytest.mark.asyncio
async def test_admin_task_list_rejects_identity_change_after_output_projection(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="list-identity-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="list-identity-employee", role="user", email="before@example.test"
    )
    await _save_config(facade, admin_id)
    await _seed_task(facade, employee_id, state="RETRY_WAIT")
    original_get = facade.get_record
    changed = False

    async def change_employee_after_task_read(namespace: str, logical_key: str) -> Any:
        nonlocal changed
        row = await original_get(namespace, logical_key)
        if not changed and namespace == "provision_tasks" and logical_key == str(employee_id):
            changed = True
            UserRepo(db).set_email(employee_id, "after@example.test")
        return row

    monkeypatch.setattr(facade, "get_record", change_employee_after_task_read)
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action(
            "provisioning.tasks.list",
            PluginActor(admin_id, True),
            {"after_user_id": 0, "limit": 10},
        )
    assert changed is True
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_task_retry_unknown_intent_overrides_retryable_task_state(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _unused_user = plugin_host
    employee_id = UserRepo(db).create(
        username="employee-with-unknown", role="user", email="employee@example.test"
    )
    admin_id = UserRepo(db).create(username="provision-admin", role="admin")
    from enterprise_knowledge.provisioning import INTENT_NAMESPACE
    from enterprise_knowledge.provisioning_records import (
        ProvisioningIdentity,
        ProvisioningTaskRecord,
    )
    from enterprise_knowledge.provisioning_state import TASK_NAMESPACE

    employee = await facade.read_user_identity(employee_id)
    task_id, intent_id, domain_id = (
        "a45dfafd-2451-4fd6-8820-28b48b82f64e",
        "f90f34df-8642-42d3-8e58-59f92e39c43b",
        "e4ff9a3c-b248-4598-b8a3-2a16a8d2d18e",
    )
    now = "2026-10-25T12:34:56Z"
    task = ProvisioningTaskRecord(
        schema_id="octop.enterprise.provisioning.v1",
        version=1,
        task_id=task_id,
        identity=ProvisioningIdentity(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            user_id=employee.user_id,
            email=employee.email,
            disabled=employee.disabled,
            snapshot_token=employee.snapshot_token,
        ),
        state="PENDING",
        error_code=None,
        checkpoint="NONE",
        domain_id=domain_id,
        base_url="https://weknora.example/api/v1",
        user_intent_id=intent_id,
        kb_intent_id=None,
        key_intent_id=None,
        bootstrap_ref=None,
        bootstrap_revision=None,
        bootstrap_created_at=None,
        bootstrap_expires_at=None,
        final_key_ref=None,
        final_key_revision=None,
        remote_user_id=None,
        tenant_id=None,
        kb_id=None,
        key_id=None,
        attempt=None,
        consecutive_failures=0,
        updated_at=now,
        last_attempt_at=None,
        next_retry_at=None,
        last_retry=None,
    )
    intent = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": domain_id,
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "phase": "UNKNOWN",
        "receipt_id": None,
        "task_id": task_id,
        "created_by_attempt_id": task_id,
        "last_send_attempt_id": task_id,
        "created_at": now,
        "updated_at": now,
        "parent_intent_id": None,
        "remote_user_id": None,
        "tenant_id": None,
        "kb_id": None,
        "absence_proof": None,
    }
    await facade.commit(
        [
            RecordWrite(
                TASK_NAMESPACE, str(employee_id), employee_id, task.model_dump(mode="python"), 0
            ),
            RecordWrite(
                "provision_task_index",
                task_id,
                employee_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "local_user_id": employee_id,
                },
                0,
            ),
        ],
        identity_checks=[UserIdentityCheck(employee)],
        retained_writes=[RetainedRecordWrite(INTENT_NAMESPACE, intent_id, intent, 0)],
    )
    await facade.commit(
        [
            RecordWrite(
                TASK_NAMESPACE, str(employee_id), employee_id, task.model_dump(mode="python"), 1
            )
        ],
        identity_checks=[UserIdentityCheck(employee)],
    )
    projected = await facade.invoke_action(
        "provisioning.status", PluginActor(employee_id, False), {}
    )
    assert projected["state"] == "MANUAL_REVIEW"
    with pytest.raises(PluginActionError) as stale:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "operator retry"},
        )
    reread = await facade.get_record(TASK_NAMESPACE, str(employee_id))
    assert stale.value.code == "PROVISIONING_CONFLICT"
    assert reread is not None and reread.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "absence_proof", "receipt_pointer", "expected_state"),
    [
        ("UNKNOWN", None, False, "MANUAL_REVIEW"),
        ("WRITE_PENDING", None, False, "MANUAL_REVIEW"),
        ("VERIFIED", None, False, "MANUAL_REVIEW"),
        ("VERIFIED", None, True, "MANUAL_REVIEW"),
        ("PREPARED", "NOT_SENT", False, "CONFIG_REQUIRED"),
        ("PREPARED", None, False, "MANUAL_REVIEW"),
        ("REJECTED", "DEFINITE_NO_EFFECT", False, "CONFIG_REQUIRED"),
        ("REJECTED", None, False, "MANUAL_REVIEW"),
    ],
)
async def test_intent_uncertainty_and_definite_absence_projection_matrix(
    plugin_host: tuple[Any, Any, int],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    absence_proof: str | None,
    receipt_pointer: bool,
    expected_state: str,
) -> None:
    _db, facade, employee_id = plugin_host
    task_id, intent_id, domain_id = str(uuid4()), str(uuid4()), str(uuid4())
    receipt_id = str(uuid4()) if receipt_pointer else None
    await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        domain_id=domain_id,
        base_url="https://weknora.example/api/v1",
        user_intent_id=intent_id,
    )
    task_row = await facade.get_record("provision_tasks", str(employee_id))
    assert task_row is not None
    task_id = task_row.payload["task_id"]
    now = "2026-10-25T12:34:56Z"
    intent = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": domain_id,
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "phase": phase,
        "receipt_id": receipt_id,
        "task_id": task_id,
        "created_by_attempt_id": str(uuid4()),
        "last_send_attempt_id": None,
        "created_at": now,
        "updated_at": now,
        "parent_intent_id": None,
        "remote_user_id": None,
        "tenant_id": None,
        "kb_id": None,
        "absence_proof": absence_proof,
    }
    await facade.commit(
        [], retained_writes=[RetainedRecordWrite("provision_intents", intent_id, intent, 0)]
    )
    status = await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert status["state"] == expected_state
    if expected_state == "MANUAL_REVIEW":
        assert status["error_code"] == "REMOTE_OUTCOME_UNKNOWN"
    admin_id = UserRepo(_db).create(
        username=f"proof-matrix-admin-{phase}-{receipt_pointer}", role="admin"
    )

    async def forbidden_commit(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("uncertain or configuration-blocked proof must not write on retry")

    monkeypatch.setattr(facade, "commit", forbidden_commit)
    result = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "proof matrix"},
    )
    assert result["state"] == expected_state


@pytest.mark.asyncio
@pytest.mark.parametrize("has_definite_no_effect", [True, False])
async def test_rejected_intent_retries_only_with_definite_no_effect_and_valid_config(
    plugin_host: tuple[Any, Any, int],
    monkeypatch: pytest.MonkeyPatch,
    has_definite_no_effect: bool,
) -> None:
    db, facade, _user_id = plugin_host
    owner_id = UserRepo(db).create(
        username=f"rejected-owner-{has_definite_no_effect}", role="admin"
    )
    employee_id = UserRepo(db).create(
        username=f"rejected-employee-{has_definite_no_effect}",
        role="user",
        email="rejected@example.test",
    )
    await _save_config(facade, owner_id)
    config = await facade.get_record("provision_config", "primary")
    assert config is not None
    intent_id = str(uuid4())
    actual_task_id, _ = await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        domain_id=config.payload["domain_id"],
        base_url=config.payload["base_url"],
        user_intent_id=intent_id,
    )
    task = await facade.get_record("provision_tasks", str(employee_id))
    assert task is not None
    now = "2026-10-25T12:34:56Z"
    intent = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": config.payload["domain_id"],
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "phase": "REJECTED",
        "receipt_id": None,
        "task_id": actual_task_id,
        "created_by_attempt_id": str(uuid4()),
        "last_send_attempt_id": str(uuid4()),
        "created_at": now,
        "updated_at": now,
        "parent_intent_id": None,
        "remote_user_id": None,
        "tenant_id": None,
        "kb_id": None,
        "absence_proof": "DEFINITE_NO_EFFECT" if has_definite_no_effect else None,
    }
    await facade.commit(
        [], retained_writes=[RetainedRecordWrite("provision_intents", intent_id, intent, 0)]
    )

    if has_definite_no_effect:
        result = await facade.invoke_action(
            "provisioning.retry", PluginActor(employee_id, False), {"expected_revision": 1}
        )
        updated = await facade.get_record("provision_tasks", str(employee_id))
        index = await facade.get_record("provision_task_index", actual_task_id)
        assert result["state"] == "PENDING" and result["revision"] == 2
        assert updated is not None and updated.revision == 2
        assert updated.payload["state"] == "PENDING"
        assert updated.payload["last_retry"]["kind"] == "SELF"
        assert index is not None and index.revision == 1
    else:

        async def forbidden_commit(*_args: Any, **_kwargs: Any) -> Any:
            pytest.fail("REJECTED without definite no-effect proof must not write")

        monkeypatch.setattr(facade, "commit", forbidden_commit)
        result = await facade.invoke_action(
            "provisioning.retry", PluginActor(employee_id, False), {"expected_revision": 1}
        )
        unchanged = await facade.get_record("provision_tasks", str(employee_id))
        assert result["state"] == "MANUAL_REVIEW"
        assert result["error_code"] == "REMOTE_OUTCOME_UNKNOWN"
        assert unchanged is not None and unchanged.revision == task.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_field",
    [
        "intent-task",
        "intent-domain",
        "intent-user",
        "kb-parent",
        "receipt-intent",
        "receipt-task",
        "receipt-domain",
        "receipt-user",
        "receipt-remote-user",
        "receipt-tenant",
    ],
)
async def test_coherent_user_receipt_rejects_one_field_link_or_typed_id_mutation(
    plugin_host: tuple[Any, Any, int], bad_field: str
) -> None:
    db, facade, _user_id = plugin_host
    owner_id = UserRepo(db).create(username=f"coherent-proof-owner-{bad_field}", role="admin")
    employee_id = UserRepo(db).create(
        username=f"coherent-proof-employee-{bad_field}", role="user", email="proof@example.test"
    )
    other_id = UserRepo(db).create(username=f"coherent-proof-other-{bad_field}", role="user")
    await _save_config(facade, owner_id)
    config = await facade.get_record("provision_config", "primary")
    assert config is not None
    user_intent_id, receipt_id = str(uuid4()), str(uuid4())
    kb_intent_id = str(uuid4()) if bad_field == "kb-parent" else None
    task_id, _ = await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        domain_id=config.payload["domain_id"],
        base_url=config.payload["base_url"],
        user_intent_id=user_intent_id,
        remote_user_id="remote-user",
        tenant_id=17,
        **({"kb_intent_id": kb_intent_id, "kb_id": "remote-kb"} if kb_intent_id else {}),
    )
    now = "2026-10-25T12:34:56Z"
    user_intent = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": config.payload["domain_id"],
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "phase": "VERIFIED",
        "receipt_id": receipt_id,
        "task_id": task_id,
        "created_by_attempt_id": task_id,
        "last_send_attempt_id": task_id,
        "created_at": now,
        "updated_at": now,
        "parent_intent_id": None,
        "remote_user_id": "remote-user",
        "tenant_id": 17,
        "kb_id": None,
        "absence_proof": None,
    }
    receipt = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "intent_id": user_intent_id,
        "domain_id": config.payload["domain_id"],
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "proof_kind": "CREATED_201",
        "task_id": task_id,
        "source_attempt_id": task_id,
        "recorded_at": now,
        "remote_user_id": "remote-user",
        "tenant_id": 17,
        "kb_id": None,
        "key_id": None,
    }
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite("provision_intents", user_intent_id, user_intent, 0),
            RetainedRecordWrite("provision_receipts", receipt_id, receipt, 0),
            *(
                [
                    RetainedRecordWrite(
                        "provision_intents",
                        kb_intent_id,
                        {
                            **user_intent,
                            "resource_kind": "KB",
                            "phase": "UNKNOWN",
                            "receipt_id": None,
                            "parent_intent_id": user_intent_id,
                            "kb_id": "remote-kb",
                            "absence_proof": None,
                        },
                        0,
                    )
                ]
                if kb_intent_id is not None
                else []
            ),
        ],
    )
    baseline = await facade.invoke_action(
        "provisioning.status", PluginActor(employee_id, False), {}
    )
    if kb_intent_id is None:
        assert baseline["state"] == "RETRY_WAIT" and baseline["can_retry"] is True
    else:
        assert baseline["state"] == "MANUAL_REVIEW"

    if bad_field == "kb-parent":
        namespace, key = "provision_intents", kb_intent_id
        changed = {
            **user_intent,
            "resource_kind": "KB",
            "phase": "UNKNOWN",
            "receipt_id": None,
            "parent_intent_id": str(uuid4()),
            "kb_id": "remote-kb",
            "absence_proof": None,
        }
    elif bad_field.startswith("intent-"):
        intent_fields: dict[str, tuple[str, Any]] = {
            "intent-task": ("task_id", str(uuid4())),
            "intent-domain": ("domain_id", str(uuid4())),
            "intent-user": ("local_user_id", other_id),
        }
        field, value = intent_fields[bad_field]
        namespace, key = "provision_intents", user_intent_id
        changed = {**user_intent, field: value}
    else:
        changed = dict(receipt)
        fields: dict[str, Any] = {
            "receipt-intent": ("intent_id", str(uuid4())),
            "receipt-task": ("task_id", str(uuid4())),
            "receipt-domain": ("domain_id", str(uuid4())),
            "receipt-user": ("local_user_id", other_id),
            "receipt-remote-user": ("remote_user_id", "other-user"),
            "receipt-tenant": ("tenant_id", 18),
        }
        field, value = fields[bad_field]
        changed[field] = value
        namespace, key = "provision_receipts", receipt_id
    await facade.commit([], retained_writes=[RetainedRecordWrite(namespace, key, changed, 1)])
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_finished_attempt_does_not_block_retry_of_retry_wait_task(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="finished-attempt-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="finished-attempt-employee", role="user", email="finished@example.test"
    )
    await _save_config(facade, admin_id)
    config = await facade.get_record("provision_config", "primary")
    anchor = await facade.get_retained_record("provision_domains", "primary")
    identity = await facade.read_user_identity(employee_id)
    assert config is not None and anchor is not None
    started = datetime.now(UTC) - timedelta(minutes=5)
    attempt = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "attempt_id": str(uuid4()),
        "mode": "ADVANCE",
        "config": {
            "schema_id": "octop.enterprise.provisioning.v1",
            "version": 1,
            "owner_user_id": admin_id,
            "config_revision": config.revision,
            "admin_credential_ref": config.payload["admin_credential_ref"],
            "admin_credential_revision": config.payload["admin_credential_revision"],
            "domain_id": config.payload["domain_id"],
            "base_url": config.payload["base_url"],
            "anchor_revision": anchor.revision,
        },
        "employee": {
            "schema_id": "octop.enterprise.provisioning.v1",
            "version": 1,
            "user_id": identity.user_id,
            "email": identity.email,
            "disabled": identity.disabled,
            "snapshot_token": identity.snapshot_token,
        },
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "deadline_at": (started + timedelta(seconds=90)).isoformat().replace("+00:00", "Z"),
        "lease_expires_at": (started + timedelta(seconds=45)).isoformat().replace("+00:00", "Z"),
        "finished_at": (started + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
    }
    await _seed_task(facade, employee_id, state="RETRY_WAIT", attempt=attempt)

    result = await facade.invoke_action(
        "provisioning.retry", PluginActor(employee_id, False), {"expected_revision": 1}
    )

    task = await facade.get_record("provision_tasks", str(employee_id))
    assert result["revision"] == 2 and result["state"] == "PENDING"
    assert task is not None and task.revision == 2 and task.payload["state"] == "PENDING"
    assert task.payload["attempt"]["finished_at"] is not None


@pytest.mark.asyncio
async def test_admin_list_returns_identity_page_cursor_without_seeding_empty_tasks(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, first_user = plugin_host
    admin_id = UserRepo(db).create(username="page-admin", role="admin")
    UserRepo(db).create(username="page-second-user", role="user", email="second@example.test")

    result = await facade.invoke_action(
        "provisioning.tasks.list",
        PluginActor(admin_id, True),
        {"after_user_id": 0, "limit": 1},
    )

    assert result["items"] == []
    assert result["next_user_id"] == first_user
    assert await facade.get_record("provision_tasks", str(first_user)) is None


@pytest.mark.asyncio
async def test_email_recovery_projects_pending_then_updates_same_task_on_retry(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="email-admin", role="admin")
    target_id = UserRepo(db).create(username="email-recovery", role="user")
    await _save_config(facade, admin_id)

    first = await facade.invoke_action(
        "provisioning.retry", PluginActor(target_id, False), {"expected_revision": 0}
    )
    task_before = await facade.get_record("provision_tasks", str(target_id))
    assert task_before is not None and first["state"] == "EMAIL_REQUIRED"
    task_id = task_before.payload["task_id"]

    UserRepo(db).set_email(target_id, "recovered@example.test")
    projected = await facade.invoke_action("provisioning.status", PluginActor(target_id, False), {})
    durable_before_retry = await facade.get_record("provision_tasks", str(target_id))
    assert projected["state"] == "PENDING" and projected["can_retry"] is True
    assert durable_before_retry is not None and durable_before_retry.revision == 1
    assert durable_before_retry.payload["state"] == "EMAIL_REQUIRED"

    retried = await facade.invoke_action(
        "provisioning.retry", PluginActor(target_id, False), {"expected_revision": 1}
    )
    durable_after_retry = await facade.get_record("provision_tasks", str(target_id))
    index = await facade.get_record("provision_task_index", task_id)
    assert retried["revision"] == 2 and retried["state"] == "PENDING"
    assert durable_after_retry is not None and durable_after_retry.payload["task_id"] == task_id
    assert durable_after_retry.payload["identity"]["email"] == "recovered@example.test"
    assert durable_after_retry.payload["last_retry"]["kind"] == "SELF"
    assert index is not None and index.revision == 1


@pytest.mark.asyncio
async def test_expired_bootstrap_is_manual_review_and_retry_is_read_only(
    plugin_host: tuple[Any, Any, int],
) -> None:
    _db, facade, user_id = plugin_host
    task_id, _revision = await _seed_task(
        facade,
        user_id,
        state="RETRY_WAIT",
        bootstrap_ref=str(uuid4()),
        bootstrap_revision=2,
        bootstrap_created_at="2026-10-23T12:00:00Z",
        bootstrap_expires_at="2026-10-24T12:00:00Z",
    )

    status = await facade.invoke_action("provisioning.status", PluginActor(user_id, False), {})
    retry = await facade.invoke_action(
        "provisioning.retry", PluginActor(user_id, False), {"expected_revision": 1}
    )
    row = await facade.get_record("provision_tasks", str(user_id))
    assert status["state"] == retry["state"] == "MANUAL_REVIEW"
    assert status["error_code"] == retry["error_code"] == "BOOTSTRAP_EXPIRED"
    assert retry["can_retry"] is False
    assert row is not None and row.revision == 1 and row.payload["task_id"] == task_id


@pytest.mark.asyncio
async def test_bootstrap_expiring_during_retry_prechecks_stays_read_only(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, facade, employee_id = plugin_host
    admin_id = UserRepo(_db).create(username="expiring-admin", role="admin")
    await _save_config(facade, admin_id)
    provisioning_state = sys.modules[
        "enterprise_knowledge_state_plugin.enterprise_knowledge.provisioning_state"
    ]

    expires = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    clock = {"now": expires - timedelta(seconds=1)}

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            value = clock["now"]
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(provisioning_state, "datetime", ControlledDateTime)
    task_id, _ = await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        bootstrap_ref=str(uuid4()),
        bootstrap_revision=2,
        bootstrap_created_at="2026-10-25T12:00:00Z",
        bootstrap_expires_at="2026-10-26T12:00:00Z",
    )
    original_read_config = provisioning_state.ProvisioningStateService._read_config
    config_reads = 0

    async def advance_after_config_reread(service: Any, actor: PluginActor) -> Any:
        nonlocal config_reads
        result = await original_read_config(service, actor)
        config_reads += 1
        if config_reads == 2:
            clock["now"] = expires + timedelta(microseconds=1)
        return result

    monkeypatch.setattr(
        provisioning_state.ProvisioningStateService, "_read_config", advance_after_config_reread
    )
    result = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "expiry boundary"},
    )
    row = await facade.get_record("provision_tasks", str(employee_id))
    assert config_reads == 2
    assert result["state"] == "MANUAL_REVIEW"
    assert result["error_code"] == "BOOTSTRAP_EXPIRED"
    assert row is not None and row.revision == 1 and row.payload["state"] == "RETRY_WAIT"


@pytest.mark.asyncio
async def test_retained_proof_change_between_read_and_commit_fails_cas(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    employee_id = UserRepo(db).create(
        username="proof-race-employee", role="user", email="proof@example.test"
    )
    admin_id = UserRepo(db).create(username="proof-race-admin", role="admin")
    await _save_config(facade, admin_id)
    task_id = str(uuid4())
    intent_id = str(uuid4())
    anchor = await facade.get_retained_record("provision_domains", "primary")
    assert anchor is not None
    domain_id = anchor.payload["domain_id"]
    now = "2026-10-25T12:34:56Z"
    employee = await facade.read_user_identity(employee_id)
    from enterprise_knowledge.provisioning import INTENT_NAMESPACE
    from enterprise_knowledge.provisioning_records import (
        ProvisioningIdentity,
        ProvisioningTaskRecord,
    )
    from enterprise_knowledge.provisioning_state import TASK_INDEX_NAMESPACE, TASK_NAMESPACE

    task = ProvisioningTaskRecord(
        schema_id="octop.enterprise.provisioning.v1",
        version=1,
        task_id=task_id,
        identity=ProvisioningIdentity(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            user_id=employee_id,
            email=employee.email,
            disabled=employee.disabled,
            snapshot_token=employee.snapshot_token,
        ),
        state="RETRY_WAIT",
        error_code=None,
        checkpoint="NONE",
        domain_id=domain_id,
        base_url="https://weknora.example/api/v1",
        user_intent_id=intent_id,
        kb_intent_id=None,
        key_intent_id=None,
        bootstrap_ref=None,
        bootstrap_revision=None,
        bootstrap_created_at=None,
        bootstrap_expires_at=None,
        final_key_ref=None,
        final_key_revision=None,
        remote_user_id=None,
        tenant_id=None,
        kb_id=None,
        key_id=None,
        attempt=None,
        consecutive_failures=0,
        updated_at=now,
        last_attempt_at=None,
        next_retry_at=None,
        last_retry=None,
    )
    intent = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": domain_id,
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "phase": "PREPARED",
        "receipt_id": None,
        "task_id": task_id,
        "created_by_attempt_id": task_id,
        "last_send_attempt_id": None,
        "created_at": now,
        "updated_at": now,
        "parent_intent_id": None,
        "remote_user_id": None,
        "tenant_id": None,
        "kb_id": None,
        "absence_proof": "NOT_SENT",
    }
    await facade.commit(
        [
            RecordWrite(
                TASK_NAMESPACE, str(employee_id), employee_id, task.model_dump(mode="python"), 0
            ),
            RecordWrite(
                TASK_INDEX_NAMESPACE,
                task_id,
                employee_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "local_user_id": employee_id,
                },
                0,
            ),
        ],
        identity_checks=[UserIdentityCheck(employee)],
        retained_writes=[RetainedRecordWrite(INTENT_NAMESPACE, intent_id, intent, 0)],
    )
    original_commit = facade.commit
    raced = False

    async def change_proof_then_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal raced
        if not raced and any(write.namespace == TASK_NAMESPACE for write in writes):
            raced = True
            changed = {**intent, "phase": "UNKNOWN", "updated_at": "2026-10-25T12:35:00Z"}
            await original_commit(
                [], retained_writes=[RetainedRecordWrite(INTENT_NAMESPACE, intent_id, changed, 1)]
            )
        return await original_commit(writes, *args, **kwargs)

    monkeypatch.setattr(facade, "commit", change_proof_then_commit)
    with pytest.raises(PluginActionError) as conflict:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "review"},
        )
    assert conflict.value.code == "PROVISIONING_CONFLICT"
    row = await facade.get_record(TASK_NAMESPACE, str(employee_id))
    proof = await facade.get_retained_record(INTENT_NAMESPACE, intent_id)
    assert row is not None and row.revision == 1 and row.payload["state"] == "RETRY_WAIT"
    assert proof is not None and proof.revision == 2 and proof.payload["phase"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_receipt_revision_change_between_read_and_commit_fails_cas(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="receipt-race-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="receipt-race-employee", role="user", email="receipt@example.test"
    )
    await _save_config(facade, admin_id)
    config = await facade.get_record("provision_config", "primary")
    assert config is not None
    task_id, intent_id, receipt_id = str(uuid4()), str(uuid4()), str(uuid4())
    now = "2026-10-25T12:34:56Z"
    await _seed_task(
        facade,
        employee_id,
        state="RETRY_WAIT",
        domain_id=config.payload["domain_id"],
        base_url=config.payload["base_url"],
        user_intent_id=intent_id,
        remote_user_id="remote-user",
        tenant_id=17,
    )
    task_row = await facade.get_record("provision_tasks", str(employee_id))
    assert task_row is not None
    task_id = task_row.payload["task_id"]
    intent = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": config.payload["domain_id"],
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "phase": "VERIFIED",
        "receipt_id": receipt_id,
        "task_id": task_id,
        "created_by_attempt_id": task_id,
        "last_send_attempt_id": task_id,
        "created_at": now,
        "updated_at": now,
        "parent_intent_id": None,
        "remote_user_id": "remote-user",
        "tenant_id": 17,
        "kb_id": None,
        "absence_proof": None,
    }
    receipt = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "intent_id": intent_id,
        "domain_id": config.payload["domain_id"],
        "local_user_id": employee_id,
        "resource_kind": "USER",
        "proof_kind": "CREATED_201",
        "task_id": task_id,
        "source_attempt_id": task_id,
        "recorded_at": now,
        "remote_user_id": "remote-user",
        "tenant_id": 17,
        "kb_id": None,
        "key_id": None,
    }
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite("provision_intents", intent_id, intent, 0),
            RetainedRecordWrite("provision_receipts", receipt_id, receipt, 0),
        ],
    )
    original_commit = facade.commit
    changed = False

    async def change_receipt_before_task_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        if not changed and any(write.namespace == "provision_tasks" for write in writes):
            changed = True
            await original_commit(
                [],
                retained_writes=[
                    RetainedRecordWrite(
                        "provision_receipts",
                        receipt_id,
                        {**receipt, "recorded_at": "2026-10-25T12:35:00Z"},
                        1,
                    )
                ],
            )
        return await original_commit(writes, *args, **kwargs)

    monkeypatch.setattr(facade, "commit", change_receipt_before_task_commit)
    with pytest.raises(PluginActionError) as conflict:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "receipt CAS"},
        )
    task = await facade.get_record("provision_tasks", str(employee_id))
    receipt_row = await facade.get_retained_record("provision_receipts", receipt_id)
    assert changed and conflict.value.code == "PROVISIONING_CONFLICT"
    assert task is not None and task.revision == 1 and task.payload["state"] == "RETRY_WAIT"
    assert receipt_row is not None and receipt_row.revision == 2


@pytest.mark.asyncio
async def test_status_reads_at_most_three_intents_and_one_receipt_per_intent(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    employee_id = UserRepo(db).create(
        username="bounded-proof-employee", role="user", email="bounded@example.test"
    )
    admin_id = UserRepo(db).create(username="bounded-proof-admin", role="admin")
    await _save_config(facade, admin_id)
    config = await facade.get_record("provision_config", "primary")
    assert config is not None
    task_id, user_intent_id, kb_intent_id, key_intent_id = [str(uuid4()) for _ in range(4)]
    user_receipt_id, kb_receipt_id, key_receipt_id = [str(uuid4()) for _ in range(3)]
    domain_id = config.payload["domain_id"]
    now = "2026-10-25T12:34:56Z"
    employee = await facade.read_user_identity(employee_id)
    from enterprise_knowledge.provisioning import INTENT_NAMESPACE, RECEIPT_NAMESPACE
    from enterprise_knowledge.provisioning_records import (
        ProvisioningIdentity,
        ProvisioningTaskRecord,
    )
    from enterprise_knowledge.provisioning_state import TASK_INDEX_NAMESPACE, TASK_NAMESPACE

    task = ProvisioningTaskRecord(
        schema_id="octop.enterprise.provisioning.v1",
        version=1,
        task_id=task_id,
        identity=ProvisioningIdentity(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            user_id=employee_id,
            email=employee.email,
            disabled=False,
            snapshot_token=employee.snapshot_token,
        ),
        state="READY",
        error_code=None,
        checkpoint="READY",
        domain_id=domain_id,
        base_url="https://weknora.example/api/v1",
        user_intent_id=user_intent_id,
        kb_intent_id=kb_intent_id,
        key_intent_id=key_intent_id,
        bootstrap_ref=None,
        bootstrap_revision=None,
        bootstrap_created_at=None,
        bootstrap_expires_at=None,
        final_key_ref=str(uuid4()),
        final_key_revision=2,
        remote_user_id="user-7",
        tenant_id=17,
        kb_id="kb-7",
        key_id=23,
        attempt=None,
        consecutive_failures=0,
        updated_at=now,
        last_attempt_at=None,
        next_retry_at=None,
        last_retry=None,
    )
    intent_common = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": domain_id,
        "local_user_id": employee_id,
        "task_id": task_id,
        "created_by_attempt_id": task_id,
        "last_send_attempt_id": None,
        "created_at": now,
        "updated_at": now,
        "remote_user_id": "user-7",
        "tenant_id": 17,
        "absence_proof": None,
    }
    intents = [
        RetainedRecordWrite(
            INTENT_NAMESPACE,
            user_intent_id,
            {
                **intent_common,
                "resource_kind": "USER",
                "phase": "VERIFIED",
                "receipt_id": user_receipt_id,
                "parent_intent_id": None,
                "kb_id": None,
            },
            0,
        ),
        RetainedRecordWrite(
            INTENT_NAMESPACE,
            kb_intent_id,
            {
                **intent_common,
                "resource_kind": "KB",
                "phase": "VERIFIED",
                "receipt_id": kb_receipt_id,
                "parent_intent_id": user_intent_id,
                "kb_id": "kb-7",
            },
            0,
        ),
        RetainedRecordWrite(
            INTENT_NAMESPACE,
            key_intent_id,
            {
                **intent_common,
                "resource_kind": "KEY",
                "phase": "VERIFIED",
                "receipt_id": key_receipt_id,
                "parent_intent_id": kb_intent_id,
                "kb_id": "kb-7",
            },
            0,
        ),
    ]
    receipts = [
        RetainedRecordWrite(
            RECEIPT_NAMESPACE,
            user_receipt_id,
            {
                "schema_id": "octop.enterprise.provisioning.v1",
                "version": 1,
                "intent_id": user_intent_id,
                "domain_id": domain_id,
                "local_user_id": employee_id,
                "resource_kind": "USER",
                "proof_kind": "CREATED_201",
                "task_id": task_id,
                "source_attempt_id": task_id,
                "recorded_at": now,
                "remote_user_id": "user-7",
                "tenant_id": 17,
                "kb_id": None,
                "key_id": None,
            },
            0,
        ),
        RetainedRecordWrite(
            RECEIPT_NAMESPACE,
            kb_receipt_id,
            {
                "schema_id": "octop.enterprise.provisioning.v1",
                "version": 1,
                "intent_id": kb_intent_id,
                "domain_id": domain_id,
                "local_user_id": employee_id,
                "resource_kind": "KB",
                "proof_kind": "CREATED_201",
                "task_id": task_id,
                "source_attempt_id": task_id,
                "recorded_at": now,
                "remote_user_id": "user-7",
                "tenant_id": 17,
                "kb_id": "kb-7",
                "key_id": None,
            },
            0,
        ),
        RetainedRecordWrite(
            RECEIPT_NAMESPACE,
            key_receipt_id,
            {
                "schema_id": "octop.enterprise.provisioning.v1",
                "version": 1,
                "intent_id": key_intent_id,
                "domain_id": domain_id,
                "local_user_id": employee_id,
                "resource_kind": "KEY",
                "proof_kind": "CREATED_201",
                "task_id": task_id,
                "source_attempt_id": task_id,
                "recorded_at": now,
                "remote_user_id": "user-7",
                "tenant_id": 17,
                "kb_id": "kb-7",
                "key_id": 23,
            },
            0,
        ),
    ]
    await facade.commit(
        [
            RecordWrite(
                TASK_NAMESPACE, str(employee_id), employee_id, task.model_dump(mode="python"), 0
            ),
            RecordWrite(
                TASK_INDEX_NAMESPACE,
                task_id,
                employee_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "local_user_id": employee_id,
                },
                0,
            ),
        ],
        identity_checks=[UserIdentityCheck(employee)],
        retained_writes=[*intents, *receipts],
    )

    reads: list[tuple[str, str]] = []
    original_get = facade.get_retained_record

    async def count_reads(namespace: str, logical_key: str) -> Any:
        reads.append((namespace, logical_key))
        return await original_get(namespace, logical_key)

    monkeypatch.setattr(facade, "get_retained_record", count_reads)
    status = await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert status["state"] == "READY"
    assert sum(namespace == INTENT_NAMESPACE for namespace, _ in reads) == 3
    assert sum(namespace == RECEIPT_NAMESPACE for namespace, _ in reads) == 3
    task_row = await facade.get_record(TASK_NAMESPACE, str(employee_id))
    assert task_row is not None
    await facade.commit(
        [
            RecordWrite(
                TASK_NAMESPACE,
                str(employee_id),
                employee_id,
                task_row.payload,
                task_row.revision,
            )
        ]
    )
    with pytest.raises(PluginActionError) as stale_ready:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "stale ready"},
        )
    assert stale_ready.value.code == "PROVISIONING_CONFLICT"
    scans: list[str] = []

    async def forbidden_scan(namespace: str, *_args: Any, **_kwargs: Any) -> Any:
        scans.append(namespace)
        pytest.fail("local status and retry must not scan retained records")

    monkeypatch.setattr(facade, "list_retained_records", forbidden_scan)
    reads.clear()
    listed = await facade.invoke_action(
        "provisioning.tasks.list",
        PluginActor(admin_id, True),
        {"after_user_id": 0, "limit": 10},
    )
    assert listed["items"]
    assert sum(namespace == INTENT_NAMESPACE for namespace, _ in reads) == 3
    assert sum(namespace == RECEIPT_NAMESPACE for namespace, _ in reads) == 3
    reads.clear()
    retry = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 2, "reason": "bounded read"},
    )
    assert retry["state"] == "READY"
    assert sum(namespace == INTENT_NAMESPACE for namespace, _ in reads) == 3
    assert sum(namespace == RECEIPT_NAMESPACE for namespace, _ in reads) == 3
    assert scans == []
    task_row = await facade.get_record(TASK_NAMESPACE, str(employee_id))
    assert task_row is not None
    key_stored = {**task_row.payload, "state": "RETRY_WAIT", "checkpoint": "KEY_STORED"}
    await facade.commit(
        [RecordWrite(TASK_NAMESPACE, str(employee_id), employee_id, key_stored, task_row.revision)]
    )
    reads.clear()
    retried = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(admin_id, True),
        {"task_id": task_id, "expected_revision": 3, "reason": "key checkpoint retry"},
    )
    after_retry = await facade.get_record(TASK_NAMESPACE, str(employee_id))
    assert retried["state"] == "PENDING" and retried["revision"] == 4
    assert after_retry is not None
    assert after_retry.payload["checkpoint"] == "KEY_STORED"
    assert after_retry.payload["final_key_ref"] == task.final_key_ref
    assert after_retry.payload["key_id"] == 23
    assert sum(namespace == INTENT_NAMESPACE for namespace, _ in reads) == 3
    assert sum(namespace == RECEIPT_NAMESPACE for namespace, _ in reads) == 3
    key_intent = next(
        item
        for item in intents
        if item.namespace == INTENT_NAMESPACE and item.logical_key == key_intent_id
    )
    malformed = {
        **key_intent.payload,
        "phase": "PREPARED",
        "receipt_id": None,
        "absence_proof": "NOT_SENT",
    }
    await facade.commit(
        [],
        retained_writes=[RetainedRecordWrite(INTENT_NAMESPACE, key_intent_id, malformed, 1)],
    )
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_task_retry_missing_index_is_not_found_and_corrupt_pair_is_unavailable(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, employee_id = plugin_host
    admin_id = UserRepo(db).create(username="missing-index-admin", role="admin")
    absent_task_id = str(uuid4())
    with pytest.raises(PluginActionError) as missing:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": absent_task_id, "expected_revision": 1, "reason": "missing"},
        )
    assert missing.value.code == "PROVISIONING_NOT_FOUND"
    assert "Configuration was not found" not in str(missing.value)

    task_id = str(uuid4())
    identity = await facade.read_user_identity(employee_id)
    from enterprise_knowledge.provisioning_records import (
        ProvisioningIdentity,
        ProvisioningTaskRecord,
    )
    from enterprise_knowledge.provisioning_state import TASK_NAMESPACE

    task = ProvisioningTaskRecord(
        schema_id="octop.enterprise.provisioning.v1",
        version=1,
        task_id=task_id,
        identity=ProvisioningIdentity(
            schema_id="octop.enterprise.provisioning.v1",
            version=1,
            user_id=employee_id,
            email=identity.email,
            disabled=identity.disabled,
            snapshot_token=identity.snapshot_token,
        ),
        state="RETRY_WAIT",
        error_code=None,
        checkpoint="NONE",
        domain_id=None,
        base_url=None,
        user_intent_id=None,
        kb_intent_id=None,
        key_intent_id=None,
        bootstrap_ref=None,
        bootstrap_revision=None,
        bootstrap_created_at=None,
        bootstrap_expires_at=None,
        final_key_ref=None,
        final_key_revision=None,
        remote_user_id=None,
        tenant_id=None,
        kb_id=None,
        key_id=None,
        attempt=None,
        consecutive_failures=0,
        updated_at="2026-10-25T12:34:56Z",
        last_attempt_at=None,
        next_retry_at=None,
        last_retry=None,
    )
    await facade.commit(
        [
            RecordWrite(
                TASK_NAMESPACE, str(employee_id), employee_id, task.model_dump(mode="python"), 0
            )
        ],
        identity_checks=[UserIdentityCheck(identity)],
    )
    with pytest.raises(PluginActionError) as corrupt:
        await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert corrupt.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_task_retry_index_without_task_is_unavailable_for_live_employee(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="index-only-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="index-only-employee", role="user", email="index-only@example.test"
    )
    task_id = str(uuid4())
    await facade.commit(
        [
            RecordWrite(
                "provision_task_index",
                task_id,
                employee_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "local_user_id": employee_id,
                },
                0,
            )
        ]
    )
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "index only"},
        )
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_status_rejects_malformed_task_domain_without_config_anchor(
    plugin_host: tuple[Any, Any, int],
) -> None:
    _db, facade, employee_id = plugin_host
    await _seed_task(facade, employee_id)
    row = await facade.get_record("provision_tasks", str(employee_id))
    assert row is not None
    payload = {**row.payload, "domain_id": "not-a-uuid", "base_url": ""}
    await facade.commit(
        [RecordWrite("provision_tasks", str(employee_id), employee_id, payload, row.revision)]
    )
    with pytest.raises(PluginActionError) as unavailable:
        await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_deleted_employee_index_does_not_resurrect_task(
    plugin_host: tuple[Any, Any, int],
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="deleted-index-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="deleted-index-employee", role="user", email="deleted@example.test"
    )
    task_id, _ = await _seed_task(facade, employee_id)
    proof_id = str(uuid4())
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                proof_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": str(uuid4()),
                    "local_user_id": employee_id,
                    "resource_kind": "USER",
                    "phase": "UNKNOWN",
                    "receipt_id": None,
                    "task_id": task_id,
                    "created_by_attempt_id": str(uuid4()),
                    "last_send_attempt_id": str(uuid4()),
                    "created_at": "2026-10-25T12:34:56Z",
                    "updated_at": "2026-10-25T12:34:56Z",
                    "parent_intent_id": None,
                    "remote_user_id": None,
                    "tenant_id": None,
                    "kb_id": None,
                    "absence_proof": None,
                },
                0,
            )
        ],
    )
    UserRepo(db).delete(employee_id)
    assert await facade.get_record("provision_tasks", str(employee_id)) is None
    assert await facade.get_record("provision_task_index", task_id) is None
    assert await facade.get_retained_record("provision_intents", proof_id) is not None
    with pytest.raises(PluginActionError) as missing:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "deleted employee"},
        )
    assert missing.value.code == "PROVISIONING_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["index-owner", "index-local-user", "task-index", "index-task"]
)
async def test_task_and_index_relationship_mismatches_fail_closed(
    plugin_host: tuple[Any, Any, int], mismatch: str
) -> None:
    db, facade, employee_id = plugin_host
    admin_id = UserRepo(db).create(username=f"relationship-admin-{mismatch}", role="admin")
    other_id = UserRepo(db).create(username=f"relationship-other-{mismatch}", role="user")
    task_id, _ = await _seed_task(facade, employee_id)
    if mismatch == "index-task":
        rogue_id = str(uuid4())
        await facade.commit(
            [
                RecordWrite(
                    "provision_task_index",
                    rogue_id,
                    employee_id,
                    {
                        "schema_id": "octop.enterprise.provisioning.v1",
                        "version": 1,
                        "local_user_id": employee_id,
                    },
                    0,
                )
            ]
        )
        with pytest.raises(PluginActionError) as unavailable:
            await facade.invoke_action(
                "provisioning.task.retry",
                PluginActor(admin_id, True),
                {"task_id": rogue_id, "expected_revision": 1, "reason": "bad index link"},
            )
    elif mismatch == "task-index":
        row = await facade.get_record("provision_tasks", str(employee_id))
        assert row is not None
        await facade.commit(
            [
                RecordWrite(
                    "provision_tasks",
                    str(employee_id),
                    employee_id,
                    {**row.payload, "task_id": str(uuid4())},
                    row.revision,
                )
            ]
        )
        with pytest.raises(PluginActionError) as unavailable:
            await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    else:
        index = await facade.get_record("provision_task_index", task_id)
        assert index is not None
        if mismatch == "index-owner":
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE plugin_records SET owner_user_id = ? WHERE namespace = ? AND logical_key = ?",
                    (other_id, "provision_task_index", task_id),
                )
            with pytest.raises(PluginActionError) as unavailable:
                await facade.invoke_action(
                    "provisioning.status", PluginActor(employee_id, False), {}
                )
            assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"
            return
        await facade.commit(
            [
                RecordWrite(
                    "provision_task_index",
                    task_id,
                    employee_id,
                    {
                        **index.payload,
                        "local_user_id": other_id
                        if mismatch == "index-local-user"
                        else employee_id,
                    },
                    index.revision,
                )
            ]
        )
        with pytest.raises(PluginActionError) as unavailable:
            await facade.invoke_action("provisioning.status", PluginActor(employee_id, False), {})
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_identity_change_inside_retry_commit_is_rejected_by_host_identity_check(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    admin_id = UserRepo(db).create(username="identity-race-admin", role="admin")
    employee_id = UserRepo(db).create(
        username="identity-race-employee", role="user", email="before@example.test"
    )
    await _save_config(facade, admin_id)
    task_id, _ = await _seed_task(facade, employee_id)
    original_commit = facade.commit
    raced = False

    async def change_identity_then_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal raced
        if not raced and any(write.namespace == "provision_tasks" for write in writes):
            raced = True
            UserRepo(db).set_email(employee_id, "after@example.test")
        return await original_commit(writes, *args, **kwargs)

    monkeypatch.setattr(facade, "commit", change_identity_then_commit)
    with pytest.raises(PluginActionError) as conflict:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(admin_id, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "identity race"},
        )
    assert conflict.value.code == "PROVISIONING_CONFLICT"
    row = await facade.get_record("provision_tasks", str(employee_id))
    assert row is not None and row.revision == 1 and row.payload["state"] == "RETRY_WAIT"


@pytest.mark.asyncio
async def test_owner_delete_and_same_revision_config_recreation_rejects_old_pin(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, facade, _user_id = plugin_host
    old_owner = UserRepo(db).create(username="old-config-owner", role="admin")
    new_owner = UserRepo(db).create(username="new-config-owner", role="admin")
    employee_id = UserRepo(db).create(
        username="owner-race-employee", role="user", email="owner@example.test"
    )
    await _save_config(facade, old_owner)
    task_id, _ = await _seed_task(facade, employee_id)
    original_commit = facade.commit
    replaced = False

    async def replace_owner_config_then_commit(writes: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal replaced
        if not replaced and any(write.namespace == "provision_tasks" for write in writes):
            replaced = True
            UserRepo(db).delete(old_owner)
            await facade.invoke_action(
                "provisioning.config.save",
                PluginActor(new_owner, True),
                {
                    "expected_revision": 0,
                    "expected_anchor_revision": 1,
                    "name": "Replacement KB",
                    "base_url": "https://weknora.example/api/v1",
                    "enabled": True,
                    "system_admin_jwt": "replacement-test-token",
                },
            )
        return await original_commit(writes, *args, **kwargs)

    monkeypatch.setattr(facade, "commit", replace_owner_config_then_commit)
    with pytest.raises(PluginActionError) as conflict:
        await facade.invoke_action(
            "provisioning.task.retry",
            PluginActor(new_owner, True),
            {"task_id": task_id, "expected_revision": 1, "reason": "owner race"},
        )
    assert replaced is True
    assert conflict.value.code == "PROVISIONING_CONFLICT"
    row = await facade.get_record("provision_tasks", str(employee_id))
    config = await facade.get_record("provision_config", "primary")
    assert row is not None and row.revision == 1 and row.payload["state"] == "RETRY_WAIT"
    assert config is not None and config.revision == 1 and config.owner_user_id == new_owner


@pytest.mark.asyncio
async def test_cancelled_retry_settles_commit_and_never_replays_seed(
    plugin_host: tuple[Any, Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, facade, user_id = plugin_host
    original_commit = facade.commit
    entered = asyncio.Event()

    async def commit_then_fail(writes: Any, *args: Any, **kwargs: Any) -> Any:
        if any(write.namespace == "provision_tasks" for write in writes):
            entered.set()
            await asyncio.sleep(0.02)
            await original_commit(writes, *args, **kwargs)
            raise RuntimeError("post-commit uncertainty")
        return await original_commit(writes, *args, **kwargs)

    monkeypatch.setattr(facade, "commit", commit_then_fail)
    action = asyncio.create_task(
        facade.invoke_action(
            "provisioning.retry", PluginActor(user_id, False), {"expected_revision": 0}
        )
    )
    await entered.wait()
    action.cancel()
    with pytest.raises(asyncio.CancelledError):
        await action
    task = await facade.get_record("provision_tasks", str(user_id))
    assert task is not None and task.revision == 1
    index = await facade.get_record("provision_task_index", task.payload["task_id"])
    assert index is not None and index.revision == 1
