"""Harness SDK entry point for the enterprise knowledge plugin."""

from __future__ import annotations

from typing import Any

from octop_harness.plugins import PluginContext

from octop.infra.agents.plugins.host import register_host_setup

from .enterprise_knowledge.actions import register_actions
from .enterprise_knowledge.provisioning_actions import register_provisioning_actions
from .enterprise_knowledge.service import KnowledgeService
from .enterprise_knowledge.tools import register_tools


def setup(ctx: PluginContext) -> None:
    tool_functions, bind_tool_facade = register_tools(ctx)

    def register(facade: Any) -> None:
        bind_tool_facade(facade)
        facade.register_user_identity_consumer()
        facade.register_service(
            api_version=1,
            factory=lambda: KnowledgeService(facade, tool_functions=tool_functions),
            required_plugins={
                "enterprise-knowledge": "0.1.0",
                "enterprise-policy": "0.1.0",
            },
        )
        facade.register_read_only_tools([function.__name__ for function in tool_functions.values()])
        for action in (*register_actions(facade), *register_provisioning_actions(facade)):
            facade.register_action(
                action.name,
                action.input_model,
                action.output_model,
                action.handler,
                action.summary,
                action.permission,
            )

    register_host_setup(ctx, register)
