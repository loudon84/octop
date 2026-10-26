from __future__ import annotations

from pathlib import Path

import pytest
from octop_harness.plugins import PluginManifest
from octop_harness.plugins.context import PluginContext

from octop.infra.agents.plugins.host import PluginHost, register_host_setup
from octop.infra.agents.plugins.host_contracts import PluginHostError


def _tool_context(plugin_id: str = "reader") -> PluginContext:
    ctx = PluginContext(
        manifest=PluginManifest(
            id=plugin_id,
            version="1.0.0",
            name=plugin_id,
            kind="tool",
            entry="plugin.py",
        ),
        source_path=Path("plugin"),
    )

    def read() -> str:
        return "read"

    ctx.tool("read_alias", read)
    ctx.tool("write_alias", read)
    return ctx


def test_setup_can_declare_one_original_sdk_registration_and_freezes_names() -> None:
    host = PluginHost(lambda: None)
    ctx = _tool_context()
    declared = ["read_alias"]
    register_host_setup(ctx, lambda facade: facade.register_read_only_tools(declared))

    facade = host.bind_context(ctx, "1.0.0")
    declared.append("write_alias")

    assert facade.plugin_id == "reader"
    with pytest.raises(PluginHostError):
        facade.register_read_only_tools(["write_alias"])


@pytest.mark.parametrize("names", ["read_alias", [], ["unknown"], ["read_alias", "read_alias"]])
def test_setup_rejects_malformed_or_ambiguous_read_only_declarations(names: object) -> None:
    host = PluginHost(lambda: None)
    ctx = _tool_context()
    register_host_setup(ctx, lambda facade: facade.register_read_only_tools(names))  # type: ignore[arg-type]

    with pytest.raises(PluginHostError):
        host.bind_context(ctx, "1.0.0")
    assert host.active_plugins == {}


def test_setup_declaration_requires_same_real_context_and_open_setup_callback() -> None:
    host = PluginHost(lambda: None)
    ctx = _tool_context()
    register_host_setup(ctx, lambda facade: facade.register_read_only_tools(["write_alias"]))

    ctx._octop_host_setup_callback = lambda facade: (
        facade.register_read_only_tools(["read_alias"]),
        facade.register_read_only_tools(["write_alias"]),
    )
    with pytest.raises(PluginHostError):
        host.bind_context(ctx, "1.0.0")

    facade = host.bind("reader", "1.0.0")
    with pytest.raises(PluginHostError):
        facade.register_read_only_tools(["read_alias"])
