"""Application-scoped, versioned host facade for trusted plugins."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel

from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    PluginHostExecutionError,
    PluginRequirementReadiness,
    PluginResource,
    PluginUserIdentity,
    PluginUserIdentityPage,
    RecordCheck,
    RecordWrite,
    RequiredExtensionState,
    RequiredExtensionWrite,
    RetainedRecord,
    RetainedRecordCheck,
    RetainedRecordPage,
    RetainedRecordWrite,
    UserIdentityCheck,
)
from octop.infra.users.email import normalize_email

_SETUP_CALLBACK_ATTR = "_octop_host_setup_callback"
_logger = logging.getLogger(__name__)
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_PERMISSIONS = {"authenticated", "admin", "agent_owner"}
_REQUIRED_NAMESPACE = "required_extensions"

HostSetupCallback = Callable[[Any], Any]
HostServicesProvider = Callable[[], Any | None]
ActionPermission = Literal["authenticated", "admin", "agent_owner"]
ActionHandler = Callable[[PluginActor, BaseModel], Any]
ResourceHandler = Callable[[PluginActor, str], Any]
_T = TypeVar("_T")


def register_host_setup(ctx: object, callback: HostSetupCallback) -> None:
    """Attach the optional host registration callback to its PluginContext."""
    if not callable(callback):
        raise PluginHostError("invalid")
    setattr(ctx, _SETUP_CALLBACK_ATTR, callback)


def host_setup_callback(ctx: object) -> HostSetupCallback | None:
    """Return the setup callback through the single public retrieval helper."""
    callback = getattr(ctx, _SETUP_CALLBACK_ATTR, None)
    return callback if callable(callback) else None


@dataclass
class _ServiceRegistration:
    factory: Callable[[], Any]
    required_plugins: dict[str, str]
    instances: dict[int, Any] = field(default_factory=dict)


@dataclass
class _ActionRegistration:
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ActionHandler
    summary: str
    permission: ActionPermission


@dataclass
class _BackgroundWorkerRegistration:
    worker: Callable[[asyncio.Event], Awaitable[None]]
    event: asyncio.Event | None = None
    task: asyncio.Task[Any] | None = None


@dataclass
class _PluginState:
    version: str
    generation: int
    setup_context: object | None = None
    setup_open: bool = False
    setup_completed: bool = False
    identity_consumer_registered: bool = False
    readonly_registered: bool = False
    readonly_tool_names: frozenset[str] = frozenset()
    actions: dict[str, _ActionRegistration] = field(default_factory=dict)
    resources: list[ResourceHandler] = field(default_factory=list)
    services: dict[int, _ServiceRegistration] = field(default_factory=dict)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    task_refs: dict[asyncio.Task[Any], int] = field(default_factory=dict)
    background_workers: dict[str, _BackgroundWorkerRegistration] = field(default_factory=dict)


def _track_task_locked(state: _PluginState, task: asyncio.Task[Any]) -> None:
    state.tasks.add(task)
    state.task_refs[task] = state.task_refs.get(task, 0) + 1


def _untrack_task_locked(state: _PluginState, task: asyncio.Task[Any]) -> None:
    count = state.task_refs.get(task, 0)
    if count <= 1:
        state.task_refs.pop(task, None)
        state.tasks.discard(task)
    else:
        state.task_refs[task] = count - 1


async def drain_plugin_tasks(tasks: Sequence[asyncio.Task[Any]]) -> None:
    """Await cancelled plugin tasks on each task's owning event loop."""
    current = asyncio.current_task()
    pending = tuple(dict.fromkeys(task for task in tasks if not task.done()))
    if current in pending:
        raise PluginHostError("unavailable")
    by_loop: dict[asyncio.AbstractEventLoop, list[asyncio.Task[Any]]] = {}
    for task in pending:
        loop = task.get_loop()
        if loop.is_closed() or not loop.is_running():
            raise PluginHostError("unavailable")
        by_loop.setdefault(loop, []).append(task)

    async def gather(group: Sequence[asyncio.Task[Any]]) -> None:
        await asyncio.gather(*group, return_exceptions=True)

    here = asyncio.get_running_loop()
    for loop, group in by_loop.items():
        if loop is here:
            await gather(group)
        else:
            future = asyncio.run_coroutine_threadsafe(gather(group), loop)
            try:
                await asyncio.wrap_future(future)
            except RuntimeError as exc:
                raise PluginHostError("unavailable") from exc


