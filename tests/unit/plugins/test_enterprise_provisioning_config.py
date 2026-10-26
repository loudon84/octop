from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))


def _host_provisioning(tmp_path: Path, username: str) -> tuple[Any, Any, int, Any, Any]:
    from enterprise_knowledge.provisioning import ProvisioningService
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "host-provisioning.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username=username, role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    return db, services, actor_id, facade, ProvisioningService(facade)


def _host_provisioning_session(db_path: Path, tmp_path: Path) -> tuple[Any, Any, Any, Any, Any]:
    from enterprise_knowledge.provisioning import ProvisioningService
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(db_path)
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    return db, services, host, facade, ProvisioningService(facade)


@pytest.mark.parametrize(
    "bad_value",
    [True, 1.0, "1"],
)
def test_internal_record_version_requires_strict_integer(bad_value: object) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningDomainRecord

    with pytest.raises(ValidationError):
        ProvisioningDomainRecord.model_validate(
            {
                "schema_id": "octop.enterprise.provisioning.v1",
                "version": bad_value,
                "domain_id": str(uuid4()),
                "canonical_base_url": "https://weknora.example/api/v1",
            }
        )


@pytest.mark.parametrize(
    ("model_name", "payload"),
    [
        (
            "ProvisioningDomainRecord",
            {
                "domain_id": str(uuid4()),
                "canonical_base_url": "https://weknora.example/api/v1",
            },
        ),
        (
            "ProvisioningConfigRecord",
            {
                "domain_id": str(uuid4()),
                "name": "Shared KB",
                "base_url": "https://weknora.example/api/v1",
                "enabled": True,
                "admin_credential_ref": str(uuid4()),
                "admin_credential_revision": 2,
                "updated_at": "2026-10-25T12:34:56Z",
            },
        ),
        (
            "ProvisioningAuthBlockRecord",
            {
                "domain_id": str(uuid4()),
                "credential_revision": 2,
                "http_status": 401,
                "rejected_at": "2026-10-25T12:34:56Z",
            },
        ),
        (
            "ProvisioningConfigPreparationRecord",
            {
                "domain_id": str(uuid4()),
                "expected_config_revision": 0,
                "expected_anchor_revision": 0,
                "previous_credential_ref": None,
                "previous_credential_revision": None,
                "planned_credential_ref": str(uuid4()),
                "planned_active_revision": 2,
                "state": "PREPARING",
                "staged_cleanup": "PENDING",
                "created_at": "2026-10-25T12:34:56Z",
                "updated_at": "2026-10-25T12:34:56Z",
            },
        ),
        (
            "ProvisioningIntentHeader",
            {
                "domain_id": str(uuid4()),
                "local_user_id": 7,
                "resource_kind": "USER",
                "phase": "PREPARED",
                "receipt_id": None,
            },
        ),
        (
            "ProvisioningReceiptHeader",
            {
                "intent_id": str(uuid4()),
                "domain_id": str(uuid4()),
                "local_user_id": 7,
                "resource_kind": "USER",
                "proof_kind": "OWNER_MATCH",
            },
        ),
    ],
)
@pytest.mark.parametrize("missing", [("schema_id",), ("version",), ("schema_id", "version")])
def test_persisted_record_types_require_explicit_schema_and_version(
    model_name: str, payload: dict[str, Any], missing: tuple[str, ...]
) -> None:
    import enterprise_knowledge.provisioning_records as records

    complete = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        **payload,
    }
    for key in missing:
        complete.pop(key)
    model = getattr(records, model_name)

    with pytest.raises(ValidationError):
        model.model_validate(complete)


def test_preparation_state_and_staging_cleanup_must_be_consistent() -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningConfigPreparationRecord

    now = "2026-10-25T12:34:56Z"
    data = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": str(uuid4()),
        "expected_config_revision": 0,
        "expected_anchor_revision": 0,
        "previous_credential_ref": None,
        "previous_credential_revision": None,
        "planned_credential_ref": str(uuid4()),
        "planned_active_revision": 2,
        "state": "PREPARING",
        "staged_cleanup": "PENDING",
        "created_at": now,
        "updated_at": now,
    }
    record = ProvisioningConfigPreparationRecord.model_validate(data)
    assert record.state == "PREPARING"

    with pytest.raises(ValidationError):
        ProvisioningConfigPreparationRecord.model_validate(
            {**data, "state": "COMMITTED", "staged_cleanup": "PENDING"}
        )
    with pytest.raises(ValidationError):
        ProvisioningConfigPreparationRecord.model_validate({**data, "planned_active_revision": 2.0})


@pytest.mark.parametrize("missing", ["schema_id", "version"])
@pytest.mark.asyncio
async def test_retained_reader_rejects_header_without_explicit_schema_or_version(
    tmp_path: Path, missing: str
) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningRetainedListInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RetainedRecordWrite,
        UserIdentityCheck,
    )
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / f"missing-retained-{missing}.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username=f"missing-retained-{missing}", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    intent_id = str(uuid4())
    payload: dict[str, Any] = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": str(uuid4()),
        "local_user_id": 21,
        "resource_kind": "USER",
        "phase": "UNKNOWN",
        "receipt_id": None,
    }
    del payload[missing]
    await facade.commit(
        [],
        retained_writes=[RetainedRecordWrite("provision_intents", intent_id, payload, 0)],
        identity_checks=[UserIdentityCheck(await facade.read_user_identity(actor_id))],
    )

    with pytest.raises(PluginActionError) as exc:
        await ProvisioningService(facade).list_retained(
            PluginActor(actor_id, True), ProvisioningRetainedListInput()
        )
    assert exc.value.code == "PROVISIONING_UNAVAILABLE"
    assert "21" not in str(exc.value)


@pytest.mark.asyncio
async def test_missing_config_projects_empty_dto_without_creating_records(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="get-config-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")

    result = await ProvisioningService(facade).get_config(PluginActor(actor_id, True))

    assert result.model_dump(mode="json") == {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "revision": 0,
        "anchor_revision": 0,
        "name": None,
        "base_url": None,
        "enabled": False,
        "credential_configured": False,
        "updated_at": None,
    }
    assert await facade.get_record("provision_config", "primary") is None
    assert await facade.get_retained_record("provision_domains", "primary") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revocation_point", "reauthorization_number"),
    [("entry", 1), ("prepare", 2), ("final", 3)],
)
async def test_role_revocation_is_checked_at_each_save_authority_boundary(
    tmp_path: Path, revocation_point: str, reauthorization_number: int
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.repos.users import UserRepo

    _, services, actor_id, facade, service = _host_provisioning(
        tmp_path, f"revoke-{revocation_point}-admin"
    )
    users = UserRepo(services.db)
    real_reauthorize = facade.reauthorize_actor
    calls = 0

    async def revoke_before_boundary(actor: PluginActor, permission: str) -> PluginActor:
        nonlocal calls
        calls += 1
        if calls == reauthorization_number:
            users.set_role(actor_id, "user")
        return await real_reauthorize(actor, permission)

    facade.reauthorize_actor = revoke_before_boundary
    with pytest.raises(PluginActionError) as denied:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-authority-boundary"),
            ),
        )
    facade.reauthorize_actor = real_reauthorize

    assert denied.value.code == "ACCESS_DENIED"
    assert calls == reauthorization_number
    assert await facade.get_record("provision_config", "primary") is None
    preparations = await facade.list_records("provision_config_preparations")
    if revocation_point == "final":
        assert len(preparations) == 1
        assert preparations[0].payload["state"] == "ABORTED"
        assert preparations[0].payload["staged_cleanup"] == "RELEASED"
        planned_ref = preparations[0].payload["planned_credential_ref"]
        assert (
            services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id)
            is None
        )
    else:
        assert preparations == []


