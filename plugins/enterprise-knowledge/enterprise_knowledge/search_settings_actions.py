"""Typed actions for enterprise search source settings and diagnostics."""

from __future__ import annotations

from typing import Any

from octop.infra.agents.plugins.host_contracts import PluginActor

from .actions import ActionSpec
from .i18n import tr
from .models import EmptyInput
from .search_settings import (
    SearchRecoverInput,
    SearchSaveInput,
    SearchSettingsGetOutput,
    SearchSettingsOutput,
    SearchSettingsService,
    SearchTestInput,
    SearchTestOutput,
)


def register_search_actions(facade: Any) -> tuple[ActionSpec, ...]:
    service = SearchSettingsService(facade)

    async def search_get(actor: PluginActor, _payload: EmptyInput) -> SearchSettingsGetOutput:
        return SearchSettingsGetOutput(await service.get_settings(actor))

    async def search_save(actor: PluginActor, payload: SearchSaveInput) -> SearchSettingsOutput:
        return await service.save(actor, payload)

    async def search_test(actor: PluginActor, payload: SearchTestInput) -> SearchTestOutput:
        return await service.test(actor, payload)

    async def search_recover(
        actor: PluginActor, payload: SearchRecoverInput
    ) -> SearchSettingsOutput:
        return await service.recover(actor, payload)

    return (
        ActionSpec(
            "search.get",
            EmptyInput,
            SearchSettingsGetOutput,
            search_get,
            tr("actions.search.get"),
            "authenticated",
        ),
        ActionSpec(
            "search.save",
            SearchSaveInput,
            SearchSettingsOutput,
            search_save,
            tr("actions.search.save"),
            "admin",
        ),
        ActionSpec(
            "search.test",
            SearchTestInput,
            SearchTestOutput,
            search_test,
            tr("actions.search.test"),
            "admin",
        ),
        ActionSpec(
            "search.recover",
            SearchRecoverInput,
            SearchSettingsOutput,
            search_recover,
            tr("actions.search.recover"),
            "admin",
        ),
    )
