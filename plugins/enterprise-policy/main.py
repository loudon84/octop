"""Harness SDK entry point for the enterprise policy hook."""

from __future__ import annotations

from octop_harness.plugins import PluginContext

from octop.infra.agents.plugins.host import register_host_setup

from .enterprise_policy.policy import EnterprisePolicyMiddleware


def setup(ctx: PluginContext) -> None:
    middleware = EnterprisePolicyMiddleware()
    ctx.middleware(middleware, priority=100)
    register_host_setup(ctx, middleware.bind_facade)