@pytest.mark.asyncio
async def test_null_name_and_enabled_updates_keep_auth_block_and_late_old_block_isolated(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import (
        PluginActor,
        RecordWrite,
        UserIdentityCheck,
    )

    _, _, actor_id, facade, service = _host_provisioning(tmp_path, "block-retention-admin")
    actor = PluginActor(actor_id, True)
    first = await service.save_config(
        actor,
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Initial",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-block-token"),
        ),
    )
    config = await facade.get_record("provision_config", "primary")
    old_ref = config.payload["admin_credential_ref"]
    identity = await facade.read_user_identity(actor_id)
    await facade.commit(
        [
            RecordWrite(
                "provision_auth_blocks",
                old_ref,
                actor_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": config.payload["domain_id"],
                    "credential_revision": 2,
                    "http_status": 401,
                    "rejected_at": "2026-10-25T12:34:56Z",
                },
                0,
            )
        ],
        identity_checks=[UserIdentityCheck(identity)],
    )

    name_only = await service.save_config(
        actor,
        ProvisioningConfigSaveInput(
            expected_revision=first.revision,
            expected_anchor_revision=first.anchor_revision,
            name="Renamed",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=None,
        ),
    )
    enabled_only = await service.save_config(
        actor,
        ProvisioningConfigSaveInput(
            expected_revision=name_only.revision,
            expected_anchor_revision=name_only.anchor_revision,
            name="Renamed",
            base_url="https://weknora.example/api/v1",
            enabled=False,
            system_admin_jwt=None,
        ),
    )
    reenabled = await service.save_config(
        actor,
        ProvisioningConfigSaveInput(
            expected_revision=enabled_only.revision,
            expected_anchor_revision=enabled_only.anchor_revision,
            name="Renamed",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=None,
        ),
    )
    same = await facade.get_record("provision_config", "primary")
    block = await facade.get_record("provision_auth_blocks", old_ref)
    assert same.payload["admin_credential_ref"] == old_ref
    assert block is not None and block.payload["http_status"] == 401
    assert reenabled.revision == enabled_only.revision + 1
    assert reenabled.enabled is True

    rotated = await service.save_config(
        actor,
        ProvisioningConfigSaveInput(
            expected_revision=reenabled.revision,
            expected_anchor_revision=reenabled.anchor_revision,
            name="Renamed",
            base_url="https://weknora.example/api/v1",
            enabled=False,
            system_admin_jwt=SecretStr("synthetic-block-token-next"),
        ),
    )
    after_rotation = await facade.get_record("provision_config", "primary")
    new_ref = after_rotation.payload["admin_credential_ref"]
    assert rotated.revision == reenabled.revision + 1 and new_ref != old_ref
    await facade.commit(
        [
            RecordWrite(
                "provision_auth_blocks",
                old_ref,
                actor_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": after_rotation.payload["domain_id"],
                    "credential_revision": 2,
                    "http_status": 403,
                    "rejected_at": "2026-10-25T12:35:00Z",
                },
                block.revision,
            )
        ],
        identity_checks=[UserIdentityCheck(await facade.read_user_identity(actor_id))],
    )
    current_block = await facade.get_record("provision_auth_blocks", new_ref)
    old_block = await facade.get_record("provision_auth_blocks", old_ref)
    projected = await service.get_config(actor)
    assert projected.revision == rotated.revision and projected.credential_configured is True
    assert current_block is None
    assert old_block.payload["http_status"] == 403


@pytest.mark.asyncio
async def test_corrupt_active_credential_payload_is_unavailable_without_secret_leak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.connectors.crypto import encrypt_credentials

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "corrupt-payload-admin")
    await service.save_config(
        PluginActor(actor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-original-payload"),
        ),
    )
    config = await facade.get_record("provision_config", "primary")
    credential_ref = config.payload["admin_credential_ref"]
    corrupted = encrypt_credentials(
        services.secret_repo,
        {
            "schema_id": "octop.enterprise.provisioning.v1",
            "version": 1,
            "system_admin_jwt": "synthetic-corrupt-secret",
            "unexpected": "synthetic-corrupt-detail",
        },
    )
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE plugin_credentials SET ciphertext = ? WHERE credential_id = ?",
            (corrupted, credential_ref),
        )

    with pytest.raises(PluginActionError) as unavailable:
        await service.get_config(PluginActor(actor_id, True))
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"
    assert "synthetic" not in str(unavailable.value)
    assert unavailable.value.__cause__ is None
    assert unavailable.value.__suppress_context__ is True
    assert "synthetic" not in repr(unavailable.value)
    assert "synthetic" not in caplog.text

    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE plugin_credentials SET ciphertext = ? WHERE credential_id = ?",
            (b"not-a-valid-fernet-token", credential_ref),
        )
    with pytest.raises(PluginActionError) as damaged_ciphertext:
        await service.get_config(PluginActor(actor_id, True))
    assert damaged_ciphertext.value.code == "PROVISIONING_UNAVAILABLE"
    assert "fernet" not in str(damaged_ciphertext.value).lower()
    with pytest.raises(PluginActionError) as damaged_save:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=1,
                expected_anchor_revision=1,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=False,
                system_admin_jwt=None,
            ),
        )
    assert damaged_save.value.code == "PROVISIONING_UNAVAILABLE"
    assert "fernet" not in str(damaged_save.value).lower()


