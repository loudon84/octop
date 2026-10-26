from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError
from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))


@pytest.fixture
def services(tmp_path: Path) -> Any:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


def _context() -> PluginContext:
    return PluginContext(
        manifest=PluginManifest(
            id="enterprise-knowledge",
            version="0.1.0",
            name="Enterprise Knowledge",
            kind="tool",
            entry="main.py",
        ),
        source_path=_PLUGIN_ROOT,
    )


@pytest.mark.asyncio
async def test_plugin_setup_registers_seven_typed_provisioning_actions_and_identity_consumer(
    services: Any,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "enterprise_knowledge_plugin",
        _PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    setup = module.setup

    actor_id = UserRepo(services.db).create(username="provision-admin", role="admin")
    host = PluginHost(lambda: services)
    context = _context()
    setup(context)
    facade = host.bind_context(context, "0.1.0")

    actions = facade.descriptors()["actions"]
    provisioning = [row for row in actions if row["name"].startswith("provisioning.")]
    assert [(row["name"], row["permission"]) for row in provisioning] == [
        ("provisioning.config.get", "admin"),
        ("provisioning.config.save", "admin"),
        ("provisioning.retained.list", "admin"),
        ("provisioning.retry", "authenticated"),
        ("provisioning.status", "authenticated"),
        ("provisioning.task.retry", "admin"),
        ("provisioning.tasks.list", "admin"),
    ]
    assert await facade.read_user_identity(actor_id)
    output = await facade.invoke_action("provisioning.config.get", PluginActor(actor_id, True), {})
    assert output["revision"] == 0
    assert output["credential_configured"] is False
    with pytest.raises(PluginHostError) as invalid:
        await facade.invoke_action(
            "provisioning.config.save",
            PluginActor(actor_id, True),
            {
                "expected_revision": 0,
                "expected_anchor_revision": 0,
                "name": "Shared KB",
                "base_url": "https://weknora.example/api/v1",
                "enabled": True,
                "system_admin_jwt": "synthetic-invalid-secret",
                "unexpected": "synthetic-invalid-detail",
            },
        )
    assert invalid.value.code == "invalid"
    assert "synthetic-invalid" not in str(invalid.value)

    saved = await facade.invoke_action(
        "provisioning.config.save",
        PluginActor(actor_id, True),
        {
            "expected_revision": 0,
            "expected_anchor_revision": 0,
            "name": "Shared KB",
            "base_url": "https://weknora.example/api/v1",
            "enabled": True,
            "system_admin_jwt": "synthetic-action-token",
        },
    )
    assert saved["revision"] == 1 and saved["credential_configured"] is True
    retained = await facade.invoke_action(
        "provisioning.retained.list", PluginActor(actor_id, True), {}
    )
    assert retained["items"] == [] and retained["next_key"] is None

    status = await facade.invoke_action("provisioning.status", PluginActor(actor_id, True), {})
    assert status["revision"] == 0 and status["state"] == "EMAIL_REQUIRED"
    retried = await facade.invoke_action(
        "provisioning.retry", PluginActor(actor_id, True), {"expected_revision": 0}
    )
    assert retried["revision"] == 1 and retried["state"] == "EMAIL_REQUIRED"
    listed = await facade.invoke_action(
        "provisioning.tasks.list", PluginActor(actor_id, True), {"after_user_id": 0, "limit": 10}
    )
    assert [item["user_id"] for item in listed["items"]] == [actor_id]
    task_id = listed["items"][0]["task_id"]
    admin_retry = await facade.invoke_action(
        "provisioning.task.retry",
        PluginActor(actor_id, True),
        {"task_id": task_id, "expected_revision": 1, "reason": "review retry"},
    )
    assert admin_retry["revision"] == 1

    from enterprise_knowledge.provisioning import ProvisioningService
    from enterprise_knowledge.provisioning_models import (
        ProvisioningConfigSaveInput,
        ProvisioningRetainedListInput,
    )

    ordinary_user = UserRepo(services.db).create(username="provision-user", role="user")
    service = ProvisioningService(facade)
    for call in (
        lambda: service.get_config(PluginActor(ordinary_user, False)),
        lambda: service.save_config(
            PluginActor(ordinary_user, False),
            ProvisioningConfigSaveInput(
                expected_revision=1,
                expected_anchor_revision=1,
                name="Denied",
                base_url="https://weknora.example/api/v1",
                enabled=True,
            ),
        ),
        lambda: service.list_retained(
            PluginActor(ordinary_user, False), ProvisioningRetainedListInput()
        ),
    ):
        with pytest.raises(PluginActionError) as denied:
            await call()
        assert denied.value.code == "ACCESS_DENIED"

    self_status = await facade.invoke_action(
        "provisioning.status", PluginActor(ordinary_user, False), {}
    )
    assert self_status["revision"] == 0

    with pytest.raises(PluginHostError) as bad_cursor:
        await facade.invoke_action(
            "provisioning.retained.list",
            PluginActor(actor_id, True),
            {"after_key": "not-a-canonical-cursor"},
        )
    assert bad_cursor.value.code == "invalid"

    UserRepo(services.db).set_role(actor_id, "user")
    with pytest.raises(PluginActionError) as exc:
        await facade.invoke_action("provisioning.config.get", PluginActor(actor_id, True), {})
    assert exc.value.code == "ACCESS_DENIED"
    assert "password" not in str(exc.value).lower()


@pytest.mark.asyncio
async def test_cached_admin_disabled_is_rejected_by_real_action_ingress(services: Any) -> None:
    spec = importlib.util.spec_from_file_location(
        "enterprise_knowledge_disabled_admin_plugin",
        _PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    actor_id = UserRepo(services.db).create(username="disabled-provision-admin", role="admin")
    host = PluginHost(lambda: services)
    context = _context()
    module.setup(context)
    facade = host.bind_context(context, "0.1.0")
    UserRepo(services.db).set_disabled(actor_id, True)

    with pytest.raises(PluginActionError) as denied:
        await facade.invoke_action("provisioning.config.get", PluginActor(actor_id, True), {})
    assert denied.value.code == "ACCESS_DENIED"