class PluginHost:
    """Owns plugin registration generations and resolves runtime services."""

    def __init__(self, services_provider: HostServicesProvider) -> None:
        self._services_provider = services_provider
        # Registry checks are also used synchronously on the event loop, so this
        # lock must never be held while waiting on storage or encryption work.
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._plugins: dict[str, _PluginState] = {}
        self._retired_tasks: set[asyncio.Task[Any]] = set()
        self._next_generation = 1
        self._closed = False
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._workers_started = False
        self._execution_ticket_key = object()

    @property
    def active_plugins(self) -> Mapping[str, str]:
        with self._lock:
            return {plugin_id: state.version for plugin_id, state in self._plugins.items()}

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def owns_task(self, task: asyncio.Task[Any]) -> bool:
        with self._lock:
            self._prune_retired_tasks_locked()
            return task in self._retired_tasks or any(
                task in state.tasks for state in self._plugins.values()
            )

    def _prune_retired_tasks_locked(self) -> None:
        for task in tuple(self._retired_tasks):
            if task.done():
                self._retired_tasks.discard(task)

    def _retain_retired_tasks_locked(self, tasks: Sequence[asyncio.Task[Any]]) -> None:
        for task in tasks:
            self._retired_tasks.add(task)
            with suppress(RuntimeError):
                task.get_loop().call_soon_threadsafe(
                    task.add_done_callback, self._retired_task_done
                )

    def _retired_task_done(self, task: asyncio.Task[Any]) -> None:
        with self._lock:
            self._retired_tasks.discard(task)

    def bind(self, plugin_id: str, version: str) -> PluginHostFacade:
        _validate_plugin_id(plugin_id)
        if not version or not version.strip():
            raise PluginHostError("invalid")
        old_tasks: tuple[asyncio.Task[Any], ...] = ()
        with self._operation_lock, self._lock:
            if self._closed:
                raise PluginHostError("unavailable")
            old = self._plugins.pop(plugin_id, None)
            if old is not None:
                old_tasks = tuple(old.tasks)
                self._retain_retired_tasks_locked(old_tasks)
            generation = self._next_generation
            self._next_generation += 1
            self._plugins[plugin_id] = _PluginState(version=version, generation=generation)
        self._cancel_tasks(old_tasks)
        return PluginHostFacade(self, plugin_id, generation)

    def bind_context(self, ctx: object, version: str) -> PluginHostFacade:
        plugin_id = getattr(ctx, "plugin_id", None)
        if not isinstance(plugin_id, str):
            raise PluginHostError("invalid")
        facade = self.bind(plugin_id, version)
        callback = host_setup_callback(ctx)
        if callback is not None:
            from octop_harness.plugins.context import PluginContext

            if not isinstance(ctx, PluginContext):
                self.deactivate_if_generation(plugin_id, facade._generation)
                raise PluginHostError("invalid")
            with self._lock:
                state = self._state(plugin_id, facade._generation)
                state.setup_context = ctx
                state.setup_open = True
            setup_succeeded = False
            try:
                result = callback(facade)
                if inspect.isawaitable(result):
                    raise PluginHostError("invalid")
                setup_succeeded = True
            except BaseException:
                self.deactivate_if_generation(plugin_id, facade._generation)
                raise
            finally:
                with self._lock:
                    current_state = self._plugins.get(plugin_id)
                    if current_state is not None and current_state.generation == facade._generation:
                        current_state.setup_open = False
                        current_state.setup_completed = setup_succeeded
            if setup_succeeded:
                self._schedule_generation_workers(plugin_id, facade._generation)
        return facade

    def _register_background_worker(
        self,
        plugin_id: str,
        generation: int,
        name: str,
        worker: Callable[[asyncio.Event], Awaitable[None]],
    ) -> None:
        from octop_harness.plugins.context import PluginContext

        if not isinstance(name, str) or not _ACTION_NAME_RE.fullmatch(name):
            raise PluginHostError("invalid")
        if not callable(worker) or not inspect.iscoroutinefunction(worker):
            raise PluginHostError("invalid")
        with self._lock:
            state = self._state(plugin_id, generation)
            context = state.setup_context
            if (
                not state.setup_open
                or not isinstance(context, PluginContext)
                or getattr(context, "plugin_id", None) != plugin_id
            ):
                raise PluginHostError("denied")
            if name in state.background_workers:
                raise PluginHostError("conflict")
            state.background_workers[name] = _BackgroundWorkerRegistration(worker)

    async def start_background_workers(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise PluginHostError("unavailable")
            if self._worker_loop is None:
                self._worker_loop = loop
                self._workers_started = True
            elif self._worker_loop is not loop:
                raise PluginHostError("unavailable")
            for plugin_id, state in self._plugins.items():
                if state.setup_completed:
                    for name, registration in state.background_workers.items():
                        self._start_worker_locked(plugin_id, state, name, registration)

    def wake_background_workers(self) -> None:
        with self._lock:
            loop = self._worker_loop
            if self._closed or not self._workers_started or loop is None or loop.is_closed():
                return
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(self._wake_workers_on_owner_loop, loop)

    def _schedule_generation_workers(self, plugin_id: str, generation: int) -> None:
        with self._lock:
            loop = self._worker_loop
            if self._closed or not self._workers_started or loop is None or loop.is_closed():
                return
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(
                self._start_generation_workers_on_owner_loop, loop, plugin_id, generation
            )

    def _start_generation_workers_on_owner_loop(
        self,
        loop: asyncio.AbstractEventLoop,
        plugin_id: str,
        generation: int,
    ) -> None:
        if loop.is_closed():
            return
        with self._lock:
            state = self._plugins.get(plugin_id)
            if (
                self._closed
                or self._worker_loop is not loop
                or state is None
                or state.generation != generation
                or not state.setup_completed
            ):
                return
            for name, registration in state.background_workers.items():
                self._start_worker_locked(plugin_id, state, name, registration)

    def _start_worker_locked(
        self,
        plugin_id: str,
        state: _PluginState,
        name: str,
        registration: _BackgroundWorkerRegistration,
    ) -> None:
        if registration.task is not None:
            return
        loop = self._worker_loop
        if loop is None or loop is not asyncio.get_running_loop():
            return
        wake = asyncio.Event()
        wake.set()
        registration.event = wake
        task = loop.create_task(
            self._run_background_worker(plugin_id, state.generation, name, registration, wake),
            name=f"octop-plugin-worker:{plugin_id}:{name}:{state.generation}",
        )
        registration.task = task
        _track_task_locked(state, task)

        def on_done(completed: asyncio.Task[Any], owned_state: _PluginState = state) -> None:
            self._background_worker_done(plugin_id, owned_state, name, completed)

        task.add_done_callback(on_done)

    async def _run_background_worker(
        self,
        plugin_id: str,
        generation: int,
        name: str,
        registration: _BackgroundWorkerRegistration,
        wake: asyncio.Event,
    ) -> None:
        try:
            with self._lock:
                state = self._plugins.get(plugin_id)
                if (
                    self._closed
                    or state is None
                    or state.generation != generation
                    or state.background_workers.get(name) is not registration
                ):
                    return
            await registration.worker(wake)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.warning(
                "plugin background worker failed plugin=%s name=%s generation=%d classification=exception",
                plugin_id,
                name,
                generation,
            )

    def _background_worker_done(
        self,
        plugin_id: str,
        state: _PluginState,
        name: str,
        task: asyncio.Task[Any],
    ) -> None:
        if not task.cancelled():
            task.exception()
        with self._lock:
            _untrack_task_locked(state, task)
        self._retired_task_done(task)

    def _wake_workers_on_owner_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if loop.is_closed():
            return
        with self._lock:
            if self._closed or self._worker_loop is not loop:
                return
            for state in self._plugins.values():
                for registration in state.background_workers.values():
                    if registration.task is not None and registration.event is not None:
                        registration.event.set()

    def _register_read_only_tools(
        self,
        plugin_id: str,
        generation: int,
        names: Sequence[str],
    ) -> None:
        from octop_harness.plugins.context import PluginContext

        if isinstance(names, str | bytes) or not isinstance(names, Sequence) or not names:
            raise PluginHostError("invalid")
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise PluginHostError("invalid")
        copied = tuple(name.strip() for name in names)
        if len(set(copied)) != len(copied):
            raise PluginHostError("invalid")
        with self._lock:
            state = self._state(plugin_id, generation)
            context = state.setup_context
            if (
                not state.setup_open
                or state.readonly_registered
                or not isinstance(context, PluginContext)
                or getattr(context, "plugin_id", None) != plugin_id
            ):
                raise PluginHostError("denied")
            registrations = getattr(context, "_tools", None)
            if not isinstance(registrations, list):
                raise PluginHostError("denied")
            registered_names = [getattr(item, "name", None) for item in registrations]
            if any(registered_names.count(name) != 1 for name in copied):
                raise PluginHostError("invalid")
            state.readonly_registered = True
            state.readonly_tool_names = frozenset(copied)

    def _register_user_identity_consumer(self, plugin_id: str, generation: int) -> None:
        from octop_harness.plugins.context import PluginContext

        with self._lock:
            state = self._state(plugin_id, generation)
            context = state.setup_context
            if (
                not state.setup_open
                or not isinstance(context, PluginContext)
                or getattr(context, "plugin_id", None) != plugin_id
            ):
                raise PluginHostError("denied")
            if state.identity_consumer_registered:
                raise PluginHostError("conflict")
            state.identity_consumer_registered = True

    def _mounted_read_only_tool_names(
        self,
        plugin_id: str,
        version: str,
        context: object,
    ) -> frozenset[str]:
        with self._lock:
            state = self._plugins.get(plugin_id)
            if state is None or state.version != version or state.setup_context is not context:
                return frozenset()
            return state.readonly_tool_names

    def facade(self, plugin_id: str) -> PluginHostFacade:
        with self._lock:
            state = self._plugins.get(plugin_id)
            if self._closed or state is None:
                raise PluginHostError("unavailable")
            return PluginHostFacade(self, plugin_id, state.generation)

    def deactivate(self, plugin_id: str) -> tuple[asyncio.Task[Any], ...]:
        with self._operation_lock, self._lock:
            state = self._plugins.pop(plugin_id, None)
            tasks = () if state is None else tuple(state.tasks)
            self._retain_retired_tasks_locked(tasks)
        self._cancel_tasks(tasks)
        return tasks

    def deactivate_if_generation(
        self, plugin_id: str, generation: int
    ) -> tuple[asyncio.Task[Any], ...]:
        """Invalidate one failed registration without touching a later bind."""
        with self._operation_lock, self._lock:
            state = self._plugins.get(plugin_id)
            if state is None or state.generation != generation:
                return ()
            self._plugins.pop(plugin_id)
            tasks = tuple(state.tasks)
            self._retain_retired_tasks_locked(tasks)
        self._cancel_tasks(tasks)
        return tasks

    def close(self) -> tuple[asyncio.Task[Any], ...]:
        with self._operation_lock, self._lock:
            tasks = tuple(task for state in self._plugins.values() for task in state.tasks)
            self._plugins.clear()
            self._retain_retired_tasks_locked(tasks)
            self._closed = True
        self._cancel_tasks(tasks)
        return tasks

    async def deactivate_and_wait(self, plugin_id: str) -> None:
        loop = asyncio.get_running_loop()
        tasks = await loop.run_in_executor(None, self.deactivate, plugin_id)
        await drain_plugin_tasks(tasks)

    async def deactivate_all_and_wait(self) -> None:
        with self._lock:
            plugin_ids = tuple(self._plugins)
        task_groups = await asyncio.gather(
            *(self.deactivate_and_wait(plugin_id) for plugin_id in plugin_ids),
            return_exceptions=True,
        )
        del task_groups

    async def close_and_wait(self) -> None:
        loop = asyncio.get_running_loop()
        tasks = await loop.run_in_executor(None, self.close)
        await drain_plugin_tasks(tasks)

    @staticmethod
    def _cancel_tasks(tasks: Sequence[asyncio.Task[Any]]) -> None:
        for task in tasks:
            with suppress(RuntimeError):
                task.get_loop().call_soon_threadsafe(task.cancel)

    @staticmethod
    async def _wait_tasks(
        tasks: Sequence[asyncio.Task[Any]], loop: asyncio.AbstractEventLoop
    ) -> None:
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current and task.get_loop() is loop]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def resolve_service(self, plugin_id: str, api_version: int) -> Any:
        with self._lock:
            state = self._plugins.get(plugin_id)
            if state is None:
                raise PluginHostError("unavailable")
            generation = state.generation
        return self._resolve_service(plugin_id, generation, plugin_id, api_version)

    def _resolve_service(
        self,
        caller_plugin_id: str,
        caller_generation: int,
        provider_plugin_id: str,
        api_version: int,
    ) -> Any:
        with self._lock:
            caller = self._plugins.get(caller_plugin_id)
            provider = self._plugins.get(provider_plugin_id)
            if (
                self._closed
                or caller is None
                or caller.generation != caller_generation
                or provider is None
            ):
                raise PluginHostError("unavailable")
            registration = provider.services.get(api_version)
            if registration is None:
                raise PluginHostError("unavailable")
            dependencies: list[tuple[str, str, int]] = []
            for required_id, required_version in registration.required_plugins.items():
                dependency = self._plugins.get(required_id)
                if dependency is None or dependency.version != required_version:
                    raise PluginHostError("unavailable")
                dependencies.append((required_id, required_version, dependency.generation))
            instance = registration.instances.get(provider.generation)
            if instance is None:
                instance = registration.factory()
                registration.instances[provider.generation] = instance
            return _ServiceProxy(
                self,
                caller_plugin_id,
                caller_generation,
                provider_plugin_id,
                provider.generation,
                instance,
                tuple(dependencies),
            )

    def _state(self, plugin_id: str, generation: int) -> _PluginState:
        with self._lock:
            state = self._plugins.get(plugin_id)
            if self._closed or state is None or state.generation != generation:
                raise PluginHostError("unavailable")
            return state

    @contextmanager
    def _operation_lease(self, plugin_id: str, generation: int) -> Iterator[None]:
        """Hold lifecycle stable without retaining the short registry lock."""
        with self._operation_lock:
            self._state(plugin_id, generation)
            yield

    def _check_service_proxy(
        self,
        caller_plugin_id: str,
        caller_generation: int,
        provider_plugin_id: str,
        provider_generation: int,
        dependencies: tuple[tuple[str, str, int], ...],
    ) -> None:
        with self._lock:
            caller = self._plugins.get(caller_plugin_id)
            provider = self._plugins.get(provider_plugin_id)
            if (
                self._closed
                or caller is None
                or caller.generation != caller_generation
                or provider is None
                or provider.generation != provider_generation
            ):
                raise PluginHostError("unavailable")
            for dependency_id, version, dependency_generation in dependencies:
                dependency = self._plugins.get(dependency_id)
                if (
                    dependency is None
                    or dependency.version != version
                    or dependency.generation != dependency_generation
                ):
                    raise PluginHostError("unavailable")

    async def authorize_actor(
        self,
        actor: PluginActor,
        permission: ActionPermission = "authenticated",
    ) -> None:
        """Check caller scope and action ownership against services off-loop."""
        if actor.agent_id is None and actor.thread_id is None and permission == "authenticated":
            return

        def read_and_authorize() -> None:
            services = self._services_provider()
            if services is None:
                raise PluginHostError("unavailable")
            self._authorize_actor_sync(services, actor, permission)

        try:
            await asyncio.get_running_loop().run_in_executor(None, read_and_authorize)
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError("unavailable") from None

    async def authorize_persistent_actor(
        self,
        actor: PluginActor,
        permission: ActionPermission = "authenticated",
    ) -> PluginActor:
        def read_and_authorize() -> PluginActor:
            services = self._services_provider()
            if services is None:
                raise PluginHostError("unavailable")
            return self._authorize_persistent_actor_sync(services, actor, permission)

        try:
            return await asyncio.get_running_loop().run_in_executor(None, read_and_authorize)
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError("unavailable") from None

    async def authorize_execution(
        self,
        ticket: Any | None,
        *,
        agent_id: str,
        thread_id: str,
        execution_kind: str | None = None,
    ) -> dict[str, str]:
        from octop.infra.agents.plugins.execution import ExecutionTicket

        if ticket is not None and (
            not isinstance(ticket, ExecutionTicket)
            or ticket._host_key is not self._execution_ticket_key
            or ticket.agent_id != agent_id
            or ticket.thread_id != thread_id
        ):
            raise PluginHostError("denied")
        if (
            ticket is not None
            and execution_kind is not None
            and ticket.execution_kind != execution_kind
        ):
            raise PluginHostError("denied")
        services = self._services_provider()
        if services is None:
            raise PluginHostError("unavailable")

        def read_requirements() -> dict[str, str]:
            rows = services.plugin_record_repo.list_prefix("@host", _REQUIRED_NAMESPACE, "")
            return _merge_requirement_records(rows, agent_id, thread_id)

        try:
            declarations = await asyncio.get_running_loop().run_in_executor(None, read_requirements)
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError("unavailable") from None
        if not declarations:
            if ticket is not None:
                await self.authorize_persistent_actor(ticket.actor)
            return {}
        if ticket is None:
            raise PluginHostError("denied")
        actor = ticket.actor
        if actor.agent_id != agent_id or actor.thread_id != thread_id:
            raise PluginHostError("denied")
        await self.authorize_persistent_actor(actor)
        active = self.active_plugins
        if any(active.get(plugin_id) != version for plugin_id, version in declarations.items()):
            raise PluginHostError("unavailable")
        return declarations

    async def read_requirements(self, actor: PluginActor) -> PluginRequirementReadiness:
        """Read the persisted effective requirements for an authenticated Agent scope."""
        if not actor.agent_id or (actor.thread_id is not None and not actor.thread_id):
            raise PluginHostError("invalid")

        def read() -> PluginRequirementReadiness:
            services = self._services_provider()
            if services is None:
                raise PluginHostError("unavailable")
            refreshed = self._authorize_persistent_actor_sync(services, actor)
            agent_id = refreshed.agent_id
            if not agent_id:
                raise PluginHostError("invalid")
            agent = services.agent_repo.get(agent_id)
            if agent is None:
                raise PluginHostError("not_found")
            try:
                config = json.loads(agent.config_json) if agent.config_json else {}
            except (TypeError, ValueError):
                raise PluginHostError("unavailable") from None
            if not isinstance(config, dict):
                raise PluginHostError("unavailable")
            plugins_config = config.get("plugins", {})
            if not isinstance(plugins_config, dict):
                raise PluginHostError("unavailable")
            rows = services.plugin_record_repo.list_prefix("@host", _REQUIRED_NAMESPACE, "")
            required = _merge_requirement_records(rows, agent_id, refreshed.thread_id)
            active = self.active_plugins
            unavailable = tuple(
                sorted(
                    plugin_id
                    for plugin_id, version in required.items()
                    if active.get(plugin_id) != version
                    or (
                        isinstance(plugins_config.get(plugin_id), dict)
                        and plugins_config[plugin_id].get("enabled") is False
                    )
                )
            )
            return PluginRequirementReadiness(dict(sorted(required.items())), unavailable)

        try:
            return await asyncio.get_running_loop().run_in_executor(None, read)
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError("unavailable") from None

    async def authorize_peer_dispatch(
        self,
        ticket: Any | None,
        *,
        from_agent_id: str,
        to_agent_id: str,
        source_thread_id: str | None = None,
        target_thread_id: str | None = None,
    ) -> Any:
        """Authorize a team edge without transferring the parent's ticket."""
        from octop.infra.agents.plugins.execution import ExecutionTicket

        if ticket is not None and (
            not isinstance(ticket, ExecutionTicket)
            or ticket._host_key is not self._execution_ticket_key
            or ticket.agent_id != from_agent_id
            or (source_thread_id and ticket.thread_id != source_thread_id)
        ):
            raise PluginHostError("denied")
        if ticket is not None:
            await self.authorize_execution(
                ticket,
                agent_id=from_agent_id,
                thread_id=ticket.thread_id,
            )
        services = self._services_provider()
        if services is None:
            raise PluginHostError("unavailable")

        source_scope = source_thread_id or (ticket.thread_id if ticket is not None else None)

        def has_requirement(agent_id: str, thread_id: str | None = None) -> bool:
            rows = services.plugin_record_repo.list_prefix("@host", _REQUIRED_NAMESPACE, "")
            for row in rows:
                try:
                    payload = json.loads(row.payload_json)
                except (TypeError, ValueError):
                    raise PluginHostError("unavailable") from None
                if not isinstance(payload, dict):
                    raise PluginHostError("unavailable")
                required = payload.get("required_plugins")
                if not isinstance(required, dict):
                    raise PluginHostError("unavailable")
                payload_thread = payload.get("thread_id")
                if (
                    payload.get("agent_id") == agent_id
                    and required
                    and (thread_id is None or payload_thread is None or payload_thread == thread_id)
                ):
                    return True
            return False

        try:
            parent_bound, child_bound = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: (
                    has_requirement(from_agent_id, source_scope),
                    has_requirement(to_agent_id, target_thread_id),
                ),
            )
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError("unavailable") from None
        # A ticket proves the parent identity only. Team children do not inherit
        # it; any bound side requires a separately authenticated execution.
        if parent_bound or child_bound:
            raise PluginHostError("denied")
        from octop.infra.agents.plugins.execution import issue_peer_dispatch_permit

        return issue_peer_dispatch_permit(
            self,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            source_thread_id=source_thread_id,
        )

    async def begin_execution(
        self,
        ticket: Any | None,
        *,
        agent_id: str,
        thread_id: str,
        execution_kind: str | None = None,
    ) -> tuple[asyncio.Task[Any], tuple[_PluginState, ...]]:
        plugin_ids = await self.authorize_execution(
            ticket,
            agent_id=agent_id,
            thread_id=thread_id,
            execution_kind=execution_kind,
        )
        return self._track_authorized_execution(plugin_ids)

    def _track_authorized_execution(
        self, plugin_ids: Mapping[str, str], task: asyncio.Task[Any] | None = None
    ) -> tuple[asyncio.Task[Any], tuple[_PluginState, ...]]:
        """Track an already-authorized task while a caller holds the coordinator guard."""
        task = task or asyncio.current_task()
        if task is None:
            raise PluginHostError("unavailable")
        with self._lock:
            states: list[_PluginState] = []
            for plugin_id, version in plugin_ids.items():
                state = self._plugins.get(plugin_id)
                if state is None or state.version != version:
                    raise PluginHostError("unavailable")
                states.append(state)
            for state in states:
                _track_task_locked(state, task)
        return task, tuple(states)

    def end_execution(self, lease: tuple[asyncio.Task[Any], tuple[_PluginState, ...]]) -> None:
        task, states = lease
        with self._lock:
            for state in states:
                _untrack_task_locked(state, task)

    async def current_execution(self, plugin_id: str, *, optional: bool = False) -> Any:
        from octop.infra.agents.plugins.execution import copy_plugin_context, current_ticket
        from octop.infra.agents.plugins.host_contracts import PluginExecution

        ticket = current_ticket()
        if ticket is None:
            if optional:
                return None
            raise PluginHostError("denied")
        if ticket._host_key is not self._execution_ticket_key:
            raise PluginHostError("denied")
        required_plugins = await self.authorize_execution(
            ticket,
            agent_id=ticket.agent_id,
            thread_id=ticket.thread_id,
        )
        services = self._services_provider()
        if services is None:
            raise PluginHostError("unavailable")

        def refresh_actor() -> PluginActor:
            return self._authorize_persistent_actor_sync(services, ticket.actor)

        try:
            actor = await asyncio.get_running_loop().run_in_executor(None, refresh_actor)
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError("unavailable") from None
        return PluginExecution(
            actor,
            copy_plugin_context(ticket, plugin_id),
            ticket.execution_kind,
            required_plugins,
        )

    @staticmethod
    def _authorize_actor_sync(
        services: Any, actor: PluginActor, permission: ActionPermission
    ) -> None:
        is_admin = actor.is_admin
        if permission == "admin" and not is_admin:
            raise PluginHostError("denied")
        if permission == "agent_owner" and actor.agent_id is None:
            raise PluginHostError("denied")
        if actor.thread_id is not None and actor.agent_id is None:
            raise PluginHostError("invalid")
        if actor.agent_id is None and actor.thread_id is None:
            return
        if actor.agent_id is not None:
            agent = services.agent_repo.get(actor.agent_id) if actor.agent_id is not None else None
            if actor.agent_id is not None:
                if agent is None:
                    raise PluginHostError("not_found")
                owner = agent.user_id == actor.user_id
                if not is_admin and not owner and not bool(agent.is_shared):
                    raise PluginHostError("denied")
                if permission == "agent_owner" and not is_admin and not owner:
                    raise PluginHostError("denied")
        if actor.thread_id is not None:
            thread = services.thread_repo.get(actor.thread_id)
            if thread is None or thread.agent_id != actor.agent_id:
                raise PluginHostError("not_found")
            if thread.user_id != actor.user_id:
                raise PluginHostError("denied")

    @classmethod
    def _authorize_persistent_actor_sync(
        cls,
        services: Any,
        actor: PluginActor,
        permission: ActionPermission = "authenticated",
    ) -> PluginActor:
        user = services.user_repo.get(actor.user_id)
        if user is None or user.disabled:
            raise PluginHostError("denied")
        refreshed = PluginActor(
            user_id=user.id,
            is_admin=user.role == "admin",
            permissions=frozenset(user.permissions),
            agent_id=actor.agent_id,
            thread_id=actor.thread_id,
            operation_id=actor.operation_id,
            locale=user.locale,
        )
        cls._authorize_actor_sync(services, refreshed, permission)
        return refreshed

    @classmethod
    def _authorize_requirement_sync(
        cls,
        services: Any,
        actor: PluginActor,
        agent_id: str,
        thread_id: str | None,
    ) -> PluginActor:
        if actor.agent_id != agent_id or actor.thread_id != thread_id:
            raise PluginHostError("invalid")
        refreshed = cls._authorize_persistent_actor_sync(services, actor)
        agent = services.agent_repo.get(agent_id)
        if agent is None:
            raise PluginHostError("not_found")
        if not refreshed.is_admin and agent.user_id != refreshed.user_id:
            raise PluginHostError("denied")
        if not refreshed.is_admin and "agent_plugins" not in refreshed.permissions:
            raise PluginHostError("denied")
        return refreshed


