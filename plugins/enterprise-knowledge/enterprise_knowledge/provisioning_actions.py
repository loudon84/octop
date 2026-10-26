"""Typed admin actions for local provisioning configuration and retained review."""

from __future__ import annotations

from typing import Any

from octop.infra.agents.plugins.host_contracts import PluginActor

from .actions import ActionSpec
from .i18n import tr
from .models import EmptyInput
from .provisioning import ProvisioningService
from .provisioning_models import (
    ProvisioningConfig,
    ProvisioningConfigSaveInput,
    ProvisioningRetainedListInput,
    ProvisioningRetainedListOutput,
    ProvisioningRetryInput,
    ProvisioningStatus,
    ProvisioningTaskRetryInput,
    ProvisioningTasksInput,
    ProvisioningTasksOutput,
)
from .provisioning_state import ProvisioningStateService


def register_provisioning_actions(facade: Any) -> tuple[ActionSpec, ...]:
    async def config_get(actor: PluginActor, _payload: EmptyInput) -> ProvisioningConfig:
        return await ProvisioningService(facade).get_config(actor)

    async def config_save(
        actor: PluginActor, payload: ProvisioningConfigSaveInput
    ) -> ProvisioningConfig:
        return await ProvisioningService(facade).save_config(actor, payload)

    async def retained_list(
        actor: PluginActor, payload: ProvisioningRetainedListInput
    ) -> ProvisioningRetainedListOutput:
        return await ProvisioningService(facade).list_retained(actor, payload)

    async def status(actor: PluginActor, _payload: EmptyInput) -> ProvisioningStatus:
        return await ProvisioningStateService(facade).get_status(actor)

    async def retry(actor: PluginActor, payload: ProvisioningRetryInput) -> ProvisioningStatus:
        return await ProvisioningStateService(facade).retry_self(actor, payload)

    async def tasks_list(
        actor: PluginActor, payload: ProvisioningTasksInput
    ) -> ProvisioningTasksOutput:
        return await ProvisioningStateService(facade).list_tasks(actor, payload)

    async def task_retry(
        actor: PluginActor, payload: ProvisioningTaskRetryInput
    ) -> ProvisioningStatus:
        return await ProvisioningStateService(facade).retry_task(actor, payload)

    return (
        ActionSpec(
            "provisioning.config.get",
            EmptyInput,
            ProvisioningConfig,
            config_get,
            tr("actions.provisioning.config.get"),
            "admin",
        ),
        ActionSpec(
            "provisioning.config.save",
            ProvisioningConfigSaveInput,
            ProvisioningConfig,
            config_save,
            tr("actions.provisioning.config.save"),
            "admin",
        ),
        ActionSpec(
            "provisioning.retained.list",
            ProvisioningRetainedListInput,
            ProvisioningRetainedListOutput,
            retained_list,
            tr("actions.provisioning.retained.list"),
            "admin",
        ),
        ActionSpec(
            "provisioning.status",
            EmptyInput,
            ProvisioningStatus,
            status,
            tr("actions.provisioning.status"),
            "authenticated",
        ),
        ActionSpec(
            "provisioning.retry",
            ProvisioningRetryInput,
            ProvisioningStatus,
            retry,
            tr("actions.provisioning.retry"),
            "authenticated",
        ),
        ActionSpec(
            "provisioning.tasks.list",
            ProvisioningTasksInput,
            ProvisioningTasksOutput,
            tasks_list,
            tr("actions.provisioning.tasks.list"),
            "admin",
        ),
        ActionSpec(
            "provisioning.task.retry",
            ProvisioningTaskRetryInput,
            ProvisioningStatus,
            task_retry,
            tr("actions.provisioning.task.retry"),
            "admin",
        ),
    )
