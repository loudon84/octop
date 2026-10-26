"""Install, load, and expose plugins under ``~/.octop/plugins/``."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import weakref
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, cast
from urllib.parse import urlparse

import yaml
from octop_harness.plugins import (
    LoadedPlugin,
    PluginManifest,
    PluginRegistry,
    discover_plugin_dirs,
    load_plugin_dir,
    unload_plugin,
)

from octop.infra.agents.plugins.host import PluginHost
from octop.infra.agents.plugins.legacy_imports import ensure_legacy_harness_agent_alias
from octop.infra.errors import ErrorCode, OctopError, corrupt_config_error
from octop.infra.utils.json_file import (
    JsonFileCorruptError,
    read_json_object,
    write_json_atomic,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistryMutationState:
    epoch: int
    in_progress: bool


_coordinator_lock = threading.RLock()
_coordinator_hosts: weakref.WeakSet[PluginHost] = weakref.WeakSet()
_coordinator_epoch = 0
_coordinator_in_progress = False
_coordinator_owner = False
_coordinator_waiters: list[concurrent.futures.Future[None]] = []
_coordinator_idle_waiters: list[concurrent.futures.Future[None]] = []
_coordinator_retired_tasks: dict[asyncio.Task[Any], weakref.WeakSet[PluginHost]] = {}
_mutation_permit = threading.local()


def register_host(host: PluginHost) -> None:
    """Register an empty runtime Host before its first plugin bind."""
    with _coordinator_lock:
        if _coordinator_in_progress:
            raise RuntimeError("cannot register a plugin Host during a registry mutation")
        if host.active_plugins:
            raise RuntimeError("plugin Host must be registered before its first bind")
        _coordinator_hosts.add(host)


def unregister_host(host: PluginHost) -> None:
    """Remove a runtime Host after its close and task drain have completed."""
    with _coordinator_lock:
        _prune_retired_tasks_locked()
        if not host.is_closed or host.active_plugins:
            raise RuntimeError("plugin Host must be closed before it is unregistered")
        if any(host in owners for owners in _coordinator_retired_tasks.values()):
            raise RuntimeError("cannot unregister Host with pending plugin cleanup")
        _coordinator_hosts.discard(host)


def current_state() -> RegistryMutationState:
    """Return the process registry epoch and mutation admission state atomically."""
    with _coordinator_lock:
        return RegistryMutationState(_coordinator_epoch, _coordinator_in_progress)


@contextmanager
def registry_state_guard() -> Iterator[RegistryMutationState]:
    """Hold the coordinator's short state lock for atomic mount admission/publication."""
    with _coordinator_lock:
        yield RegistryMutationState(_coordinator_epoch, _coordinator_in_progress)


def _require_mutation_permit(manager: PluginManager) -> None:
    if getattr(_mutation_permit, "depth", 0):
        return
    with _coordinator_lock:
        if manager.host is not None or _coordinator_hosts:
            raise RuntimeError("runtime plugin mutation requires async mutate()")


def _with_mutation_permit(operation: Callable[[], Any]) -> Any:
    depth = getattr(_mutation_permit, "depth", 0)
    _mutation_permit.depth = depth + 1
    try:
        return operation()
    finally:
        _mutation_permit.depth = depth


def _begin_reservation() -> None:
    global _coordinator_epoch, _coordinator_in_progress
    with _coordinator_lock:
        _coordinator_epoch += 1
        _coordinator_in_progress = True


async def _acquire_reservation() -> None:
    global _coordinator_owner
    with _coordinator_lock:
        if not _coordinator_owner:
            _coordinator_owner = True
            _begin_reservation()
            return
        waiter: concurrent.futures.Future[None] = concurrent.futures.Future()
        _coordinator_waiters.append(waiter)
    try:
        await asyncio.wrap_future(waiter)
    except asyncio.CancelledError:
        if waiter.running() or (waiter.done() and not waiter.cancelled()):
            _release_reservation()
        raise
    _begin_reservation()


def _is_plugin_task(task: asyncio.Task[Any] | None) -> bool:
    if task is None:
        return False
    with _coordinator_lock:
        hosts = tuple(_coordinator_hosts)
    return any(host.owns_task(task) for host in hosts)


def _release_reservation() -> None:
    global _coordinator_owner, _coordinator_in_progress
    with _coordinator_lock:
        while _coordinator_waiters:
            waiter = _coordinator_waiters.pop(0)
            if _notify_reservation_waiter(waiter):
                break
        else:
            _coordinator_owner = False
            _coordinator_in_progress = False
            idle_waiters = tuple(_coordinator_idle_waiters)
            _coordinator_idle_waiters.clear()
            for idle_waiter in idle_waiters:
                _notify_reservation_waiter(idle_waiter)
            return