@pytest.mark.asyncio
async def test_owner_identity_change_before_mixed_commit_rolls_back_new_active_key(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.repos.users import UserRepo

    _, services, editor_id, facade, service = _host_provisioning(tmp_path, "owner-race-editor")
    users = UserRepo(services.db)
    owner_id = users.create(username="owner-race-owner", role="admin")
    await service.save_config(
        PluginActor(owner_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-owner-race-old"),
        ),
    )
    before = await facade.get_record("provision_config", "primary")
    old_ref = before.payload["admin_credential_ref"]
    users.set_disabled(owner_id, True)
    # A fresh, disabled-owner identity snapshot is accepted and remains the original owner.
    accepted = await service.save_config(
        PluginActor(editor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=before.revision,
            expected_anchor_revision=1,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=None,
        ),
    )
    assert (await facade.get_record("provision_config", "primary")).owner_user_id == owner_id
    users.set_disabled(owner_id, False)
    real_commit = facade.commit

    async def change_owner_identity_then_commit(*args: Any, **kwargs: Any) -> Any:
        writes = args[0]
        if any(write.namespace == "provision_config" for write in writes):
            users.set_disabled(owner_id, True)
        return await real_commit(*args, **kwargs)

    facade.commit = change_owner_identity_then_commit
    with pytest.raises(PluginActionError) as conflict:
        await service.save_config(
            PluginActor(editor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=accepted.revision,
                expected_anchor_revision=1,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-owner-race-new"),
            ),
        )
    facade.commit = real_commit

    assert conflict.value.code == "PROVISIONING_CONFLICT"
    after = await facade.get_record("provision_config", "primary")
    assert after.owner_user_id == owner_id
    assert after.payload["admin_credential_ref"] == old_ref
    old_credential = services.plugin_credential_repo.get("enterprise-knowledge", old_ref, owner_id)
    assert old_credential is not None and old_credential.state == "active"
    preps = await facade.list_records("provision_config_preparations")
    failed = next(row for row in preps if row.payload["state"] == "ABORTED")
    assert failed.payload["staged_cleanup"] == "RELEASED"
    assert (
        services.plugin_credential_repo.get(
            "enterprise-knowledge", failed.payload["planned_credential_ref"], owner_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_demoted_config_owner_can_be_rotated_by_another_admin_without_transfer(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor
    from octop.infra.db.repos.users import UserRepo

    _, services, editor_id, facade, service = _host_provisioning(tmp_path, "demoted-owner-editor")
    users = UserRepo(services.db)
    owner_id = users.create(username="demoted-config-owner", role="admin")
    initial = await service.save_config(
        PluginActor(owner_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-demoted-owner-old"),
        ),
    )
    old = await facade.get_record("provision_config", "primary")
    old_ref = old.payload["admin_credential_ref"]
    users.set_role(owner_id, "user")

    rotated = await service.save_config(
        PluginActor(editor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=initial.revision,
            expected_anchor_revision=initial.anchor_revision,
            name="Updated by admin",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-demoted-owner-next"),
        ),
    )

    current = await facade.get_record("provision_config", "primary")
    assert rotated.revision == initial.revision + 1
    assert current.owner_user_id == owner_id
    assert current.payload["admin_credential_ref"] != old_ref
    assert services.plugin_credential_repo.get("enterprise-knowledge", old_ref, owner_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_identity", ["actor_email", "owner_delete"])
async def test_final_identity_change_or_owner_deletion_cannot_rebind_staged_save(
    tmp_path: Path, changed_identity: str
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.repos.users import UserRepo

    _, services, editor_id, facade, service = _host_provisioning(
        tmp_path, f"identity-race-{changed_identity}"
    )
    users = UserRepo(services.db)
    owner_id = users.create(username=f"identity-owner-{changed_identity}", role="admin")
    initial = await service.save_config(
        PluginActor(owner_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-identity-old"),
        ),
    )
    initial_config = await facade.get_record("provision_config", "primary")
    old_ref = initial_config.payload["admin_credential_ref"]
    real_commit = facade.commit
    real_stage = facade.stage_credential
    changed = False
    staged_refs: list[str] = []

    async def track_stage(*args: Any, **kwargs: Any) -> str:
        planned_ref = await real_stage(*args, **kwargs)
        staged_refs.append(planned_ref)
        return planned_ref

    async def change_identity_before_final_commit(*args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        writes = args[0]
        if not changed and any(write.namespace == "provision_config" for write in writes):
            changed = True
            if changed_identity == "actor_email":
                users.set_email(editor_id, "changed-identity@example.invalid")
            else:
                users.delete(owner_id)
        return await real_commit(*args, **kwargs)

    facade.commit = change_identity_before_final_commit
    facade.stage_credential = track_stage
    with pytest.raises(PluginActionError):
        await service.save_config(
            PluginActor(editor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=initial.revision,
                expected_anchor_revision=initial.anchor_revision,
                name="Must not activate",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-identity-next"),
            ),
        )
    facade.commit = real_commit
    facade.stage_credential = real_stage

    current = await facade.get_record("provision_config", "primary")
    assert changed is True
    assert len(staged_refs) == 1
    staged_after = services.plugin_credential_repo.get(
        "enterprise-knowledge", staged_refs[0], owner_id
    )
    assert staged_after is None or staged_after.state != "active"
    if changed_identity == "actor_email":
        assert current.owner_user_id == owner_id
        assert current.payload["admin_credential_ref"] == old_ref
        old_credential = services.plugin_credential_repo.get(
            "enterprise-knowledge", old_ref, owner_id
        )
        assert old_credential is not None and old_credential.state == "active"
        preparations = await facade.list_records("provision_config_preparations")
        latest = max(preparations, key=lambda row: row.revision)
        assert latest.payload["state"] == "ABORTED"
        assert latest.payload["staged_cleanup"] == "RELEASED"
    else:
        assert current is None
        assert await facade.get_retained_record("provision_domains", "primary") is not None
        assert await facade.list_records("provision_config_preparations") == []


@pytest.mark.asyncio
async def test_unknown_retained_version_is_rejected_in_isolated_page(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningRetainedListInput

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RetainedRecordWrite,
    )

    _, _, actor_id, facade, service = _host_provisioning(tmp_path, "unknown-version-admin")
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                str(uuid4()),
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 99,
                    "domain_id": str(uuid4()),
                    "local_user_id": 77,
                    "resource_kind": "USER",
                    "phase": "UNKNOWN",
                    "receipt_id": None,
                },
                0,
            )
        ],
    )

    with pytest.raises(PluginActionError) as invalid:
        await service.list_retained(PluginActor(actor_id, True), ProvisioningRetainedListInput())
    assert invalid.value.code == "PROVISIONING_UNAVAILABLE"
    assert "77" not in str(invalid.value)


@pytest.mark.asyncio
async def test_verified_intent_with_missing_receipt_is_review_required(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningRetainedListInput

    from octop.infra.agents.plugins.host_contracts import PluginActor, RetainedRecordWrite

    _, _, actor_id, facade, service = _host_provisioning(tmp_path, "missing-receipt-admin")
    intent_id, receipt_id = str(uuid4()), str(uuid4())
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                intent_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": str(uuid4()),
                    "local_user_id": 88,
                    "resource_kind": "KB",
                    "phase": "VERIFIED",
                    "receipt_id": receipt_id,
                },
                0,
            )
        ],
    )

    output = await service.list_retained(
        PluginActor(actor_id, True), ProvisioningRetainedListInput()
    )
    assert len(output.items) == 1
    assert output.items[0].intent_id == intent_id
    assert output.items[0].has_receipt is False
    assert output.items[0].review_required is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["intent_id", "domain_id", "local_user_id", "resource_kind"])
async def test_retained_reader_rejects_each_receipt_binding_mismatch_in_isolation(
    tmp_path: Path, mismatch: str
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningRetainedListInput

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RetainedRecordWrite,
    )

    _, _, actor_id, facade, service = _host_provisioning(tmp_path, f"receipt-mismatch-{mismatch}")
    intent_id, receipt_id, domain_id = str(uuid4()), str(uuid4()), str(uuid4())
    receipt: dict[str, Any] = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "intent_id": intent_id,
        "domain_id": domain_id,
        "local_user_id": 89,
        "resource_kind": "USER",
        "proof_kind": "OWNER_MATCH",
    }
    receipt[mismatch] = {
        "intent_id": str(uuid4()),
        "domain_id": str(uuid4()),
        "local_user_id": 90,
        "resource_kind": "KB",
    }[mismatch]
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                intent_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": domain_id,
                    "local_user_id": 89,
                    "resource_kind": "USER",
                    "phase": "VERIFIED",
                    "receipt_id": receipt_id,
                },
                0,
            ),
            RetainedRecordWrite("provision_receipts", receipt_id, receipt, 0),
        ],
    )

    with pytest.raises(PluginActionError) as unavailable:
        await service.list_retained(PluginActor(actor_id, True), ProvisioningRetainedListInput())
    assert unavailable.value.code == "PROVISIONING_UNAVAILABLE"
    assert mismatch not in str(unavailable.value)


@pytest.mark.asyncio
async def test_retained_opaque_header_remains_readable_after_target_user_deletion(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningRetainedListInput

    from octop.infra.agents.plugins.host_contracts import PluginActor, RetainedRecordWrite
    from octop.infra.db.repos.users import UserRepo

    db, services, actor_id, facade, service = _host_provisioning(tmp_path, "retained-delete-admin")
    target_id = UserRepo(db).create(username="opaque-target", role="user")
    intent_id = str(uuid4())
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                intent_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": str(uuid4()),
                    "local_user_id": target_id,
                    "resource_kind": "USER",
                    "phase": "UNKNOWN",
                    "receipt_id": None,
                    "email": "synthetic-private@example.test",
                },
                0,
            )
        ],
    )
    UserRepo(services.db).delete(target_id)

    output = await service.list_retained(
        PluginActor(actor_id, True), ProvisioningRetainedListInput()
    )
    assert len(output.items) == 1 and output.items[0].local_user_id == target_id
    assert "synthetic-private" not in output.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["config.get", "retained.list"])
async def test_reader_rechecks_fresh_admin_after_persistent_read(
    tmp_path: Path, reader: str
) -> None:
    from enterprise_knowledge.provisioning_models import (
        ProvisioningConfigSaveInput,
        ProvisioningRetainedListInput,
    )
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.repos.users import UserRepo

    db, services, actor_id, facade, service = _host_provisioning(
        tmp_path, f"reader-revoke-{reader.replace('.', '-')}"
    )
    users = UserRepo(services.db)
    if reader == "config.get":
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-reader-revoke"),
            ),
        )
        real_resolve = facade.resolve_credential
        completed = False

        async def read_then_revoke(credential_ref: str, owner_user_id: int) -> Any:
            nonlocal completed
            payload = await real_resolve(credential_ref, owner_user_id)
            completed = True
            users.set_role(actor_id, "user")
            return payload

        facade.resolve_credential = read_then_revoke
        read = service.get_config(PluginActor(actor_id, True))
    else:
        from octop.infra.agents.plugins.host_contracts import RetainedRecordWrite

        await facade.commit(
            [],
            retained_writes=[
                RetainedRecordWrite(
                    "provision_intents",
                    str(uuid4()),
                    {
                        "schema_id": "octop.enterprise.provisioning.v1",
                        "version": 1,
                        "domain_id": str(uuid4()),
                        "local_user_id": 21,
                        "resource_kind": "USER",
                        "phase": "UNKNOWN",
                        "receipt_id": None,
                    },
                    0,
                )
            ],
        )
        real_page = facade.list_retained_records
        completed = False

        async def page_then_revoke(*args: Any, **kwargs: Any) -> Any:
            nonlocal completed
            page = await real_page(*args, **kwargs)
            completed = True
            users.set_role(actor_id, "user")
            return page

        facade.list_retained_records = page_then_revoke
        read = service.list_retained(PluginActor(actor_id, True), ProvisioningRetainedListInput())

    with pytest.raises(PluginActionError) as denied:
        await read
    assert completed is True
    assert denied.value.code == "ACCESS_DENIED"