class PluginHostFacade:
    """A plugin-scoped capability surface; no repo or database escapes it."""

    api_version = 1

    def __init__(self, host: PluginHost, plugin_id: str, generation: int) -> None:
        self._host = host
        self._plugin_id = plugin_id
        self._generation = generation

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    def _check(self) -> _PluginState:
        return self._host._state(self.plugin_id, self._generation)

    async def reauthorize_actor(
        self,
        actor: PluginActor,
        permission: ActionPermission = "authenticated",
    ) -> PluginActor:
        """Refresh a trusted actor's authority from persistent user and scope rows."""
        self._check()
        if not isinstance(actor, PluginActor) or not isinstance(permission, str):
            raise PluginHostError("invalid")
        if permission not in _PERMISSIONS:
            raise PluginHostError("invalid")
        try:
            services = self._require_services()
        except PluginHostError as exc:
            raise PluginHostError(exc.code) from None

        def read() -> PluginActor:
            try:
                return self._host._authorize_persistent_actor_sync(
                    services,
                    actor,
                    permission,
                )
            except PluginHostError:
                raise
            except Exception:
                raise PluginHostError("unavailable") from None

        refreshed = await self._run(read)
        self._check()
        return refreshed

    async def current_execution(self, *, optional: bool = False) -> Any:
        self._check()
        return await self._host.current_execution(self.plugin_id, optional=optional)

    def register_read_only_tools(self, tool_names: Sequence[str]) -> None:
        self._host._register_read_only_tools(self.plugin_id, self._generation, tool_names)

    def register_user_identity_consumer(self) -> None:
        self._host._register_user_identity_consumer(self.plugin_id, self._generation)

    def register_background_worker(
        self,
        name: str,
        worker: Callable[[asyncio.Event], Awaitable[None]],
    ) -> None:
        self._host._register_background_worker(self.plugin_id, self._generation, name, worker)

    async def read_user_identity(self, user_id: int) -> PluginUserIdentity:
        self._check()
        _validate_owner(user_id)
        state = self._check()
        if not state.identity_consumer_registered:
            raise PluginHostError("denied")
        services = self._require_services()

        def read() -> PluginUserIdentity | None:
            row = services.user_repo.get_identity(user_id)
            return _identity_from_row(row) if row is not None else None

        identity = await self._run(read)
        if identity is None:
            raise PluginHostError("not_found")
        return identity

    async def list_user_identities(
        self, *, after_user_id: int = 0, limit: int = 50
    ) -> PluginUserIdentityPage:
        self._check()
        if (
            isinstance(after_user_id, bool)
            or not isinstance(after_user_id, int)
            or after_user_id < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise PluginHostError("invalid")
        state = self._check()
        if not state.identity_consumer_registered:
            raise PluginHostError("denied")
        services = self._require_services()

        def read() -> list[PluginUserIdentity]:
            rows = services.user_repo.list_identity_page(after_user_id, limit + 1)
            return [_identity_from_row(row) for row in rows]

        identities = await self._run(read)
        has_more = len(identities) > limit
        items = tuple(identities[:limit])
        return PluginUserIdentityPage(items, items[-1].user_id if has_more else None)

    def register_service(
        self,
        api_version: int,
        factory: Callable[[], Any],
        required_plugins: Mapping[str, str],
    ) -> None:
        state = self._check()
        if api_version < 1 or not callable(factory):
            raise PluginHostError("invalid")
        required = dict(required_plugins)
        for plugin_id, version in required.items():
            _validate_plugin_id(plugin_id)
            if not version:
                raise PluginHostError("invalid")
        if api_version in state.services:
            raise PluginHostError("conflict")
        state.services[api_version] = _ServiceRegistration(factory, required)

    def resolve_service(self, provider_plugin_id: str, api_version: int) -> Any:
        """Resolve a versioned provider service bound to both plugin generations."""
        self._check()
        if isinstance(api_version, bool) or not isinstance(api_version, int) or api_version < 1:
            raise PluginHostError("invalid")
        _validate_plugin_id(provider_plugin_id)
        return self._host._resolve_service(
            self.plugin_id,
            self._generation,
            provider_plugin_id,
            api_version,
        )

    def register_action(
        self,
        name: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        handler: ActionHandler,
        summary: str,
        permission: ActionPermission,
    ) -> None:
        state = self._check()
        if (
            not _ACTION_NAME_RE.fullmatch(name)
            or any(part in {"", ".", ".."} for part in name.split("."))
            or not isinstance(input_model, type)
            or not isinstance(output_model, type)
            or not issubclass(input_model, BaseModel)
            or not issubclass(output_model, BaseModel)
            or not inspect.iscoroutinefunction(handler)
            or permission not in _PERMISSIONS
        ):
            raise PluginHostError("invalid")
        if name in state.actions:
            raise PluginHostError("conflict")
        state.actions[name] = _ActionRegistration(
            input_model=input_model,
            output_model=output_model,
            handler=handler,
            summary=summary,
            permission=permission,
        )

    def register_resource(self, handler: ResourceHandler) -> None:
        state = self._check()
        if not inspect.iscoroutinefunction(handler):
            raise PluginHostError("invalid")
        state.resources.append(handler)

    def descriptors(self) -> dict[str, Any]:
        state = self._check()
        return {
            "api_version": self.api_version,
            "actions": [
                {
                    "name": name,
                    "summary": entry.summary,
                    "permission": entry.permission,
                    "input_schema": entry.input_model.model_json_schema(),
                    "output_schema": entry.output_model.model_json_schema(),
                }
                for name, entry in sorted(state.actions.items())
            ],
            "resource_count": len(state.resources),
        }

    def action_permission(self, name: str) -> ActionPermission:
        state = self._check()
        action = state.actions.get(name)
        if action is None:
            raise PluginHostError("not_found")
        return action.permission

    async def invoke_action(
        self, name: str, actor: PluginActor, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        state = self._check()
        action = state.actions.get(name)
        if action is None:
            raise PluginHostError("not_found")
        await self._host.authorize_actor(actor, action.permission)
        try:
            parsed = action.input_model.model_validate(payload)
        except Exception:
            raise PluginHostError("invalid") from None
        try:
            result = await self._invoke_tracked(state, action.handler(actor, parsed))
        except (PluginHostError, PluginActionError):
            raise
        except Exception:
            raise PluginHostExecutionError() from None
        self._check()
        try:
            validated = action.output_model.model_validate(result)
            output = validated.model_dump(mode="json")
            if not isinstance(output, dict):
                raise ValueError
            return output
        except Exception:
            raise PluginHostExecutionError() from None

    async def _invoke_tracked(self, state: _PluginState, callback: Any) -> Any:
        task = asyncio.create_task(cast(Coroutine[Any, Any, Any], callback))
        with self._host._lock:
            if self._host._plugins.get(self.plugin_id) is not state:
                task.cancel()
                raise PluginHostError("unavailable")
            _track_task_locked(state, task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise PluginHostError("unavailable") from None
        finally:
            with self._host._lock:
                _untrack_task_locked(state, task)

    async def invoke_resource(self, actor: PluginActor, token: str) -> PluginResource:
        await self._host.authorize_actor(actor)
        state = self._check()
        if not state.resources:
            raise PluginHostError("not_found")
        try:
            result = await self._invoke_tracked(state, state.resources[0](actor, token))
        except (PluginHostError, PluginActionError):
            raise
        except Exception:
            raise PluginHostExecutionError() from None
        self._check()
        if not isinstance(result, PluginResource):
            raise PluginHostError("invalid")
        if not result.media_type or "\r" in result.media_type or "\n" in result.media_type:
            raise PluginHostError("invalid")
        if result.filename is not None and (
            not result.filename
            or len(result.filename) > 255
            or any(not (ch.isalnum() or ch in "._-") for ch in result.filename)
        ):
            raise PluginHostError("invalid")
        if not isinstance(result.body, bytes) and not hasattr(result.body, "__aiter__"):
            raise PluginHostError("invalid")
        return result

    async def stream_resource(self, resource: PluginResource) -> Any:
        if isinstance(resource.body, bytes):
            self._check()
            yield resource.body
            return
        task = asyncio.current_task()
        state = self._check()
        if task is None:
            raise PluginHostError("unavailable")
        with self._host._lock:
            if self._host._plugins.get(self.plugin_id) is not state:
                raise PluginHostError("unavailable")
            _track_task_locked(state, task)
        iterator = resource.body.__aiter__()
        try:
            async for chunk in iterator:
                self._check()
                if not isinstance(chunk, bytes):
                    raise PluginHostExecutionError() from None
                yield chunk
        except PluginHostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PluginHostExecutionError() from None
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()
            with self._host._lock:
                _untrack_task_locked(state, task)

    async def get_record(self, namespace: str, logical_key: str) -> HostRecord | None:
        self._check()
        _validate_namespace(namespace)
        if namespace == _REQUIRED_NAMESPACE:
            raise PluginHostError("denied")
        _validate_key(logical_key)
        services = self._require_services()
        row = await self._run(
            lambda: services.plugin_record_repo.get(self.plugin_id, namespace, logical_key)
        )
        return _row_to_record(row)

    async def list_records(self, namespace: str, prefix: str = "") -> list[HostRecord]:
        self._check()
        _validate_namespace(namespace)
        if namespace == _REQUIRED_NAMESPACE:
            raise PluginHostError("denied")
        if not isinstance(prefix, str):
            raise PluginHostError("invalid")
        services = self._require_services()
        rows = await self._run(
            lambda: services.plugin_record_repo.list_prefix(self.plugin_id, namespace, prefix)
        )
        return [record for row in rows if (record := _row_to_record(row)) is not None]

    async def get_retained_record(self, namespace: str, logical_key: str) -> RetainedRecord | None:
        self._check()
        _validate_namespace(namespace)
        if namespace == _REQUIRED_NAMESPACE:
            raise PluginHostError("denied")
        _validate_key(logical_key)
        _validate_retained_text(namespace, logical_key)
        services = self._require_services()

        def read() -> RetainedRecord | None:
            try:
                data = services.plugin_retained_record_repo.get(
                    self.plugin_id, namespace, logical_key
                )
            except (TypeError, ValueError):
                raise PluginHostError("unavailable") from None
            return None if data is None else _retained_data_to_record(data)

        return await self._run(read)

    async def list_retained_records(
        self,
        namespace: str,
        prefix: str = "",
        *,
        after_key: str | None = None,
        limit: int = 50,
    ) -> RetainedRecordPage:
        self._check()
        _validate_namespace(namespace)
        if namespace == _REQUIRED_NAMESPACE:
            raise PluginHostError("denied")
        if (
            not isinstance(prefix, str)
            or (after_key is not None and not isinstance(after_key, str))
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise PluginHostError("invalid")
        _validate_retained_text(namespace, prefix)
        if after_key is not None:
            _validate_key(after_key)
            _validate_retained_text(namespace, after_key)
        services = self._require_services()

        def read() -> RetainedRecordPage:
            try:
                page = services.plugin_retained_record_repo.list_prefix_page(
                    self.plugin_id,
                    namespace,
                    prefix,
                    after_key=after_key,
                    limit=limit,
                )
            except (TypeError, ValueError):
                raise PluginHostError("unavailable") from None
            return RetainedRecordPage(
                tuple(_retained_data_to_record(row) for row in page.items),
                page.next_logical_key,
            )

        return await self._run(read)

    async def stage_credential(
        self,
        owner_user_id: int,
        payload: Mapping[str, Any],
        *,
        credential_id: str | None = None,
    ) -> str:
        self._check()
        _validate_owner(owner_user_id)
        if credential_id is not None:
            _validate_preallocated_credential_id(credential_id)
        encoded = _json_object(payload)
        services = self._require_services()

        def stage() -> str:
            from octop.infra.connectors.crypto import encrypt_credentials

            repo = services.plugin_credential_repo
            if credential_id is not None and repo.credential_id_exists(credential_id):
                raise PluginHostError("conflict")
            ciphertext = encrypt_credentials(services.secret_repo, json.loads(encoded))
            try:
                row = repo.stage(
                    self.plugin_id,
                    owner_user_id,
                    ciphertext,
                    credential_id=credential_id,
                )
            except Exception:
                if credential_id is not None and repo.credential_id_exists(credential_id):
                    raise PluginHostError("conflict") from None
                raise
            return cast(str, row.id)

        credential_id = await self._run(stage)
        return credential_id

    async def resolve_credential(self, credential_id: str, owner_user_id: int) -> dict[str, Any]:
        self._check()
        _validate_owner(owner_user_id)
        if not credential_id:
            raise PluginHostError("not_found")
        services = self._require_services()

        def resolve() -> dict[str, Any] | None:
            row = services.plugin_credential_repo.get(self.plugin_id, credential_id, owner_user_id)
            if row is None or row.state != "active":
                return None
            from octop.infra.connectors.crypto import decrypt_credentials

            return decrypt_credentials(services.secret_repo, row.ciphertext)

        payload = await self._run(resolve)
        if payload is None:
            raise PluginHostError("not_found")
        return cast(dict[str, Any], json.loads(_json_object(payload)))

    async def release_staged_credential(self, credential_id: str, owner_user_id: int) -> bool:
        self._check()
        _validate_owner(owner_user_id)
        if not credential_id:
            return False
        services = self._require_services()

        def release() -> bool:
            repo = services.plugin_credential_repo
            with services.db.transaction() as conn:
                row = repo.get(self.plugin_id, credential_id, owner_user_id, conn=conn)
                if row is None or row.state != "staged":
                    return False
                return cast(
                    bool,
                    repo.delete(self.plugin_id, credential_id, owner_user_id, conn=conn),
                )

        removed = await self._run(release)
        return removed

    async def commit(
        self,
        writes: Sequence[RecordWrite],
        activations: Sequence[CredentialActivation] = (),
        *,
        requirements: Sequence[RequiredExtensionWrite] = (),
        checks: Sequence[RecordCheck] = (),
        identity_checks: Sequence[UserIdentityCheck] = (),
        credential_deletions: Sequence[CredentialDeletion] = (),
        retained_writes: Sequence[RetainedRecordWrite] = (),
        retained_checks: Sequence[RetainedRecordCheck] = (),
    ) -> list[HostRecord]:
        self._check()
        if any(write.namespace == _REQUIRED_NAMESPACE for write in writes):
            raise PluginHostError("denied")
        prepared = [(_validate_write(write), _json_object(write.payload)) for write in writes]
        retained = bool(retained_writes or retained_checks)
        try:
            retained_writes = tuple(retained_writes)
            retained_checks = tuple(retained_checks)
        except TypeError:
            raise PluginHostError("invalid") from None
        prepared_retained: list[tuple[RetainedRecordWrite, str]] = []
        retained_write_keys: set[tuple[str, str]] = set()
        for retained_write in retained_writes:
            if not isinstance(retained_write, RetainedRecordWrite):
                raise PluginHostError("invalid")
            _validate_namespace(retained_write.namespace)
            if retained_write.namespace == _REQUIRED_NAMESPACE:
                raise PluginHostError("denied")
            _validate_key(retained_write.logical_key)
            _validate_retained_text(retained_write.namespace, retained_write.logical_key)
            if (
                isinstance(retained_write.expected_revision, bool)
                or not isinstance(retained_write.expected_revision, int)
                or retained_write.expected_revision < 0
            ):
                raise PluginHostError("invalid")
            key = (retained_write.namespace, retained_write.logical_key)
            if key in retained_write_keys:
                raise PluginHostError("invalid")
            retained_write_keys.add(key)
            prepared_retained.append((retained_write, _json_object(retained_write.payload)))
        retained_checked_keys: dict[tuple[str, str], int] = {}
        for retained_check in retained_checks:
            if not isinstance(retained_check, RetainedRecordCheck):
                raise PluginHostError("invalid")
            _validate_namespace(retained_check.namespace)
            if retained_check.namespace == _REQUIRED_NAMESPACE:
                raise PluginHostError("denied")
            _validate_key(retained_check.logical_key)
            _validate_retained_text(retained_check.namespace, retained_check.logical_key)
            if (
                isinstance(retained_check.expected_revision, bool)
                or not isinstance(retained_check.expected_revision, int)
                or retained_check.expected_revision < 0
            ):
                raise PluginHostError("invalid")
            key = (retained_check.namespace, retained_check.logical_key)
            prior = retained_checked_keys.setdefault(key, retained_check.expected_revision)
            if prior != retained_check.expected_revision:
                raise PluginHostError("invalid")
        for retained_write, _ in prepared_retained:
            checked_revision = retained_checked_keys.get(
                (retained_write.namespace, retained_write.logical_key)
            )
            if (
                checked_revision is not None
                and checked_revision != retained_write.expected_revision
            ):
                raise PluginHostError("invalid")
        checked_keys: dict[tuple[str, str], int] = {}
        for check in checks:
            if not isinstance(check, RecordCheck):
                raise PluginHostError("invalid")
            _validate_namespace(check.namespace)
            _validate_key(check.logical_key)
            if (
                isinstance(check.expected_revision, bool)
                or not isinstance(check.expected_revision, int)
                or check.expected_revision < 0
            ):
                raise PluginHostError("invalid")
            key = (check.namespace, check.logical_key)
            prior = checked_keys.setdefault(key, check.expected_revision)
            if prior != check.expected_revision:
                raise PluginHostError("invalid")
        for activation in activations:
            _validate_owner(activation.owner_user_id)
            if (
                not isinstance(activation.credential_id, str)
                or not activation.credential_id
                or isinstance(activation.expected_revision, bool)
                or not isinstance(activation.expected_revision, int)
                or activation.expected_revision < 1
            ):
                raise PluginHostError("invalid")
        checked_identities: dict[int, PluginUserIdentity] = {}
        try:
            identity_checks = tuple(identity_checks)
            credential_deletions = tuple(credential_deletions)
        except TypeError:
            raise PluginHostError("invalid") from None
        if identity_checks:
            state = self._check()
            if not state.identity_consumer_registered:
                raise PluginHostError("denied")
        for identity_check in identity_checks:
            if not isinstance(identity_check, UserIdentityCheck) or not isinstance(
                identity_check.expected, PluginUserIdentity
            ):
                raise PluginHostError("invalid")
            expected = identity_check.expected
            _validate_owner(expected.user_id)
            if (
                (expected.email is not None and not isinstance(expected.email, str))
                or not isinstance(expected.disabled, bool)
                or not isinstance(expected.snapshot_token, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected.snapshot_token)
                or normalize_email(expected.email) != expected.email
                or _identity_snapshot_token(expected.user_id, expected.email, expected.disabled)
                != expected.snapshot_token
            ):
                raise PluginHostError("invalid")
            prior_identity = checked_identities.setdefault(expected.user_id, expected)
            if prior_identity != expected:
                raise PluginHostError("invalid")
        deletion_ids: set[str] = set()
        for deletion in credential_deletions:
            if not isinstance(deletion, CredentialDeletion):
                raise PluginHostError("invalid")
            _validate_owner(deletion.owner_user_id)
            if (
                not isinstance(deletion.credential_id, str)
                or not deletion.credential_id
                or isinstance(deletion.expected_revision, bool)
                or not isinstance(deletion.expected_revision, int)
                or deletion.expected_revision < 1
                or deletion.credential_id in deletion_ids
            ):
                raise PluginHostError("invalid")
            deletion_ids.add(deletion.credential_id)
        activation_ids = {activation.credential_id for activation in activations}
        if deletion_ids & activation_ids:
            raise PluginHostError("invalid")
        for requirement in requirements:
            if (
                isinstance(requirement.expected_revision, bool)
                or not isinstance(requirement.expected_revision, int)
                or requirement.expected_revision < 0
                or not requirement.agent_id
                or requirement.agent_id != requirement.actor.agent_id
            ):
                raise PluginHostError("invalid")
            if requirement.thread_id != requirement.actor.thread_id:
                raise PluginHostError("invalid")
            if requirement.required_plugins and self.plugin_id not in requirement.required_plugins:
                raise PluginHostError("invalid")
            for plugin_id, version in requirement.required_plugins.items():
                _validate_plugin_id(plugin_id)
                if not version or self._host.active_plugins.get(plugin_id) != version:
                    raise PluginHostError("unavailable")
        if retained:
            if requirements:
                raise PluginHostError("invalid")
            covered = set(checked_identities)
            ordinary_owners = {write.owner_user_id for write, _ in prepared}
            ordinary_owners.update(activation.owner_user_id for activation in activations)
            ordinary_owners.update(deletion.owner_user_id for deletion in credential_deletions)
            if not ordinary_owners.issubset(covered):
                raise PluginHostError("invalid")
        services = self._require_services()

        def commit_all() -> list[HostRecord]:
            records = services.plugin_record_repo
            credentials = services.plugin_credential_repo
            retained_records = services.plugin_retained_record_repo if retained else None
            result: list[HostRecord] = []
            with services.db.transaction() as conn:
                if checked_identities:
                    identity_rows = services.user_repo.lock_identity_rows(
                        conn, sorted(checked_identities)
                    )
                    current_identities = {
                        row.user_id: _identity_from_row(row) for row in identity_rows
                    }
                    if any(
                        current_identities.get(user_id) != expected
                        for user_id, expected in checked_identities.items()
                    ):
                        raise PluginHostError("conflict")
                if retained:
                    records.lock_for_checks(conn)
                    cast(Any, retained_records).lock_for_checks(conn)
                if checked_keys:
                    if not retained:
                        records.lock_for_checks(conn)
                    for (namespace, logical_key), expected_revision in checked_keys.items():
                        current = records.get(self.plugin_id, namespace, logical_key, conn=conn)
                        if (current.revision if current is not None else 0) != expected_revision:
                            raise PluginHostError("conflict")
                for (namespace, logical_key), expected_revision in retained_checked_keys.items():
                    current = cast(Any, retained_records).get(
                        self.plugin_id, namespace, logical_key, conn=conn
                    )
                    if (current.revision if current is not None else 0) != expected_revision:
                        raise PluginHostError("conflict")
                for requirement in requirements:
                    actor = self._host._authorize_requirement_sync(
                        services,
                        requirement.actor,
                        requirement.agent_id,
                        requirement.thread_id,
                    )
                    user = services.user_repo.get(actor.user_id)
                    if user is None:
                        raise PluginHostError("not_found")
                    if requirement.thread_id is not None:
                        thread = services.thread_repo.get(requirement.thread_id)
                        if thread is None or thread.agent_id != requirement.agent_id:
                            raise PluginHostError("not_found")
                        if thread.user_id != user.id:
                            raise PluginHostError("denied")
                    logical_key = _requirement_key(
                        requirement.agent_id,
                        requirement.thread_id,
                        self.plugin_id,
                    )
                    current = records.get("@host", _REQUIRED_NAMESPACE, logical_key, conn=conn)
                    if current is None:
                        if requirement.expected_revision != 0:
                            raise PluginHostError("conflict")
                        owner_user_id = user.id
                    else:
                        if current.revision != requirement.expected_revision:
                            raise PluginHostError("conflict")
                        owner_user_id = current.owner_user_id
                    payload = {
                        "actor_user_id": user.id,
                        "agent_id": requirement.agent_id,
                        "thread_id": requirement.thread_id,
                        "declaring_plugin_id": self.plugin_id,
                        "required_plugins": dict(requirement.required_plugins),
                        "enabled": bool(requirement.required_plugins),
                    }
                    row = records.cas(
                        "@host",
                        _REQUIRED_NAMESPACE,
                        logical_key,
                        owner_user_id=owner_user_id,
                        payload_json=_json_object(payload),
                        expected_revision=requirement.expected_revision,
                        conn=conn,
                    )
                    if row is None:
                        raise PluginHostError("conflict")
                for write, payload_json in prepared:
                    row = records.cas(
                        self.plugin_id,
                        write.namespace,
                        write.logical_key,
                        owner_user_id=write.owner_user_id,
                        payload_json=payload_json,
                        expected_revision=write.expected_revision,
                        conn=conn,
                    )
                    if row is None:
                        raise PluginHostError("conflict")
                    record = _row_to_record(row)
                    if record is None:
                        raise PluginHostError("unavailable")
                    result.append(record)
                for retained_write, payload_json in prepared_retained:
                    envelope = json.dumps(
                        {
                            "format_version": 1,
                            "revision": retained_write.expected_revision + 1,
                            "payload": json.loads(payload_json),
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    row = cast(Any, retained_records).cas(
                        self.plugin_id,
                        retained_write.namespace,
                        retained_write.logical_key,
                        payload_json=envelope,
                        expected_revision=retained_write.expected_revision,
                        conn=conn,
                    )
                    if row is None:
                        raise PluginHostError("conflict")
                for activation in activations:
                    row = credentials.activate(
                        self.plugin_id,
                        activation.credential_id,
                        activation.owner_user_id,
                        expected_revision=activation.expected_revision,
                        conn=conn,
                    )
                    if row is None:
                        raise PluginHostError("conflict")
                for deletion in credential_deletions:
                    removed = credentials.delete_active(
                        self.plugin_id,
                        deletion.credential_id,
                        deletion.owner_user_id,
                        expected_revision=deletion.expected_revision,
                        conn=conn,
                    )
                    if not removed:
                        raise PluginHostError("conflict")
            return result

        required_versions = {
            plugin_id: version
            for requirement in requirements
            for plugin_id, version in requirement.required_plugins.items()
        }
        return await self._run(commit_all, required_versions=required_versions)

    async def get_requirement(
        self,
        actor: PluginActor,
        agent_id: str,
        thread_id: str | None = None,
    ) -> RequiredExtensionState:
        self._check()
        if actor.agent_id != agent_id or actor.thread_id != thread_id:
            raise PluginHostError("invalid")
        services = self._require_services()
        key = _requirement_key(agent_id, thread_id, self.plugin_id)

        def read() -> RequiredExtensionState:
            self._host._authorize_requirement_sync(services, actor, agent_id, thread_id)
            row = services.plugin_record_repo.get("@host", _REQUIRED_NAMESPACE, key)
            if row is None:
                return RequiredExtensionState(0, {})
            payload = json.loads(_json_object(json.loads(row.payload_json)))
            required = payload.get("required_plugins")
            if not isinstance(required, dict):
                raise PluginHostError("unavailable")
            return RequiredExtensionState(row.revision, required)

        return await self._run(read)

    async def _run(
        self,
        operation: Callable[[], _T],
        *,
        required_versions: dict[str, str] | None = None,
    ) -> _T:
        self._check()
        loop = asyncio.get_running_loop()

        def run_with_lease() -> _T:
            with self._host._operation_lease(self.plugin_id, self._generation):
                if required_versions:
                    active = self._host.active_plugins
                    if any(
                        active.get(plugin_id) != version
                        for plugin_id, version in required_versions.items()
                    ):
                        raise PluginHostError("unavailable")
                return operation()

        try:
            result = cast(_T, await loop.run_in_executor(None, run_with_lease))
        except PluginHostError:
            raise
        except Exception as exc:
            raise PluginHostError("unavailable") from exc
        return result

    def _require_services(self) -> Any:
        self._check()
        try:
            services = self._host._services_provider()
        except Exception as exc:
            raise PluginHostError("unavailable") from exc
        if services is None:
            raise PluginHostError("unavailable")
        return services


class _ServiceProxy:
    def __init__(
        self,
        host: PluginHost,
        caller_plugin_id: str,
        caller_generation: int,
        provider_plugin_id: str,
        provider_generation: int,
        target: Any,
        dependencies: tuple[tuple[str, str, int], ...],
    ) -> None:
        self._host = host
        self._caller_plugin_id = caller_plugin_id
        self._caller_generation = caller_generation
        self._provider_plugin_id = provider_plugin_id
        self._provider_generation = provider_generation
        self._target = target
        self._dependencies = dependencies

    def _check(self) -> None:
        self._host._check_service_proxy(
            self._caller_plugin_id,
            self._caller_generation,
            self._provider_plugin_id,
            self._provider_generation,
            self._dependencies,
        )

    def __getattr__(self, name: str) -> Any:
        self._check()
        value = getattr(self._target, name)
        if not callable(value):
            self._check()
            return value

        if inspect.iscoroutinefunction(value):

            async def invoke_async(*args: Any, **kwargs: Any) -> Any:
                self._check()
                result = await value(*args, **kwargs)
                self._check()
                return result

            return invoke_async

        def invoke(*args: Any, **kwargs: Any) -> Any:
            self._check()
            result = value(*args, **kwargs)
            self._check()
            return result

        return invoke


def _validate_plugin_id(plugin_id: str) -> None:
    if not isinstance(plugin_id, str) or not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise PluginHostError("invalid")
    if plugin_id.startswith("@"):
        raise PluginHostError("denied")


def _validate_namespace(namespace: str) -> None:
    if (
        not isinstance(namespace, str)
        or not namespace.strip()
        or namespace != namespace.strip()
        or namespace.startswith("@")
        or "/" in namespace
        or "\\" in namespace
    ):
        raise PluginHostError(
            "denied" if isinstance(namespace, str) and namespace.startswith("@") else "invalid"
        )


def _validate_key(logical_key: str) -> None:
    if not isinstance(logical_key, str) or not logical_key or not logical_key.strip():
        raise PluginHostError("invalid")


def _validate_retained_text(namespace: str, logical_key: str) -> None:
    try:
        namespace.encode("utf-8")
        logical_key.encode("utf-8")
    except UnicodeEncodeError:
        raise PluginHostError("invalid") from None


def _validate_owner(owner_user_id: int) -> None:
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id <= 0:
        raise PluginHostError("invalid")


def _identity_snapshot_token(user_id: int, email: str | None, disabled: bool) -> str:
    canonical = json.dumps(
        [user_id, email, disabled], ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(
        ("octop.plugin-user-identity.v1:" + canonical).encode("utf-8")
    ).hexdigest()


def _identity_from_row(row: Any) -> PluginUserIdentity:
    email = normalize_email(row.email)
    disabled = bool(row.disabled)
    return PluginUserIdentity(
        user_id=row.user_id,
        email=email,
        disabled=disabled,
        snapshot_token=_identity_snapshot_token(row.user_id, email, disabled),
    )


def _validate_preallocated_credential_id(credential_id: str) -> None:
    if not isinstance(credential_id, str):
        raise PluginHostError("invalid")
    try:
        parsed = UUID(credential_id)
    except (ValueError, AttributeError):
        raise PluginHostError("invalid") from None
    if parsed.version != 4 or str(parsed) != credential_id:
        raise PluginHostError("invalid")


def _validate_write(write: RecordWrite) -> RecordWrite:
    _validate_namespace(write.namespace)
    _validate_key(write.logical_key)
    _validate_owner(write.owner_user_id)
    if (
        isinstance(write.expected_revision, bool)
        or not isinstance(write.expected_revision, int)
        or write.expected_revision < 0
    ):
        raise PluginHostError("invalid")
    return write


def _json_object(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise PluginHostError("invalid")
    try:
        result = json.dumps(dict(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PluginHostError("invalid") from exc
    return result


def _requirement_key(agent_id: str, thread_id: str | None, declaring_plugin_id: str) -> str:
    return json.dumps(
        [agent_id, thread_id, declaring_plugin_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _merge_requirement_records(
    rows: Sequence[Any], agent_id: str, thread_id: str | None
) -> dict[str, str]:
    """Validate canonical reserved records and merge the requested scope."""
    required_plugins: dict[str, str] = {}
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
            if not isinstance(payload, dict):
                raise ValueError
            record_agent = payload.get("agent_id")
            record_thread = payload.get("thread_id")
            declaring_plugin = payload.get("declaring_plugin_id")
            required = payload.get("required_plugins")
            if not isinstance(record_agent, str) or not record_agent:
                raise ValueError
            if record_thread is not None and (
                not isinstance(record_thread, str) or not record_thread
            ):
                raise ValueError
            if not isinstance(declaring_plugin, str):
                raise ValueError
            _validate_plugin_id(declaring_plugin)
            if row.logical_key != _requirement_key(record_agent, record_thread, declaring_plugin):
                raise ValueError
            if not isinstance(required, dict):
                raise ValueError
            for plugin_id, version in required.items():
                _validate_plugin_id(plugin_id)
                if not isinstance(version, str) or not version.strip():
                    raise ValueError
            if record_agent != agent_id or record_thread not in {None, thread_id}:
                continue
            for plugin_id, version in required.items():
                previous = required_plugins.get(plugin_id)
                if previous is not None and previous != version:
                    raise ValueError
                required_plugins[plugin_id] = version
        except PluginHostError:
            raise PluginHostError("unavailable") from None
        except (TypeError, ValueError, AttributeError):
            raise PluginHostError("unavailable") from None
    return dict(sorted(required_plugins.items()))


def _row_to_record(row: Any) -> HostRecord | None:
    if row is None:
        return None
    try:
        payload = json.loads(row.payload_json)
        if not isinstance(payload, dict):
            raise ValueError
        return HostRecord(
            id=row.id,
            namespace=row.namespace,
            logical_key=row.logical_key,
            owner_user_id=row.owner_user_id,
            revision=row.revision,
            payload=payload,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise PluginHostError("unavailable") from exc


def _retained_data_to_record(data: Any) -> RetainedRecord:
    try:
        payload_json = _json_object(data.payload)
        payload = json.loads(payload_json)
        return RetainedRecord(
            namespace=data.namespace,
            logical_key=data.logical_key,
            revision=data.revision,
            payload=payload,
        )
    except (TypeError, ValueError, AttributeError):
        raise PluginHostError("unavailable") from None