def _notify_reservation_waiter(waiter: concurrent.futures.Future[None]) -> bool:
    """Claim a Future atomically with cancellation before delivering its grant."""
    if not waiter.set_running_or_notify_cancel():
        return False
    waiter.set_result(None)
    return True


def _prune_retired_tasks_locked() -> None:
    for task in tuple(_coordinator_retired_tasks):
        if task.done():
            _coordinator_retired_tasks.pop(task, None)


def _retain_retired_tasks(
    groups: tuple[tuple[PluginHost, tuple[asyncio.Task[Any], ...]], ...],
) -> tuple[asyncio.Task[Any], ...]:
    with _coordinator_lock:
        _prune_retired_tasks_locked()
        for host, tasks in groups:
            for task in tasks:
                owners = _coordinator_retired_tasks.setdefault(task, weakref.WeakSet())
                owners.add(host)
        return tuple(_coordinator_retired_tasks)


async def _run_mutation[R](
    operation: Callable[[], R], manager: PluginManager | None, initiator: asyncio.Task[Any] | None
) -> R:
    try:
        with _coordinator_lock:
            hosts = tuple(_coordinator_hosts)
        if manager is not None and manager.host is not None and manager.host not in hosts:
            raise RuntimeError("plugin manager Host must be registered before runtime mutation")
        if initiator is not None and any(host.owns_task(initiator) for host in hosts):
            raise RuntimeError("a plugin action cannot mutate its own Registry generation")

        def invalidate() -> tuple[tuple[PluginHost, tuple[asyncio.Task[Any], ...]], ...]:
            groups: list[tuple[PluginHost, tuple[asyncio.Task[Any], ...]]] = []
            for host in hosts:
                tasks: list[asyncio.Task[Any]] = []
                for plugin_id in tuple(host.active_plugins):
                    tasks.extend(host.deactivate(plugin_id))
                if tasks:
                    groups.append((host, tuple(dict.fromkeys(tasks))))
            return tuple(groups)

        groups = await asyncio.get_running_loop().run_in_executor(None, invalidate)
        tasks = _retain_retired_tasks(groups)
        from octop.infra.agents.plugins.host import drain_plugin_tasks

        await drain_plugin_tasks(tasks)
        with _coordinator_lock:
            _prune_retired_tasks_locked()
        raw_result = await asyncio.get_running_loop().run_in_executor(
            None, _with_mutation_permit, operation
        )
        host = None if manager is None else manager.host
        if manager is not None and host is not None:

            def rebind() -> None:
                enabled = manager.global_enabled_map()
                active = set(host.active_plugins)
                for plugin in PluginRegistry().all_loaded():
                    plugin_id = plugin.manifest.id
                    if enabled.get(plugin_id, True) is not False and plugin_id not in active:
                        manager._bind_loaded(plugin)

            await asyncio.get_running_loop().run_in_executor(None, _with_mutation_permit, rebind)
        return cast(R, raw_result)
    finally:
        _release_reservation()


async def mutate[R](
    operation: Callable[[], R],
    *,
    manager: PluginManager | None = None,
    affected_plugin_ids: frozenset[str] | None = None,
) -> R:
    """Serialize runtime Registry changes, draining every registered Host first."""
    del affected_plugin_ids  # The global epoch deliberately invalidates every Host.
    initiator = asyncio.current_task()
    if _is_plugin_task(initiator):
        raise RuntimeError("a plugin action cannot mutate its own Registry generation")
    await _acquire_reservation()
    if _is_plugin_task(initiator):
        _release_reservation()
        raise RuntimeError("a plugin action cannot mutate its own Registry generation")
    owner = asyncio.create_task(_run_mutation(operation, manager, initiator))
    owner.add_done_callback(_consume_mutation_result)
    return await asyncio.shield(owner)


def _consume_mutation_result(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def wait_for_mutations() -> None:
    """Wait until active and already queued registry mutations have finished."""
    with _coordinator_lock:
        if not _coordinator_in_progress:
            return
        waiter: concurrent.futures.Future[None] = concurrent.futures.Future()
        _coordinator_idle_waiters.append(waiter)
    await asyncio.wrap_future(waiter)


_GITHUB_BLOB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:blob|raw)/"
    r"(?P<ref>[^/]+)/(?P<path>.+)$",
    re.IGNORECASE,
)


_CHANNEL_FINGERPRINTS: dict[str, str] = {}