@pytest.mark.asyncio
async def test_abort_cas_winner_prevents_final_config_activation(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning import PREPARATION_NAMESPACE
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RecordWrite,
    )

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "abort-final-race-admin")
    real_commit = facade.commit
    abort_won = False

    async def abort_before_final(*args: Any, **kwargs: Any) -> Any:
        nonlocal abort_won
        writes = args[0]
        if not abort_won and any(write.namespace == "provision_config" for write in writes):
            preparations = await facade.list_records(PREPARATION_NAMESPACE)
            assert len(preparations) == 1
            prep = preparations[0]
            payload = dict(prep.payload)
            payload["state"] = "ABORTED"
            payload["staged_cleanup"] = "UNRESOLVED"
            payload["updated_at"] = "2026-10-25T12:40:00Z"
            await real_commit(
                [
                    RecordWrite(
                        PREPARATION_NAMESPACE,
                        prep.logical_key,
                        prep.owner_user_id,
                        payload,
                        prep.revision,
                    )
                ]
            )
            abort_won = True
        return await real_commit(*args, **kwargs)

    facade.commit = abort_before_final
    with pytest.raises(PluginActionError) as conflict:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-abort-race"),
            ),
        )
    facade.commit = real_commit

    assert abort_won is True and conflict.value.code == "PROVISIONING_CONFLICT"
    assert await facade.get_record("provision_config", "primary") is None
    preparations = await facade.list_records(PREPARATION_NAMESPACE)
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "UNRESOLVED"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    staged = services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id)
    assert staged is not None and staged.state == "staged"


@pytest.mark.asyncio
async def test_release_false_never_marks_staged_cleanup_released(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning import PREPARATION_NAMESPACE
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

    _, _, actor_id, facade, service = _host_provisioning(tmp_path, "release-false-admin")
    real_stage = facade.stage_credential

    async def claim_but_do_not_store(*args: Any, **kwargs: Any) -> str:
        preparations = await facade.list_records(PREPARATION_NAMESPACE)
        assert len(preparations) == 1
        return preparations[0].payload["planned_credential_ref"]

    facade.stage_credential = claim_but_do_not_store
    with pytest.raises(PluginActionError) as conflict:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-release-false"),
            ),
        )
    facade.stage_credential = real_stage

    assert conflict.value.code == "PROVISIONING_CONFLICT"
    preparations = await facade.list_records(PREPARATION_NAMESPACE)
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "UNRESOLVED"
    assert await facade.get_record("provision_config", "primary") is None


