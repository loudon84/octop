"""Turn-scoped policy consumer for enterprise knowledge retrieval."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import AgentState, ModelRequest, ModelResponse, OmitFromInput
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from octop_harness.middleware.tool_search import ToolSearchMiddleware

from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginHostError

from .bootstrap import BootstrapModel
from .i18n import tr

_REQUIRED = {"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"}
_SERVICE_ID = "enterprise-knowledge"
_USER_INPUT_KEY = "octop_user_input"
_SEARCH_NAME_RE = re.compile(r"^enterprise_knowledge_search(?:_[2-9]|_[1-9][0-9]*)?$")


class PolicyState(AgentState[Any], total=False):
    policy_operation_id: NotRequired[Annotated[str | None, OmitFromInput]]


def _messages(state: Any) -> list[Any]:
    if isinstance(state, Mapping):
        value = state.get("messages", [])
    else:
        value = getattr(state, "messages", [])
    return list(value or [])


def _human_input(state: Any) -> str | None:
    messages = _messages(state)
    if not messages or not isinstance(messages[-1], HumanMessage):
        return None
    marker = messages[-1].additional_kwargs.get(_USER_INPUT_KEY)
    if not isinstance(marker, dict) or set(marker) != {"version", "text"}:
        return None
    if type(marker.get("version")) is not int or marker["version"] != 1:
        return None
    text = marker.get("text")
    return text if isinstance(text, str) and text.strip() else None


def _current_turn_messages(state: Any) -> list[Any]:
    messages = _messages(state)
    start = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], HumanMessage)
        ),
        len(messages),
    )
    return messages[start:]


def _tool_error(state: Any) -> bool:
    return any(
        isinstance(message, ToolMessage) and message.status == "error"
        for message in _current_turn_messages(state)
    )


class EnterprisePolicyMiddleware(AgentMiddleware[Any, Any, Any]):
    state_schema = PolicyState

    def __init__(self) -> None:
        self._facade: Any | None = None

    def bind_facade(self, facade: Any) -> None:
        self._facade = facade

    async def _execution(self) -> Any:
        if self._facade is None:
            raise PluginHostError("unavailable")
        return await self._facade.current_execution(optional=True)

    @staticmethod
    def _relevant(execution: Any) -> bool:
        required = getattr(execution, "required_plugins", {})
        return bool({"enterprise-knowledge", "enterprise-policy"} & set(required))

    async def _service(self) -> Any:
        if self._facade is None:
            raise PluginHostError("unavailable")
        return self._facade.resolve_service(_SERVICE_ID, 1)

    async def _receipt_is_current(self, service: Any, receipt: Any) -> bool:
        if not isinstance(receipt, str):
            return False
        current = await service.search_current()
        return bool(current.operation.operation_id == receipt)

    def _locale(self, execution: Any) -> str:
        return str(execution.actor.locale)

    async def _authorized(self, execution: Any) -> bool:
        return (
            execution.execution_kind == "turn"
            and dict(execution.required_plugins) == _REQUIRED
            and bool(execution.actor.agent_id)
            and bool(execution.actor.thread_id)
        )

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        del runtime
        update: dict[str, Any] = {"policy_operation_id": None}
        try:
            execution = await self._execution()
        except (PluginHostError, PluginActionError):
            return {
                **update,
                "messages": [AIMessage(content=tr("errors.ACCESS_DENIED"))],
                "jump_to": "end",
            }
        if execution is None or not self._relevant(execution):
            return update
        service = await self._service()
        locale = self._locale(execution)
        if not await self._authorized(execution):
            message = tr("errors.PLUGIN_NOT_READY", locale)
            return {**update, "messages": [AIMessage(content=message)], "jump_to": "end"}
        question = _human_input(state)
        if question is None:
            await self._deny(service, "CONFIG_INVALID")
            return {
                **update,
                "messages": [AIMessage(content=tr("errors.CONFIG_INVALID", locale))],
                "jump_to": "end",
            }
        try:
            result = await service.retrieve_current(question)
        except PluginActionError as exc:
            code = (
                exc.code
                if exc.code in {"CONFIG_INVALID", "ACCESS_DENIED", "PLUGIN_NOT_READY"}
                else "UPSTREAM_UNAVAILABLE"
            )
            await self._deny(
                service,
                code
                if code in {"CONFIG_INVALID", "ACCESS_DENIED", "PLUGIN_NOT_READY"}
                else "PLUGIN_NOT_READY",
            )
            return {
                **update,
                "messages": [AIMessage(content=tr(f"errors.{code}", locale))],
                "jump_to": "end",
            }
        except PluginHostError:
            await self._deny(service, "PLUGIN_NOT_READY")
            return {
                **update,
                "messages": [AIMessage(content=tr("errors.PLUGIN_NOT_READY", locale))],
                "jump_to": "end",
            }
        if result.operation.status == "EMPTY":
            return {
                **update,
                "messages": [AIMessage(content=tr("empty", locale))],
                "jump_to": "end",
            }
        return {"policy_operation_id": result.operation.operation_id}

    async def _deny(self, service: Any, code: str) -> None:
        try:
            await service.deny_current(code)
        except (PluginActionError, PluginHostError):
            return

    async def _owned(self, tools: Sequence[Any]) -> tuple[dict[str, str], list[Any]]:
        service = await self._service()
        names: dict[str, str] = {}
        permitted: list[Any] = []
        for tool in tools:
            mapping = await service.owned_tools_current([tool])
            for role, name in mapping.items():
                if role in {"list", "search", "read"} and getattr(tool, "name", None) == name:
                    names[role] = name
                    permitted.append(tool)
        return names, permitted

    @staticmethod
    def _tool_search_metadata(tool: Any) -> bool:
        if getattr(tool, "name", None) != "tool_search":
            return False
        coroutine = getattr(tool, "coroutine", None)
        if coroutine is not None:
            return (
                isinstance(getattr(coroutine, "__self__", None), ToolSearchMiddleware)
                and getattr(coroutine, "__func__", None) is ToolSearchMiddleware._atool_search
            )
        function = getattr(tool, "func", None)
        return (
            isinstance(getattr(function, "__self__", None), ToolSearchMiddleware)
            and getattr(function, "__func__", None) is ToolSearchMiddleware._tool_search
        )

    async def _permitted_tools(self, tools: Sequence[Any]) -> tuple[dict[str, str], list[Any]]:
        role_names, owned = await self._owned(tools)
        metadata = [tool for tool in tools if self._tool_search_metadata(tool)]
        return role_names, [*owned, *metadata]

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> Any:
        try:
            execution = await self._execution()
        except (PluginHostError, PluginActionError):
            return AIMessage(content=tr("errors.ACCESS_DENIED"))
        if execution is None or not self._relevant(execution):
            return await handler(request)
        locale = self._locale(execution)
        if not await self._authorized(execution):
            return AIMessage(content=tr("errors.PLUGIN_NOT_READY", locale))
        service = await self._service()
        receipt = request.state.get("policy_operation_id") if request.state else None
        if not isinstance(receipt, str):
            return AIMessage(content=tr("errors.ACCESS_DENIED", locale))
        try:
            if not await self._receipt_is_current(service, receipt):
                await service.fail_current(receipt, "ACCESS_DENIED")
                return AIMessage(content=tr("errors.ACCESS_DENIED", locale))
        except (PluginActionError, PluginHostError):
            with suppress(PluginActionError, PluginHostError):
                await service.fail_current(receipt, "READ_SOURCE_CHANGED")
            return AIMessage(content=tr("errors.READ_SOURCE_CHANGED", locale))
        if _tool_error(request.state):
            await service.fail_current(receipt, "UPSTREAM_UNAVAILABLE")
            return AIMessage(content=tr("errors.UPSTREAM_UNAVAILABLE", locale))
        role_names, permitted = await self._permitted_tools(request.tools or [])
        search_name = role_names.get("search")
        if not search_name:
            await service.fail_current(receipt, "ACCESS_DENIED")
            return AIMessage(content=tr("errors.ACCESS_DENIED", locale))
        current = _current_turn_messages(request.state)
        has_search = any(
            isinstance(message, ToolMessage)
            and message.name == search_name
            and message.status == "success"
            for message in current
        )
        if not has_search:
            metadata_names = [tool.name for tool in permitted if self._tool_search_metadata(tool)]
            loaded_tool_names = request.state.get("loaded_tool_names", [])
            loaded_names = (
                {name for name in loaded_tool_names if isinstance(name, str)}
                if isinstance(loaded_tool_names, (list, tuple))
                else set()
            )
            metadata_completed = any(
                isinstance(message, ToolMessage)
                and message.name == "tool_search"
                and message.status == "success"
                for message in current
            )
            if metadata_completed and search_name not in loaded_names:
                await service.fail_current(receipt, "UPSTREAM_SCHEMA_INVALID")
                return AIMessage(content=tr("errors.UPSTREAM_SCHEMA_INVALID", locale))
            await service.search_current()
            return await handler(
                request.override(
                    model=BootstrapModel(
                        operation_id=receipt,
                        search_name=search_name,
                        discovery_name=metadata_names[0] if metadata_names else None,
                    ),
                    tools=permitted,
                )
            )
        return await handler(request.override(tools=permitted))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            execution = await self._execution()
        except (PluginHostError, PluginActionError):
            return ToolMessage(
                content=tr("errors.ACCESS_DENIED"),
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
        if execution is None or not self._relevant(execution):
            return await handler(request)
        locale = self._locale(execution)
        if not await self._authorized(execution):
            return ToolMessage(
                content=tr("errors.PLUGIN_NOT_READY", locale),
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
        try:
            service = await self._service()
            receipt = request.state.get("policy_operation_id") if request.state else None
            if not await self._receipt_is_current(service, receipt):
                return ToolMessage(
                    content=tr("errors.ACCESS_DENIED", locale),
                    tool_call_id=request.tool_call.get("id", ""),
                    status="error",
                )
        except (PluginActionError, PluginHostError):
            return ToolMessage(
                content=tr("errors.READ_SOURCE_CHANGED", locale),
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
        tool = request.tool
        name = getattr(tool, "name", None)
        call_name = request.tool_call.get("name")
        if tool is not None and isinstance(name, str) and name == call_name:
            try:
                if self._tool_search_metadata(tool):
                    return await handler(request)
                mapping = await service.owned_tools_current([tool])
                if name in mapping.values():
                    return await handler(request)
            except (PluginActionError, PluginHostError):
                pass
        return ToolMessage(
            content=tr("errors.ACCESS_DENIED", locale),
            tool_call_id=request.tool_call.get("id", ""),
            status="error",
            name=name,
        )

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        del runtime
        receipt = state.get("policy_operation_id") if isinstance(state, Mapping) else None
        if not isinstance(receipt, str):
            return {"policy_operation_id": None}
        try:
            execution = await self._execution()
            if (
                execution is None
                or not self._relevant(execution)
                or not await self._authorized(execution)
            ):
                return {"policy_operation_id": None}
            service = await self._service()
            search = await service.search_current()
            if (
                search.operation.operation_id == receipt
                and not _tool_error(state)
                and any(
                    isinstance(message, ToolMessage)
                    and message.status == "success"
                    and isinstance(message.name, str)
                    and _SEARCH_NAME_RE.fullmatch(message.name)
                    for message in _current_turn_messages(state)
                )
            ):
                await service.complete_current()
        except (PluginActionError, PluginHostError):
            with suppress(PluginActionError, PluginHostError):
                await (await self._service()).fail_current(receipt, "READ_SOURCE_CHANGED")
        return {"policy_operation_id": None}
