from __future__ import annotations

from pathlib import Path
from typing import Any

from octop_harness.plugins import PluginRegistry


async def install_enterprise_knowledge(server: Any) -> Any:
    """Install through Octop's real manager and the installed Harness SDK loader."""
    assert server.plugin_manager is not None
    plugin_path = Path(__file__).resolve().parents[2] / "plugins" / "enterprise-knowledge"
    return await server.plugin_manager.mutate(
        lambda: server.plugin_manager.install_path(plugin_path, force=True)
    )


async def install_synthetic_enterprise_policy(server: Any, tmp_path: Path) -> Any:
    """Load an empty companion manifest to exercise Host required-extension checks locally."""
    assert server.plugin_manager is not None
    plugin_path = tmp_path / "enterprise-policy"
    plugin_path.mkdir()
    (plugin_path / "plugin.yaml").write_text(
        "id: enterprise-policy\nversion: 0.1.0\nname: Synthetic enterprise policy\n"
        "description: Test-only companion\nkind: tool\nentry: main.py\n",
        encoding="utf-8",
    )
    (plugin_path / "main.py").write_text(
        "from octop_harness.plugins import PluginContext\n\n"
        "def setup(ctx: PluginContext) -> None:\n    return None\n",
        encoding="utf-8",
    )
    return await server.plugin_manager.mutate(
        lambda: server.plugin_manager.install_path(plugin_path, force=True)
    )


def loaded_enterprise_knowledge() -> Any:
    plugin = PluginRegistry().get("enterprise-knowledge")
    assert plugin is not None
    return plugin