@pytest.mark.asyncio
async def test_late_stage_after_abort_remains_staged_and_cannot_activate(tmp_path: Path) -> None:
    import asyncio
    import threading

    from enterprise_knowledge.provisioning import PREPARATION_NAMESPACE
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor, RecordWrite

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "late-stage-admin")
    real_stage = services.plugin_credential_repo.stage
    real_facade_stage = facade.stage_credential
    entered = threading.Event()
    release = threading.Event()
    held_prep: list[Any] = []

    def gate_stage(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        if not release.wait(60):
            raise TimeoutError("test did not release late stage operation")
        return real_stage(*args, **kwargs)

    async def abort_then_stage(*args: Any, **kwargs: Any) -> str:
        preparations = await facade.list_records(PREPARATION_NAMESPACE)
        assert len(preparations) == 1
        prep = preparations[0]
        held_prep.append(prep)
        aborted_payload = dict(prep.payload)
        aborted_payload["state"] = "ABORTED"
        aborted_payload["staged_cleanup"] = "UNRESOLVED"
        aborted_payload["updated_at"] = "2026-10-25T12:41:00Z"
        await facade.commit(
            [
                RecordWrite(
                    PREPARATION_NAMESPACE,
                    prep.logical_key,
                    actor_id,
                    aborted_payload,
                    prep.revision,
                )
            ]
        )
        return await real_facade_stage(*args, **kwargs)

    services.plugin_credential_repo.stage = gate_stage
    facade.stage_credential = abort_then_stage
    save = asyncio.create_task(
        service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-late-stage"),
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    prep = held_prep[0]
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    services.plugin_credential_repo.stage = real_stage
    facade.stage_credential = real_facade_stage

    final_prep = await facade.get_record(PREPARATION_NAMESPACE, prep.logical_key)
    assert final_prep.payload["state"] == "ABORTED"
    assert final_prep.payload["staged_cleanup"] == "UNRESOLVED"
    late_credential = services.plugin_credential_repo.get(
        "enterprise-knowledge", prep.payload["planned_credential_ref"], actor_id
    )
    assert late_credential is not None and late_credential.state == "staged"
    assert await facade.get_record("provision_config", "primary") is None


@pytest.mark.asyncio
async def test_first_save_atomically_creates_anchor_config_and_active_credential(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "first-save.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="first-save-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    actor = PluginActor(actor_id, True)
    request = ProvisioningConfigSaveInput(
        expected_revision=0,
        expected_anchor_revision=0,
        name="Shared KB",
        base_url="https://weknora.example/api/v1",
        enabled=False,
        system_admin_jwt=SecretStr("synthetic-admin-token"),
    )

    result = await ProvisioningService(facade).save_config(actor, request)
    projected = await ProvisioningService(facade).get_config(actor)

    config = await facade.get_record("provision_config", "primary")
    anchor = await facade.get_retained_record("provision_domains", "primary")
    preparations = await facade.list_records("provision_config_preparations")
    assert result.revision == config.revision == 1
    assert result.anchor_revision == anchor.revision == 1
    assert projected.credential_configured is True
    assert "admin_credential_ref" not in projected.model_dump()
    assert config.owner_user_id == actor_id
    assert config.payload["admin_credential_revision"] == 2
    assert preparations[0].payload["state"] == "COMMITTED"
    assert preparations[0].payload["staged_cleanup"] == "NOT_REQUIRED"
    assert (
        preparations[0].payload["planned_credential_ref"] == config.payload["admin_credential_ref"]
    )
    assert preparations[0].payload["domain_id"] == config.payload["domain_id"]
    credential = services.plugin_credential_repo.get(
        "enterprise-knowledge", config.payload["admin_credential_ref"], actor_id
    )
    assert credential is not None and credential.state == "active" and credential.revision == 2
    assert await facade.resolve_credential(credential.id, actor_id) == {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "system_admin_jwt": "synthetic-admin-token",
    }
    services.plugin_credential_repo.delete("enterprise-knowledge", credential.id, actor_id)
    assert (await ProvisioningService(facade).get_config(actor)).credential_configured is False
    with pytest.raises(PluginActionError) as missing_active:
        await ProvisioningService(facade).save_config(
            actor,
            ProvisioningConfigSaveInput(
                expected_revision=1,
                expected_anchor_revision=1,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=False,
                system_admin_jwt=SecretStr("synthetic-repair-token"),
            ),
        )
    assert missing_active.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_retained_list_projects_only_bound_header_fields_and_pages(tmp_path: Path) -> None:
    from uuid import uuid4

    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningRetainedListInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RetainedRecordWrite,
    )
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "retained-list.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="retained-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    intent_ids = sorted((str(uuid4()), str(uuid4())))
    domain_id = str(uuid4())
    receipt_id = str(uuid4())
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                intent_ids[0],
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": domain_id,
                    "local_user_id": 44,
                    "resource_kind": "KB",
                    "phase": "VERIFIED",
                    "receipt_id": receipt_id,
                    "email": "private@example.test",
                    "remote_id": "remote-secret-id",
                    "reason": "private reason",
                },
                0,
            ),
            RetainedRecordWrite(
                "provision_intents",
                intent_ids[1],
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": domain_id,
                    "local_user_id": 45,
                    "resource_kind": "USER",
                    "phase": "UNKNOWN",
                    "receipt_id": None,
                    "system_admin_jwt": "synthetic-secret",
                },
                0,
            ),
            RetainedRecordWrite(
                "provision_receipts",
                receipt_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "intent_id": intent_ids[0],
                    "domain_id": domain_id,
                    "local_user_id": 44,
                    "resource_kind": "KB",
                    "proof_kind": "CREATED_201",
                    "remote_id": "remote-secret-id",
                    "response": "private response",
                },
                0,
            ),
        ],
    )

    real_get_retained = facade.get_retained_record
    receipt_reads = 0

    async def count_receipt_reads(namespace: str, logical_key: str) -> Any:
        nonlocal receipt_reads
        if namespace == "provision_receipts":
            receipt_reads += 1
        return await real_get_retained(namespace, logical_key)

    facade.get_retained_record = count_receipt_reads
    first = await ProvisioningService(facade).list_retained(
        PluginActor(actor_id, True), ProvisioningRetainedListInput(limit=1)
    )
    assert receipt_reads <= len(first.items)
    assert [item.intent_id for item in first.items] == [intent_ids[0]]
    assert first.next_key == intent_ids[0]
    assert first.items[0].model_dump() == {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "intent_id": intent_ids[0],
        "local_user_id": 44,
        "resource_kind": "KB",
        "phase": "VERIFIED",
        "proof_kind": "CREATED_201",
        "has_receipt": True,
        "review_required": False,
        "revision": 1,
    }
    serialized = first.model_dump_json()
    for private_value in (
        "private@example.test",
        "remote-secret-id",
        "private reason",
        "synthetic-secret",
        "private response",
    ):
        assert private_value not in serialized

    second = await ProvisioningService(facade).list_retained(
        PluginActor(actor_id, True),
        ProvisioningRetainedListInput(after_key=first.next_key, limit=1),
    )
    assert [item.intent_id for item in second.items] == [intent_ids[1]]
    assert second.items[0].review_required is True
    assert second.items[0].has_receipt is False
    second_serialized = second.model_dump_json()
    for private_value in ("synthetic-secret", "private@example.test", "remote-secret-id"):
        assert private_value not in second_serialized
    facade.get_retained_record = real_get_retained

    mismatched_id = str(uuid4())
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                mismatched_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": domain_id,
                    "local_user_id": 44,
                    "resource_kind": "KB",
                    "phase": "VERIFIED",
                    "receipt_id": receipt_id,
                },
                0,
            )
        ],
    )
    with pytest.raises(PluginActionError) as mismatch:
        await ProvisioningService(facade).list_retained(
            PluginActor(actor_id, True), ProvisioningRetainedListInput()
        )
    assert mismatch.value.code == "PROVISIONING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_same_material_keeps_auth_block_and_rotation_preserves_owner(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import (
        PluginActor,
        RecordWrite,
        UserIdentityCheck,
    )
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "rotation.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    users = UserRepo(db)
    owner_id = users.create(username="config-owner", role="admin")
    editor_id = users.create(username="config-editor", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    service = ProvisioningService(facade)

    first = await service.save_config(
        PluginActor(owner_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-admin-token-A"),
        ),
    )
    before = await facade.get_record("provision_config", "primary")
    old_ref = before.payload["admin_credential_ref"]
    owner_identity = await facade.read_user_identity(owner_id)
    await facade.commit(
        [
            RecordWrite(
                "provision_auth_blocks",
                old_ref,
                owner_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": before.payload["domain_id"],
                    "credential_revision": 2,
                    "http_status": 403,
                    "rejected_at": "2026-10-25T12:34:56Z",
                },
                0,
            )
        ],
        identity_checks=[UserIdentityCheck(owner_identity)],
    )

    same = await service.save_config(
        PluginActor(editor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=first.revision,
            expected_anchor_revision=first.anchor_revision,
            name="Renamed KB",
            base_url="https://weknora.example:443/api/v1",
            enabled=False,
            system_admin_jwt=SecretStr("synthetic-admin-token-A"),
        ),
    )
    after_same = await facade.get_record("provision_config", "primary")
    block = await facade.get_record("provision_auth_blocks", old_ref)
    assert same.revision == 2
    assert after_same.owner_user_id == owner_id
    assert after_same.payload["admin_credential_ref"] == old_ref
    assert block is not None and block.payload["http_status"] == 403

    rotated = await service.save_config(
        PluginActor(editor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=same.revision,
            expected_anchor_revision=same.anchor_revision,
            name="Renamed KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-admin-token-B"),
        ),
    )
    after_rotation = await facade.get_record("provision_config", "primary")
    new_ref = after_rotation.payload["admin_credential_ref"]
    assert rotated.revision == 3
    assert after_rotation.owner_user_id == owner_id
    assert new_ref != old_ref
    assert services.plugin_credential_repo.get("enterprise-knowledge", old_ref, owner_id) is None
    new_credential = services.plugin_credential_repo.get("enterprise-knowledge", new_ref, owner_id)
    assert (
        new_credential is not None
        and new_credential.state == "active"
        and new_credential.revision == 2
    )
    assert (await facade.get_record("provision_auth_blocks", old_ref)).payload["http_status"] == 403
    assert await facade.get_record("provision_auth_blocks", new_ref) is None
    preps = await facade.list_records("provision_config_preparations")
    assert len(preps) == 2 and all(row.payload["state"] == "COMMITTED" for row in preps)
    assert (await facade.resolve_credential(new_ref, owner_id))[
        "system_admin_jwt"
    ] == "synthetic-admin-token-B"


@pytest.mark.asyncio
async def test_owner_deletion_keeps_anchor_and_new_admin_rebuilds_only_same_url(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "owner-deletion.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    users = UserRepo(db)
    owner_id = users.create(username="soon-deleted-owner", role="admin")
    new_admin_id = users.create(username="new-config-owner", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    service = ProvisioningService(facade)
    await service.save_config(
        PluginActor(owner_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Original name",
            base_url="https://weknora.example/api/v1",
            enabled=False,
            system_admin_jwt=SecretStr("synthetic-initial-token"),
        ),
    )
    original_anchor = await facade.get_retained_record("provision_domains", "primary")
    users.delete(owner_id)

    empty = await service.get_config(PluginActor(new_admin_id, True))
    assert empty.revision == 0 and empty.anchor_revision == 1
    assert empty.base_url == "https://weknora.example/api/v1"
    assert empty.credential_configured is False
    with pytest.raises(PluginActionError) as changed_domain:
        await service.save_config(
            PluginActor(new_admin_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=1,
                name="Wrong domain",
                base_url="https://other.example/api/v1",
                enabled=False,
                system_admin_jwt=SecretStr("synthetic-new-token"),
            ),
        )
    assert changed_domain.value.code == "PROVISIONING_INVALID"
    rebuilt = await service.save_config(
        PluginActor(new_admin_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=1,
            name="Rebuilt name",
            base_url="https://weknora.example:443/api/v1",
            enabled=False,
            system_admin_jwt=SecretStr("synthetic-new-token"),
        ),
    )
    rebuilt_config = await facade.get_record("provision_config", "primary")
    new_anchor = await facade.get_retained_record("provision_domains", "primary")
    assert rebuilt.revision == 1 and rebuilt.anchor_revision == 1
    assert rebuilt_config.owner_user_id == new_admin_id
    assert rebuilt_config.payload["domain_id"] == original_anchor.payload["domain_id"]
    assert new_anchor.revision == original_anchor.revision


@pytest.mark.asyncio
async def test_fresh_admin_reads_existing_receipt_after_config_owner_deletion(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning_models import (
        ProvisioningConfigSaveInput,
        ProvisioningRetainedListInput,
    )
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor, RetainedRecordWrite
    from octop.infra.db.repos.users import UserRepo

    db_path = tmp_path / "retained-config-owner-delete.db"
    db, services, host, facade, service = _host_provisioning_session(db_path, tmp_path)
    users = UserRepo(services.db)
    owner_id = users.create(username="retained-config-owner", role="admin")
    reader_id = users.create(username="retained-config-reader", role="admin")
    await service.save_config(
        PluginActor(owner_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-retained-owner"),
        ),
    )
    intent_id, receipt_id, domain_id = str(uuid4()), str(uuid4()), str(uuid4())
    await facade.commit(
        [],
        retained_writes=[
            RetainedRecordWrite(
                "provision_intents",
                intent_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "domain_id": domain_id,
                    "local_user_id": 91,
                    "resource_kind": "USER",
                    "phase": "VERIFIED",
                    "receipt_id": receipt_id,
                },
                0,
            ),
            RetainedRecordWrite(
                "provision_receipts",
                receipt_id,
                {
                    "schema_id": "octop.enterprise.provisioning.v1",
                    "version": 1,
                    "intent_id": intent_id,
                    "domain_id": domain_id,
                    "local_user_id": 91,
                    "resource_kind": "USER",
                    "proof_kind": "OWNER_MATCH",
                },
                0,
            ),
        ],
    )
    users.delete(owner_id)
    await host.close_and_wait()
    db.close()

    db2, services2, host2, facade2, service2 = _host_provisioning_session(db_path, tmp_path)
    output = await service2.list_retained(
        PluginActor(reader_id, True), ProvisioningRetainedListInput()
    )
    assert len(output.items) == 1
    summary = output.items[0]
    assert summary.intent_id == intent_id
    assert summary.local_user_id == 91
    assert summary.phase == "VERIFIED"
    assert summary.proof_kind == "OWNER_MATCH"
    assert summary.has_receipt is True and summary.review_required is False
    assert "retained-config-owner" not in repr(output)
    assert "synthetic-retained-owner" not in repr(output)
    await host2.close_and_wait()
    db2.close()


@pytest.mark.asyncio
async def test_explicit_stage_failure_leaves_preparation_unresolved_and_no_active_config(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, PluginHostError, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "stage-failure.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="stage-failure-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")

    async def fail_stage(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        raise PluginHostError("unavailable")

    facade.stage_credential = fail_stage
    with pytest.raises(PluginActionError) as exc:
        await ProvisioningService(facade).save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-admin-token"),
            ),
        )
    assert exc.value.code == "PROVISIONING_UNAVAILABLE"
    assert await facade.get_record("provision_config", "primary") is None
    assert await facade.get_retained_record("provision_domains", "primary") is None
    preparations = await facade.list_records("provision_config_preparations")
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "UNRESOLVED"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id) is None
    )