def _lifecycle_serialized[**P, R](
    method: Callable[Concatenate[PluginManager, P], R],
) -> Callable[Concatenate[PluginManager, P], R]:
    @wraps(method)
    def serialized(
        self: PluginManager,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        _require_mutation_permit(self)
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)

    return cast(Callable[Concatenate["PluginManager", P], R], serialized)


def _channel_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or any(
            part in {"__pycache__", ".git"} for part in path.relative_to(directory).parts
        ):
            continue
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def normalize_plugin_download_url(url: str) -> str:
    """Rewrite GitHub ``/blob/`` (and ``/raw/``) page URLs to raw content URLs.

    ``https://github.com/org/repo/blob/main/plugin.zip`` returns HTML, not the
    archive — that is the common cause of ``BadZipFile`` during install.
    """
    cleaned = url.strip()
    match = _GITHUB_BLOB_RE.match(cleaned)
    if match is None:
        return cleaned
    owner = match.group("owner")
    repo = match.group("repo")
    ref = match.group("ref")
    path = match.group("path").lstrip("/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def _load_plugin_dir(plugin_dir: Path, *, install_deps: bool) -> LoadedPlugin:
    """Load one plugin, accepting the pre-rename ``harness_agent`` import."""
    ensure_legacy_harness_agent_alias()
    return load_plugin_dir(plugin_dir, install_deps=install_deps)


def _read_global_plugins(config_path: Path) -> dict[str, bool]:
    if not config_path.is_file():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    out: dict[str, bool] = {}
    for plugin_id, entry in plugins.items():
        if isinstance(entry, dict):
            out[str(plugin_id)] = bool(entry.get("enabled", True))
        else:
            out[str(plugin_id)] = True
    return out


def _write_global_plugin_enabled(config_path: Path, plugin_id: str, enabled: bool) -> None:
    """Merge ``plugins.<id>.enabled`` into ``config.json`` without dropping other keys.

    A corrupt config.json raises instead of being treated as empty: merging into
    ``{}`` and writing back destroys every other setting, including the
    ``database`` section (issue #730).
    """
    try:
        data = read_json_object(config_path)
    except JsonFileCorruptError as exc:
        raise corrupt_config_error(exc.path, exc.detail) from exc
    if data is None:
        data = {}
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    entry = plugins.get(plugin_id)
    if not isinstance(entry, dict):
        entry = {}
    entry["enabled"] = bool(enabled)
    plugins[plugin_id] = entry
    write_json_atomic(config_path, data)


def _assert_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OctopError(
            ErrorCode.PLUGIN_INVALID_ARCHIVE,
            "plugin URL must be an http(s) address to a ZIP archive",
        )


def _assert_zip_magic(archive: Path) -> None:
    head = archive.read_bytes()[:4]
    if len(head) < 2 or head[:2] != b"PK":
        raise OctopError(
            ErrorCode.PLUGIN_INVALID_ARCHIVE,
            "file is not a valid ZIP archive",
        )


def _read_plugin_yaml(plugin_dir: Path) -> dict[str, Any]:
    raw = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def parse_plugin_ui_meta(plugin_dir: Path) -> dict[str, str] | None:
    """Return ``{entry, manifest}`` relative paths when ``plugin.yaml`` declares ``ui``.

    Missing entry file → ``None`` (treat as backend-only). Harness ignores the
    ``ui`` key; Octop surfaces it for Dashboard dynamic loading.
    """
    try:
        data = _read_plugin_yaml(plugin_dir)
    except Exception:
        return None
    ui = data.get("ui")
    if not isinstance(ui, dict):
        return None
    entry = str(ui.get("entry") or "ui/dist/index.js").strip()
    manifest = str(ui.get("manifest") or "ui/dist/manifest.json").strip()
    if not entry or ".." in entry.replace("\\", "/").split("/"):
        return None
    if ".." in manifest.replace("\\", "/").split("/"):
        return None
    if not (plugin_dir / entry).is_file():
        logger.warning(
            "plugin %s declares ui.entry=%s but file is missing",
            plugin_dir.name,
            entry,
        )
        return None
    return {"entry": entry, "manifest": manifest}


_ICON_FILE_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Stable catalog slugs for Dashboard grouping. Unknown values still pass through.
PLUGIN_GROUPS: frozenset[str] = frozenset(
    {
        "lifestyle",
        "news",
        "finance",
        "media",
        "fun",
        "games",
        "tools",
        "ops",
    },
)


def parse_plugin_group(plugin_dir: Path) -> str | None:
    """Optional ``group`` slug from ``plugin.yaml`` (e.g. ``lifestyle``, ``games``)."""
    try:
        data = _read_plugin_yaml(plugin_dir)
    except Exception:
        return None
    raw = data.get("group")
    if raw is None:
        return None
    group = str(raw).strip().lower().replace("_", "-")
    if not group or len(group) > 64:
        return None
    if not all(ch.isalnum() or ch == "-" for ch in group):
        return None
    return group


def parse_plugin_icon(
    plugin_dir: Path,
    *,
    asset_prefix: str | None = None,
) -> str | None:
    """Optional ``icon`` from ``plugin.yaml`` for Dashboard cards.

    Accepts emoji text, absolute ``http(s)://`` / ``/api/...`` URLs, or a
    plugin-relative image path (e.g. ``icon.svg``) resolved to
    ``/api/plugins/{id}/ui/...`` (or ``asset_prefix`` when listing the market).
    """
    try:
        data = _read_plugin_yaml(plugin_dir)
    except Exception:
        return None
    raw = data.get("icon")
    if raw is None:
        return None
    icon = str(raw).strip()
    if not icon or len(icon) > 2048:
        return None
    if icon.startswith(("http://", "https://", "/api/", "data:image/")) or icon.startswith("//"):
        return icon
    # Relative image file shipped with the plugin package.
    cleaned = icon.lstrip("/").replace("\\", "/")
    if (
        cleaned
        and ".." not in cleaned.split("/")
        and Path(cleaned).suffix.lower() in _ICON_FILE_SUFFIXES
    ):
        target = (plugin_dir / cleaned).resolve()
        try:
            target.relative_to(plugin_dir.resolve())
        except ValueError:
            return icon
        if target.is_file():
            plugin_id = str(data.get("id") or plugin_dir.name).strip() or plugin_dir.name
            prefix = (asset_prefix or f"/api/plugins/{plugin_id}/ui").rstrip("/")
            return f"{prefix}/{cleaned}"
    return icon


def parse_plugin_requires(plugin_dir: Path) -> list[str]:
    try:
        data = _read_plugin_yaml(plugin_dir)
    except Exception:
        return []
    raw = data.get("requires") or []
    if not isinstance(raw, list):
        return []
    return [str(r).strip() for r in raw if str(r).strip()]


def _read_installed_version(plugin_dir: Path) -> str | None:
    try:
        data = _read_plugin_yaml(plugin_dir)
    except Exception:
        return None
    ver = data.get("version")
    if ver is None:
        return None
    text = str(ver).strip()
    return text or None


def _version_tuple(version: str | None) -> tuple[int, ...]:
    ver = str(version or "0").strip() or "0"
    parts: list[int] = []
    for bit in ver.split("."):
        try:
            parts.append(int(bit))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


_MARKET_ASSET_SUFFIXES = frozenset({".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"})


class PluginManager:
    def __init__(
        self,
        *,
        plugins_dir: Path,
        config_path: Path,
        host: PluginHost | None = None,
    ) -> None:
        self._plugins_dir = plugins_dir
        self._config_path = config_path
        self._host = host
        self._lifecycle_lock = threading.RLock()
        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        # Last-known tool metadata so Admin / Experts can still list tools after
        # a global disable unloads the plugin from the process registry.
        self._tool_catalog: dict[str, list[dict[str, Any]]] = {}
        self._skill_catalog: dict[str, set[str]] = {}

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    @property
    def host(self) -> PluginHost | None:
        return self._host

    async def mutate[R](self, operation: Callable[[], R]) -> R:
        """Run an application lifecycle operation through the process coordinator."""
        return await mutate(operation, manager=self)

    def global_enabled_map(self) -> dict[str, bool]:
        return _read_global_plugins(self._config_path)

    @_lifecycle_serialized
    def seed_bundled(self) -> list[str]:
        """Upgrade already-installed bundled plugins when catalog versions are newer."""
        from octop.infra.agents.plugins.bundled import default_bundled_plugins_root
        from octop.infra.agents.plugins.seed import seed_bundled_plugins

        return seed_bundled_plugins(
            bundled_root=default_bundled_plugins_root(),
            plugins_dir=self._plugins_dir,
            config_path=self._config_path,
        )

    def market_root(self) -> Path:
        """In-tree marketplace catalog root (future: remote index)."""
        from octop.infra.agents.plugins.bundled import default_bundled_plugins_root

        return default_bundled_plugins_root()

    def market_plugin_dir(self, plugin_id: str) -> Path | None:
        """Return a catalog plugin directory when it exists under the market root."""
        cleaned = plugin_id.strip()
        if not cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned:
            return None
        dest = self.market_root() / cleaned
        if dest.is_dir() and (dest / "plugin.yaml").is_file():
            return dest
        return None

    def list_market(self) -> list[dict[str, Any]]:
        """List marketplace catalog entries (in-tree today; remote API later)."""
        root = self.market_root()
        out: list[dict[str, Any]] = []
        if not root.is_dir():
            return out
        for plugin_dir in discover_plugin_dirs(root):
            try:
                manifest = PluginManifest.load(plugin_dir / "plugin.yaml")
            except Exception as exc:
                out.append(
                    {
                        "id": plugin_dir.name,
                        "error": str(exc),
                        "installed": (self._plugins_dir / plugin_dir.name).is_dir(),
                    },
                )
                continue
            installed_dir = self.plugin_dir(manifest.id)
            installed_version = (
                None if installed_dir is None else _read_installed_version(installed_dir)
            )
            out.append(
                {
                    "id": manifest.id,
                    "version": manifest.version,
                    "name": manifest.name,
                    "kind": manifest.kind,
                    "description": manifest.description,
                    "icon": parse_plugin_icon(
                        plugin_dir,
                        asset_prefix=f"/api/plugins/market/{manifest.id}/ui",
                    ),
                    "group": parse_plugin_group(plugin_dir),
                    "requires": parse_plugin_requires(plugin_dir),
                    "installed": installed_dir is not None,
                    "installed_version": installed_version,
                    "update_available": (
                        installed_dir is not None
                        and _version_tuple(manifest.version) > _version_tuple(installed_version)
                    ),
                },
            )
        return out

    @_lifecycle_serialized
    def install_from_market(self, plugin_id: str, *, force: bool = False) -> LoadedPlugin:
        """Install a catalog plugin by copying the in-tree package locally.

        Future: download a ZIP from a remote API / object storage and extract.
        """
        source = self.market_plugin_dir(plugin_id)
        if source is None:
            raise OctopError(
                ErrorCode.NOT_FOUND,
                f"marketplace plugin {plugin_id!r} not found",
            )
        loaded = self.install_path(source, force=force)
        _write_global_plugin_enabled(self._config_path, loaded.manifest.id, True)
        return loaded

    def resolve_market_ui_file(self, plugin_id: str, rel_path: str) -> Path:
        """Resolve a marketplace catalog asset (icons only)."""
        plugin_dir = self.market_plugin_dir(plugin_id)
        if plugin_dir is None:
            raise OctopError(
                ErrorCode.NOT_FOUND,
                f"marketplace plugin {plugin_id!r} not found",
            )
        cleaned = rel_path.strip().lstrip("/").replace("\\", "/")
        suffix = Path(cleaned).suffix.lower()
        if suffix not in _MARKET_ASSET_SUFFIXES:
            raise OctopError(ErrorCode.NOT_FOUND, "invalid marketplace asset path")
        return self._resolve_plugin_asset(plugin_dir, cleaned)

    @_lifecycle_serialized
    def load_installed(self, *, install_deps: bool = True) -> list[LoadedPlugin]:
        enabled = self.global_enabled_map()
        previous = {plugin.manifest.id: plugin for plugin in PluginRegistry().all_loaded()}
        if self._host is not None:
            for plugin_id in tuple(self._host.active_plugins):
                self._host.deactivate(plugin_id)
        PluginRegistry().clear()
        loaded: list[LoadedPlugin] = []
        for plugin_dir in discover_plugin_dirs(self._plugins_dir):
            try:
                manifest = PluginManifest.load(plugin_dir / "plugin.yaml")
            except Exception as exc:
                logger.error("skip plugin dir %s: %s", plugin_dir, exc)
                continue
            if enabled.get(manifest.id, True) is False:
                continue
            try:
                fingerprint = (
                    _channel_fingerprint(plugin_dir) if str(manifest.kind) == "channel" else ""
                )
                old = previous.get(manifest.id)
                if (
                    str(manifest.kind) == "channel"
                    and old is not None
                    and _CHANNEL_FINGERPRINTS.get(manifest.id) == fingerprint
                ):
                    PluginRegistry().register(old)
                    plugin = old
                else:
                    plugin = _load_plugin_dir(plugin_dir, install_deps=install_deps)
                self._bind_loaded(plugin)
                loaded.append(plugin)
                if str(manifest.kind) == "channel":
                    _CHANNEL_FINGERPRINTS[manifest.id] = fingerprint
            except Exception as exc:
                logger.error(
                    "failed to load plugin from %s: %s",
                    plugin_dir,
                    exc,
                    exc_info=True,
                )
        for plugin_id, is_on in enabled.items():
            if not is_on:
                if self._host is not None:
                    self._host.deactivate(plugin_id)
                unload_plugin(plugin_id)
        return [p for p in loaded if enabled.get(p.manifest.id, True) is not False]

    @_lifecycle_serialized
    def load_missing(self, *, install_deps: bool = False) -> list[LoadedPlugin]:
        """Load any on-disk plugins that are not yet in the process registry.

        Used after CLI ``octop plugin install`` while ``octop run`` is already
        up — unlike ``load_installed``, this does not clear already-loaded
        plugins.
        """
        enabled = self.global_enabled_map()
        newly: list[LoadedPlugin] = []
        for plugin_dir in discover_plugin_dirs(self._plugins_dir):
            try:
                manifest = PluginManifest.load(plugin_dir / "plugin.yaml")
            except Exception as exc:
                logger.error("skip plugin dir %s: %s", plugin_dir, exc)
                continue
            if enabled.get(manifest.id) is False:
                continue
            if PluginRegistry().get(manifest.id) is not None:
                continue
            try:
                plugin = _load_plugin_dir(plugin_dir, install_deps=install_deps)
                self._bind_loaded(plugin)
                newly.append(plugin)
                logger.info(
                    "loaded missing plugin %s v%s",
                    manifest.id,
                    manifest.version,
                )
            except Exception as exc:
                logger.error(
                    "failed to load plugin from %s: %s",
                    plugin_dir,
                    exc,
                    exc_info=True,
                )
        return newly

    def _bind_loaded(self, plugin: LoadedPlugin) -> None:
        if self._host is None:
            return
        try:
            self._host.bind_context(plugin.context, str(plugin.manifest.version))
        except BaseException:
            if PluginRegistry().get(plugin.manifest.id) is plugin:
                unload_plugin(plugin.manifest.id)
            raise

    @staticmethod
    def _channel_registration_error(plugin_id: str) -> str | None:
        from octop.infra.gateway.plugin_channels import registration_errors

        return registration_errors().get(plugin_id)

    def list_installed(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        enabled_map = self.global_enabled_map()
        for plugin_dir in discover_plugin_dirs(self._plugins_dir):
            try:
                manifest = PluginManifest.load(plugin_dir / "plugin.yaml")
            except Exception as exc:
                out.append(
                    {
                        "id": plugin_dir.name,
                        "error": str(exc),
                        "path": str(plugin_dir),
                        "enabled": enabled_map.get(plugin_dir.name, True),
                    },
                )
                continue
            loaded = PluginRegistry().get(manifest.id)
            ui_meta = parse_plugin_ui_meta(plugin_dir)
            if loaded is not None:
                tools_meta = [
                    {
                        "name": t.name,
                        "description": t.description,
                        "config_fields": t.config_fields,
                    }
                    for t in loaded.tools
                ]
                self._tool_catalog[manifest.id] = tools_meta
                if loaded.skills_dir is not None:
                    self._skill_catalog[manifest.id] = {
                        path.name
                        for path in loaded.skills_dir.iterdir()
                        if path.is_dir() and (path / "SKILL.md").is_file()
                    }
            else:
                tools_meta = list(self._tool_catalog.get(manifest.id) or [])
            out.append(
                {
                    "id": manifest.id,
                    "version": manifest.version,
                    "name": manifest.name,
                    "kind": manifest.kind,
                    "description": manifest.description,
                    "icon": parse_plugin_icon(plugin_dir),
                    "group": parse_plugin_group(plugin_dir),
                    "requires": parse_plugin_requires(plugin_dir),
                    "path": str(plugin_dir),
                    "loaded": loaded is not None,
                    "registration_error": self._channel_registration_error(manifest.id),
                    "enabled": enabled_map.get(manifest.id, True),
                    "ui": ui_meta,
                    "tools": tools_meta,
                },
            )
        return out

    @_lifecycle_serialized
    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        """Toggle global plugin enablement in ``config.json`` and load/unload registry."""
        plugin_dir = self.plugin_dir(plugin_id)
        if plugin_dir is None:
            raise OctopError(ErrorCode.NOT_FOUND, f"plugin {plugin_id!r} not found")
        try:
            PluginManifest.load(plugin_dir / "plugin.yaml")
        except Exception as exc:
            raise OctopError(
                ErrorCode.PLUGIN_INVALID_ARCHIVE,
                f"invalid plugin manifest: {exc}",
            ) from exc

        _write_global_plugin_enabled(self._config_path, plugin_id, enabled)
        if enabled:
            if PluginRegistry().get(plugin_id) is None:
                try:
                    plugin = _load_plugin_dir(plugin_dir, install_deps=False)
                    self._bind_loaded(plugin)
                except Exception as exc:
                    raise OctopError(
                        ErrorCode.PLUGIN_INSTALL_FAILED,
                        f"failed to load plugin: {exc}",
                        details={"reason": str(exc)},
                    ) from exc
        else:
            if self._host is not None:
                self._host.deactivate(plugin_id)
            loaded = PluginRegistry().get(plugin_id)
            if loaded is not None:
                self._tool_catalog[plugin_id] = [
                    {
                        "name": t.name,
                        "description": t.description,
                        "config_fields": t.config_fields,
                    }
                    for t in loaded.tools
                ]
                if loaded.skills_dir is not None:
                    self._skill_catalog[plugin_id] = {
                        path.name
                        for path in loaded.skills_dir.iterdir()
                        if path.is_dir() and (path / "SKILL.md").is_file()
                    }
            unload_plugin(plugin_id)

        for item in self.list_installed():
            if item.get("id") == plugin_id:
                return item
        return {"id": plugin_id, "enabled": enabled}

    def plugin_dir(self, plugin_id: str) -> Path | None:
        """Return the on-disk plugin directory when it exists."""
        dest = self._plugins_dir / plugin_id
        if dest.is_dir() and (dest / "plugin.yaml").is_file():
            return dest
        return None

    def resolve_ui_file(self, plugin_id: str, rel_path: str) -> Path:
        """Resolve a UI asset under the plugin tree with path-traversal checks.

        ``rel_path`` is typically relative to ``<plugin>/ui/`` (e.g. ``dist/index.js``)
        as served by ``GET /api/plugins/{id}/ui/{path}``. Full paths from the
        plugin root (``ui/dist/index.js``) are also accepted.
        """
        plugin_dir = self.plugin_dir(plugin_id)
        if plugin_dir is None:
            raise OctopError(
                ErrorCode.NOT_FOUND,
                f"plugin {plugin_id!r} not found",
            )
        return self._resolve_plugin_asset(plugin_dir, rel_path)

    @staticmethod
    def _resolve_plugin_asset(plugin_dir: Path, rel_path: str) -> Path:
        cleaned = rel_path.strip().lstrip("/").replace("\\", "/")
        if not cleaned or any(part == ".." for part in cleaned.split("/")):
            raise OctopError(ErrorCode.NOT_FOUND, "invalid plugin UI path")
        candidates = [
            plugin_dir / "ui" / cleaned,
            plugin_dir / cleaned,
        ]
        if not cleaned.startswith("ui/") and not cleaned.startswith("dist/"):
            candidates.append(plugin_dir / "ui" / "dist" / cleaned)
        for target in candidates:
            resolved = target.resolve()
            try:
                resolved.relative_to(plugin_dir.resolve())
            except ValueError as exc:
                raise OctopError(ErrorCode.NOT_FOUND, "invalid plugin UI path") from exc
            if resolved.is_file():
                return resolved
        raise OctopError(
            ErrorCode.NOT_FOUND,
            f"plugin UI file not found: {cleaned}",
        )

    @_lifecycle_serialized
    def install_path(self, source: Path, *, force: bool = False) -> LoadedPlugin:
        source = source.resolve()
        if not source.is_dir():
            raise OctopError(
                ErrorCode.PLUGIN_INVALID_ARCHIVE,
                f"plugin directory not found: {source}",
            )
        try:
            manifest = PluginManifest.load(source / "plugin.yaml")
        except Exception as exc:
            raise OctopError(
                ErrorCode.PLUGIN_INVALID_ARCHIVE,
                f"invalid plugin manifest: {exc}",
            ) from exc
        dest = self._plugins_dir / manifest.id
        if dest.exists():
            if not force:
                raise OctopError(
                    ErrorCode.PLUGIN_ALREADY_EXISTS,
                    f"plugin already installed: {manifest.id}",
                    details={"id": manifest.id},
                )
            if self._host is not None:
                self._host.deactivate(manifest.id)
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        unload_plugin(manifest.id)
        try:
            loaded = _load_plugin_dir(dest, install_deps=True)
            self._bind_loaded(loaded)
            return loaded
        except Exception as exc:
            raise OctopError(
                ErrorCode.PLUGIN_INSTALL_FAILED,
                f"failed to load plugin: {exc}",
                details={"reason": str(exc)},
            ) from exc

    @_lifecycle_serialized
    def install_archive(self, archive: Path, *, force: bool = False) -> LoadedPlugin:
        """Install a plugin from a local ZIP archive.

        The archive must contain exactly one plugin directory (at the zip root
        or in a single top-level folder) with a ``plugin.yaml`` manifest.
        """
        _assert_zip_magic(archive)
        with tempfile.TemporaryDirectory() as tmp:
            extract_to = Path(tmp) / "extract"
            extract_to.mkdir()
            try:
                with zipfile.ZipFile(archive) as zf:
                    for member in zf.namelist():
                        target = (extract_to / member).resolve()
                        if not str(target).startswith(str(extract_to.resolve())):
                            raise OctopError(
                                ErrorCode.PLUGIN_INVALID_ARCHIVE,
                                "zip path traversal detected",
                            )
                    zf.extractall(extract_to)
            except OctopError:
                raise
            except zipfile.BadZipFile as exc:
                raise OctopError(
                    ErrorCode.PLUGIN_INVALID_ARCHIVE,
                    "file is not a valid ZIP archive",
                ) from exc

            # Support zip root being the plugin dir or containing one subdir
            candidates = [p for p in extract_to.iterdir() if (p / "plugin.yaml").is_file()]
            if not candidates and (extract_to / "plugin.yaml").is_file():
                candidates = [extract_to]
            if len(candidates) != 1:
                raise OctopError(
                    ErrorCode.PLUGIN_INVALID_ARCHIVE,
                    "zip must contain exactly one plugin directory with plugin.yaml",
                )
            return self.install_path(candidates[0], force=force)

    @_lifecycle_serialized
    def install_url(self, url: str, *, force: bool = False) -> LoadedPlugin:
        resolved = normalize_plugin_download_url(url)
        _assert_http_url(resolved)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "plugin.zip"
            try:
                urllib.request.urlretrieve(resolved, archive)  # noqa: S310
            except urllib.error.HTTPError as exc:
                raise OctopError(
                    ErrorCode.PLUGIN_INSTALL_FAILED,
                    f"download failed with HTTP {exc.code}",
                    details={"reason": f"HTTP {exc.code}"},
                ) from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None) or str(exc)
                raise OctopError(
                    ErrorCode.PLUGIN_INSTALL_FAILED,
                    f"download failed: {reason}",
                    details={"reason": str(reason)},
                ) from exc
            except OSError as exc:
                raise OctopError(
                    ErrorCode.PLUGIN_INSTALL_FAILED,
                    f"download failed: {exc}",
                    details={"reason": str(exc)},
                ) from exc
            try:
                return self.install_archive(archive, force=force)
            except OctopError as exc:
                if exc.code is not ErrorCode.PLUGIN_INVALID_ARCHIVE:
                    raise
                raise OctopError(
                    ErrorCode.PLUGIN_INVALID_ARCHIVE,
                    f"{exc.message} (GitHub /blob/ pages return HTML — use a raw .zip URL)",
                    details=exc.details,
                ) from exc

    @_lifecycle_serialized
    def uninstall(self, plugin_id: str) -> None:
        if self._host is not None:
            self._host.deactivate(plugin_id)
        unload_plugin(plugin_id)
        self._tool_catalog.pop(plugin_id, None)
        self._skill_catalog.pop(plugin_id, None)
        dest = self._plugins_dir / plugin_id
        if dest.is_dir():
            shutil.rmtree(dest)

    def plugin_skill_names(self, plugin_id: str) -> set[str]:
        """Return skill directory names registered by one loaded plugin."""
        plugin = PluginRegistry().get(plugin_id)
        if plugin is None or plugin.skills_dir is None:
            return set(self._skill_catalog.get(plugin_id) or ())
        names = {
            path.name
            for path in plugin.skills_dir.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        }
        self._skill_catalog[plugin_id] = names
        return set(names)

    def sync_skills_to_workspace(
        self,
        workspace: Any,
        *,
        agent_plugins: object = None,
    ) -> None:
        from octop_harness.backends.workspace import BackendWorkspace

        from octop.infra.agents.plugins.plugin_tool_defaults import agent_plugin_enabled

        if not isinstance(workspace, BackendWorkspace):
            return
        enabled = self.global_enabled_map()
        pairs: list[tuple[str, bytes]] = []
        for plugin in PluginRegistry().list_plugins():
            if enabled.get(plugin.manifest.id) is False:
                continue
            if not agent_plugin_enabled(agent_plugins, plugin.manifest.id):
                continue
            if plugin.skills_dir is None:
                continue
            for skill_dir in plugin.skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                if not (skill_dir / "SKILL.md").is_file():
                    continue
                dest = f"skills/{skill_dir.name}"
                if workspace.exists(f"{dest}/SKILL.md"):
                    continue
                for path in skill_dir.rglob("*"):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(skill_dir).as_posix()
                    pairs.append((f"{dest}/{rel}", path.read_bytes()))
        if pairs:
            workspace.upload_many(pairs)