@pytest.mark.asyncio
async def test_cancelled_stage_waits_for_real_stage_then_cleans_exact_reference(
    tmp_path: Path,
) -> None:
    import asyncio

    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "stage-cancel.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="stage-cancel-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    real_stage = facade.stage_credential
    stage_finished = asyncio.Event()
    return_allowed = asyncio.Event()

    async def stage_then_hold(*args: Any, **kwargs: Any) -> str:
        credential_ref = await real_stage(*args, **kwargs)
        stage_finished.set()
        await return_allowed.wait()
        return credential_ref

    facade.stage_credential = stage_then_hold
    save = asyncio.create_task(
        ProvisioningService(facade).save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-cancel-token"),
            ),
        )
    )
    await stage_finished.wait()
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    return_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await save

    preparations = await facade.list_records("provision_config_preparations")
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "RELEASED"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id) is None
    )
    assert await facade.get_record("provision_config", "primary") is None
    assert await facade.get_retained_record("provision_domains", "primary") is None


@pytest.mark.asyncio
async def test_cancelled_during_real_prepare_commit_settles_and_keeps_unstaged_preparation(
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "prepare-cancel-admin")
    real_cas = services.plugin_record_repo.cas
    entered = threading.Event()
    release = threading.Event()
    stage_calls = 0

    def gate_prepare(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "provision_config_preparations" and kwargs["expected_revision"] == 0:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release real preparation CAS")
        return real_cas(*args, **kwargs)

    async def count_stage(*args: Any, **kwargs: Any) -> Any:
        nonlocal stage_calls
        stage_calls += 1
        return await real_stage(*args, **kwargs)

    real_stage = facade.stage_credential
    services.plugin_record_repo.cas = gate_prepare
    facade.stage_credential = count_stage
    save = asyncio.create_task(
        service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-prep-cancel"),
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    services.plugin_record_repo.cas = real_cas

    preparations = await facade.list_records("provision_config_preparations")
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "PREPARING"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    assert stage_calls == 0
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id) is None
    )


@pytest.mark.asyncio
async def test_cancelled_prepare_rollback_propagates_cancellation_without_staging(
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, services, actor_id, facade, service = _host_provisioning(
        tmp_path, "prepare-cancel-rollback-admin"
    )
    real_cas = services.plugin_record_repo.cas
    entered = threading.Event()
    release = threading.Event()
    stage_calls = 0

    def fail_prepare_after_cancel(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "provision_config_preparations" and kwargs["expected_revision"] == 0:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release preparation rollback")
            raise RuntimeError("synthetic preparation CAS rollback")
        return real_cas(*args, **kwargs)

    async def count_stage(*args: Any, **kwargs: Any) -> Any:
        nonlocal stage_calls
        stage_calls += 1
        return await real_stage(*args, **kwargs)

    real_stage = facade.stage_credential
    services.plugin_record_repo.cas = fail_prepare_after_cancel
    facade.stage_credential = count_stage
    save = asyncio.create_task(
        service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-prepare-rollback-cancel"),
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    services.plugin_record_repo.cas = real_cas
    facade.stage_credential = real_stage

    assert stage_calls == 0
    assert await facade.list_records("provision_config_preparations") == []
    assert await facade.get_record("provision_config", "primary") is None
    assert await facade.get_retained_record("provision_domains", "primary") is None


@pytest.mark.asyncio
async def test_cancelled_during_real_final_commit_settles_success_before_propagating(
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "final-cancel-admin")
    real_cas = services.plugin_record_repo.cas
    entered = threading.Event()
    release = threading.Event()

    def gate_final(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "provision_config" and args[2] == "primary":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release real final config CAS")
        return real_cas(*args, **kwargs)

    services.plugin_record_repo.cas = gate_final
    save = asyncio.create_task(
        service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-final-cas-cancel"),
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    services.plugin_record_repo.cas = real_cas

    config = await facade.get_record("provision_config", "primary")
    preparations = await facade.list_records("provision_config_preparations")
    assert config is not None and config.revision == 1
    assert preparations[0].payload["state"] == "COMMITTED"
    active = services.plugin_credential_repo.get(
        "enterprise-knowledge", config.payload["admin_credential_ref"], actor_id
    )
    assert active is not None and active.state == "active" and active.revision == 2


@pytest.mark.asyncio
async def test_cancelled_during_real_final_rollback_settles_then_releases_staged_ref(
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "rollback-cancel-admin")
    real_cas = services.plugin_record_repo.cas
    entered = threading.Event()
    release = threading.Event()

    def fail_after_config_cas(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "provision_config" and args[2] == "primary":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release real rollback path")
            real_cas(*args, **kwargs)
            raise RuntimeError("synthetic transaction rollback")
        return real_cas(*args, **kwargs)

    services.plugin_record_repo.cas = fail_after_config_cas
    save = asyncio.create_task(
        service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-rollback-cancel"),
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    services.plugin_record_repo.cas = real_cas

    assert await facade.get_record("provision_config", "primary") is None
    assert await facade.get_retained_record("provision_domains", "primary") is None
    preparations = await facade.list_records("provision_config_preparations")
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "RELEASED"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id) is None
    )


@pytest.mark.asyncio
async def test_cancelled_during_real_stage_operation_waits_then_releases_exact_ref(
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "stage-db-cancel-admin")
    real_stage = services.plugin_credential_repo.stage
    entered = threading.Event()
    release = threading.Event()

    def gate_stage(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release real credential stage")
        return real_stage(*args, **kwargs)

    services.plugin_credential_repo.stage = gate_stage
    save = asyncio.create_task(
        service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-stage-db-cancel"),
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    services.plugin_credential_repo.stage = real_stage

    preparations = await facade.list_records("provision_config_preparations")
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "RELEASED"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id) is None
    )


@pytest.mark.asyncio
async def test_final_commit_requires_original_held_preparation_revision(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning import PREPARATION_NAMESPACE
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RecordWrite,
    )

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "held-preparation-admin")
    real_get = facade.get_record
    real_commit = facade.commit
    changed = False

    async def change_held_preparation(namespace: str, logical_key: str) -> Any:
        nonlocal changed
        row = await real_get(namespace, logical_key)
        if namespace == PREPARATION_NAMESPACE and not changed:
            assert row is not None
            changed = True
            payload = dict(row.payload)
            payload["updated_at"] = "2026-10-25T12:35:00Z"
            await real_commit(
                [
                    RecordWrite(
                        PREPARATION_NAMESPACE,
                        logical_key,
                        row.owner_user_id,
                        payload,
                        row.revision,
                    )
                ]
            )
            return await real_get(namespace, logical_key)
        return row

    facade.get_record = change_held_preparation
    with pytest.raises(PluginActionError) as exc:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-held-preparation"),
            ),
        )
    facade.get_record = real_get

    assert changed is True and exc.value.code == "PROVISIONING_UNAVAILABLE"
    assert await facade.get_record("provision_config", "primary") is None
    preparations = await facade.list_records(PREPARATION_NAMESPACE)
    assert len(preparations) == 1
    held = preparations[0]
    assert held.revision == 2 and held.owner_user_id == actor_id
    assert held.payload["state"] == "PREPARING"
    planned_ref = held.payload["planned_credential_ref"]
    staged = services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id)
    assert staged is not None and staged.state == "staged"
    independent = await service.save_config(
        PluginActor(actor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Independent save",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-independent-save"),
        ),
    )
    assert independent.revision == 1
    still_held = await facade.get_record(PREPARATION_NAMESPACE, held.logical_key)
    assert still_held.revision == held.revision and still_held.payload["state"] == "PREPARING"
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id).state
        == "staged"
    )


@pytest.mark.asyncio
async def test_fresh_host_rebind_preserves_staged_preparation_and_committed_config(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning import PREPARATION_NAMESPACE
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        PluginHostError,
        RecordWrite,
    )
    from octop.infra.db.repos.users import UserRepo

    db_path = tmp_path / "fresh-rebind.db"
    db, services, host, facade, service = _host_provisioning_session(db_path, tmp_path)
    actor_id = UserRepo(services.db).create(username="fresh-rebind-admin", role="admin")
    real_get = facade.get_record
    real_commit = facade.commit
    changed = False

    async def bump_persisted_prepare(namespace: str, logical_key: str) -> Any:
        nonlocal changed
        row = await real_get(namespace, logical_key)
        if namespace == PREPARATION_NAMESPACE and not changed:
            assert row is not None
            changed = True
            payload = dict(row.payload)
            payload["updated_at"] = "2026-10-25T12:35:00Z"
            await real_commit(
                [
                    RecordWrite(
                        PREPARATION_NAMESPACE, logical_key, row.owner_user_id, payload, row.revision
                    )
                ]
            )
            return await real_get(namespace, logical_key)
        return row

    facade.get_record = bump_persisted_prepare
    with pytest.raises(PluginActionError):
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Held preparation",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-held-for-rebind"),
            ),
        )
    facade.get_record = real_get
    preparations = await facade.list_records(PREPARATION_NAMESPACE)
    assert changed and len(preparations) == 1
    held = preparations[0]
    assert held.payload["state"] == "PREPARING"
    planned_ref = held.payload["planned_credential_ref"]
    staged = services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id)
    assert staged is not None and staged.state == "staged"
    await host.close_and_wait()
    db.close()

    db2, services2, host2, facade2, service2 = _host_provisioning_session(db_path, tmp_path)
    held_after_rebind = await facade2.get_record(PREPARATION_NAMESPACE, held.logical_key)
    assert held_after_rebind.revision == held.revision
    assert held_after_rebind.payload == held.payload
    assert (
        services2.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id).state
        == "staged"
    )
    with pytest.raises(PluginHostError) as staged_resolve:
        await facade2.resolve_credential(planned_ref, actor_id)
    assert staged_resolve.value.code == "not_found"
    saved = await service2.save_config(
        PluginActor(actor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Fresh save",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-fresh-save"),
        ),
    )
    assert saved.revision == 1
    config2 = await facade2.get_record("provision_config", "primary")
    active_ref = config2.payload["admin_credential_ref"]
    assert active_ref != planned_ref
    assert (await service2.get_config(PluginActor(actor_id, True))).credential_configured
    await host2.close_and_wait()
    db2.close()

    db3, services3, host3, facade3, service3 = _host_provisioning_session(db_path, tmp_path)
    config3 = await facade3.get_record("provision_config", "primary")
    prep3 = await facade3.get_record(PREPARATION_NAMESPACE, held.logical_key)
    active3 = services3.plugin_credential_repo.get("enterprise-knowledge", active_ref, actor_id)
    assert config3.owner_user_id == actor_id and config3.payload == config2.payload
    assert prep3.payload == held.payload
    assert active3 is not None and active3.state == "active"
    assert await facade3.resolve_credential(active_ref, actor_id) == {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "system_admin_jwt": "synthetic-fresh-save",
    }
    assert await service3.get_config(PluginActor(actor_id, True)) == saved
    await host3.close_and_wait()
    db3.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_persists", [False, True])
async def test_prepare_failure_or_lost_result_never_starts_credential_stage(
    tmp_path: Path, commit_persists: bool
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        PluginHostError,
    )

    _, services, actor_id, facade, service = _host_provisioning(
        tmp_path, f"prepare-outcome-{commit_persists}"
    )
    real_commit = facade.commit
    real_cas = services.plugin_record_repo.cas
    stage_calls = 0

    async def fail_or_lose_prepare(*args: Any, **kwargs: Any) -> Any:
        writes = args[0]
        if writes and writes[0].namespace == "provision_config_preparations":
            result = await real_commit(*args, **kwargs)
            if commit_persists:
                raise PluginHostError("unavailable")
            return result
        return await real_commit(*args, **kwargs)

    def fail_inside_prepare(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "provision_config_preparations" and kwargs["expected_revision"] == 0:
            real_cas(*args, **kwargs)
            raise RuntimeError("synthetic preparation rollback")
        return real_cas(*args, **kwargs)

    async def count_stage(*args: Any, **kwargs: Any) -> Any:
        nonlocal stage_calls
        stage_calls += 1
        raise AssertionError("stage must not run after prepare failure")

    facade.commit = fail_or_lose_prepare
    facade.stage_credential = count_stage
    if not commit_persists:
        services.plugin_record_repo.cas = fail_inside_prepare
    with pytest.raises(PluginActionError) as exc:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-prepare-outcome"),
            ),
        )
    facade.commit = real_commit
    services.plugin_record_repo.cas = real_cas
    assert exc.value.code == "PROVISIONING_UNAVAILABLE"
    assert stage_calls == 0
    preparations = await facade.list_records("provision_config_preparations")
    if commit_persists:
        assert len(preparations) == 1
        prep = preparations[0]
        assert prep.payload["state"] == "PREPARING"
        assert prep.payload["staged_cleanup"] == "PENDING"
        assert (
            services.plugin_credential_repo.get(
                "enterprise-knowledge", prep.payload["planned_credential_ref"], actor_id
            )
            is None
        )
    else:
        assert preparations == []


@pytest.mark.asyncio
async def test_failed_rotation_rolls_back_and_keeps_previous_active_credential(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "failed-rotation-admin")
    first = await service.save_config(
        PluginActor(actor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-rotation-old"),
        ),
    )
    original = await facade.get_record("provision_config", "primary")
    old_ref = original.payload["admin_credential_ref"]
    real_cas = services.plugin_record_repo.cas

    def rollback_after_config_cas(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "provision_config" and args[2] == "primary":
            real_cas(*args, **kwargs)
            raise RuntimeError("synthetic failed rotation transaction")
        return real_cas(*args, **kwargs)

    services.plugin_record_repo.cas = rollback_after_config_cas
    with pytest.raises(PluginActionError) as exc:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=first.revision,
                expected_anchor_revision=first.anchor_revision,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-rotation-new"),
            ),
        )
    services.plugin_record_repo.cas = real_cas

    assert exc.value.code == "PROVISIONING_UNAVAILABLE"
    unchanged = await facade.get_record("provision_config", "primary")
    assert unchanged.revision == first.revision
    assert unchanged.payload["admin_credential_ref"] == old_ref
    old_credential = services.plugin_credential_repo.get("enterprise-knowledge", old_ref, actor_id)
    assert old_credential is not None and old_credential.state == "active"
    preps = await facade.list_records("provision_config_preparations")
    assert len(preps) == 2
    failed = next(row for row in preps if row.payload["state"] == "ABORTED")
    assert failed.payload["staged_cleanup"] == "RELEASED"
    assert (
        services.plugin_credential_repo.get(
            "enterprise-knowledge", failed.payload["planned_credential_ref"], actor_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_successful_final_commit_with_lost_response_stays_committed(tmp_path: Path) -> None:
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        PluginHostError,
    )

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "lost-final-result-admin")
    first = await service.save_config(
        PluginActor(actor_id, True),
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="Shared KB",
            base_url="https://weknora.example/api/v1",
            enabled=True,
            system_admin_jwt=SecretStr("synthetic-lost-old"),
        ),
    )
    before = await facade.get_record("provision_config", "primary")
    old_ref = before.payload["admin_credential_ref"]
    real_commit = facade.commit

    async def commit_then_lose_result(*args: Any, **kwargs: Any) -> Any:
        result = await real_commit(*args, **kwargs)
        writes = args[0]
        if any(write.namespace == "provision_config" for write in writes):
            raise PluginHostError("unavailable")
        return result

    facade.commit = commit_then_lose_result
    with pytest.raises(PluginActionError) as exc:
        await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=first.revision,
                expected_anchor_revision=first.anchor_revision,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-lost-new"),
            ),
        )
    facade.commit = real_commit

    assert exc.value.code == "PROVISIONING_UNAVAILABLE"
    config = await facade.get_record("provision_config", "primary")
    assert config.revision == first.revision + 1
    new_ref = config.payload["admin_credential_ref"]
    assert new_ref != old_ref
    assert services.plugin_credential_repo.get("enterprise-knowledge", old_ref, actor_id) is None
    new_credential = services.plugin_credential_repo.get("enterprise-knowledge", new_ref, actor_id)
    assert new_credential is not None and new_credential.state == "active"
    preparations = await facade.list_records("provision_config_preparations")
    assert sum(row.payload["state"] == "COMMITTED" for row in preparations) == 2


@pytest.mark.asyncio
async def test_cancel_after_actual_final_commit_preserves_committed_config_and_key(
    tmp_path: Path,
) -> None:
    import asyncio

    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "final-commit-cancel.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="final-cancel-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    real_commit = facade.commit
    final_commit_finished = asyncio.Event()
    return_allowed = asyncio.Event()

    async def commit_then_hold(*args: Any, **kwargs: Any) -> Any:
        result = await real_commit(*args, **kwargs)
        writes = args[0]
        if any(write.namespace == "provision_config" for write in writes):
            final_commit_finished.set()
            await return_allowed.wait()
        return result

    facade.commit = commit_then_hold
    save = asyncio.create_task(
        ProvisioningService(facade).save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-final-token"),
            ),
        )
    )
    await final_commit_finished.wait()
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    return_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await save

    config = await facade.get_record("provision_config", "primary")
    preparations = await facade.list_records("provision_config_preparations")
    credential_ref = config.payload["admin_credential_ref"]
    credential = services.plugin_credential_repo.get(
        "enterprise-knowledge", credential_ref, actor_id
    )
    assert credential is not None and credential.state == "active" and credential.revision == 2
    assert preparations[0].payload["state"] == "COMMITTED"
    assert preparations[0].payload["staged_cleanup"] == "NOT_REQUIRED"
    assert await facade.resolve_credential(credential_ref, actor_id)


@pytest.mark.asyncio
async def test_concurrent_initial_saves_have_one_config_and_active_key_winner(
    tmp_path: Path,
) -> None:
    import asyncio

    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "concurrent-save.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username="concurrent-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    service = ProvisioningService(facade)

    async def save(token: str):
        return await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr(token),
            ),
        )

    results = await asyncio.gather(
        save("synthetic-concurrent-A"), save("synthetic-concurrent-B"), return_exceptions=True
    )
    winners = [result for result in results if not isinstance(result, BaseException)]
    losers = [result for result in results if isinstance(result, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1 and isinstance(losers[0], PluginActionError)
    config = await facade.get_record("provision_config", "primary")
    anchor = await facade.get_retained_record("provision_domains", "primary")
    preparations = await facade.list_records("provision_config_preparations")
    assert config.revision == 1 and anchor.revision == 1
    active = [
        services.plugin_credential_repo.get(
            "enterprise-knowledge", row.payload["planned_credential_ref"], actor_id
        )
        for row in preparations
    ]
    assert (
        sum(credential is not None and credential.state == "active" for credential in active) == 1
    )
    assert sum(row.payload["state"] == "COMMITTED" for row in preparations) == 1
    assert all(
        row.payload["state"] == "COMMITTED"
        or (
            row.payload["state"] == "ABORTED"
            and row.payload["staged_cleanup"] in {"RELEASED", "UNRESOLVED"}
        )
        for row in preparations
    )


@pytest.mark.asyncio
async def test_two_staged_preparations_compete_only_at_final_config_cas(tmp_path: Path) -> None:
    import asyncio

    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from pydantic import SecretStr

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

    _, services, actor_id, facade, service = _host_provisioning(tmp_path, "staged-race-admin")
    real_stage = facade.stage_credential
    both_staged = asyncio.Barrier(2)

    async def stage_then_race(*args: Any, **kwargs: Any) -> Any:
        row = await real_stage(*args, **kwargs)
        await both_staged.wait()
        return row

    facade.stage_credential = stage_then_race

    async def save(token: str) -> Any:
        return await service.save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr(token),
            ),
        )

    results = await asyncio.gather(
        save("synthetic-staged-winner-A"), save("synthetic-staged-winner-B"), return_exceptions=True
    )
    facade.stage_credential = real_stage
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1, [
        (type(item).__name__, getattr(item, "code", None)) for item in results
    ]
    assert len(losers) == 1 and isinstance(losers[0], PluginActionError)
    config = await facade.get_record("provision_config", "primary")
    preparations = await facade.list_records("provision_config_preparations")
    assert config is not None and config.revision == 1
    assert len(preparations) == 2
    states = {row.payload["state"] for row in preparations}
    assert states == {"COMMITTED", "ABORTED"}
    active = [
        services.plugin_credential_repo.get(
            "enterprise-knowledge", row.payload["planned_credential_ref"], actor_id
        )
        for row in preparations
    ]
    assert sum(row is not None and row.state == "active" for row in active) == 1
    losing = next(row for row in preparations if row.payload["state"] == "ABORTED")
    assert losing.payload["staged_cleanup"] == "RELEASED"
    assert losing.payload["planned_credential_ref"] != config.payload["admin_credential_ref"]
    assert (
        services.plugin_credential_repo.get(
            "enterprise-knowledge", losing.payload["planned_credential_ref"], actor_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_admin_revoked_after_stage_is_denied_and_staged_key_is_settled(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import ProvisioningConfigSaveInput
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from pydantic import SecretStr

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "revoked-stage.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    users = UserRepo(db)
    actor_id = users.create(username="revoked-admin", role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    real_stage = facade.stage_credential

    async def stage_then_revoke(*args: Any, **kwargs: Any) -> str:
        ref = await real_stage(*args, **kwargs)
        users.set_role(actor_id, "user")
        return ref

    facade.stage_credential = stage_then_revoke
    with pytest.raises(PluginActionError) as exc:
        await ProvisioningService(facade).save_config(
            PluginActor(actor_id, True),
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="Shared KB",
                base_url="https://weknora.example/api/v1",
                enabled=True,
                system_admin_jwt=SecretStr("synthetic-revoked-token"),
            ),
        )
    assert exc.value.code == "ACCESS_DENIED"
    preparations = await facade.list_records("provision_config_preparations")
    assert len(preparations) == 1
    assert preparations[0].payload["state"] == "ABORTED"
    assert preparations[0].payload["staged_cleanup"] == "RELEASED"
    planned_ref = preparations[0].payload["planned_credential_ref"]
    assert (
        services.plugin_credential_repo.get("enterprise-knowledge", planned_ref, actor_id) is None
    )
    assert await facade.get_record("provision_config", "primary") is None
    assert await facade.get_retained_record("provision_domains", "primary") is None
