"""Deterministic local model used to drive native discovery and cached search."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class BootstrapModel(BaseChatModel):
    """Emit a real native cached-search call against the currently bound tools."""

    operation_id: str
    search_name: str
    discovery_name: str | None = None
    needs_discovery: bool = False
    bound_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "enterprise-policy-bootstrap"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BootstrapModel:
        del kwargs
        bound_names = [getattr(tool, "name", "") for tool in tools]
        search_tool = next(
            (tool for tool in tools if getattr(tool, "name", None) == self.search_name), None
        )
        search_schema = getattr(search_tool, "args", None)
        search_schema_is_full = isinstance(search_schema, dict) and bool(search_schema)
        discovery_is_bound = self.discovery_name in bound_names
        return self.model_copy(
            update={
                "bound_names": bound_names,
                "needs_discovery": bool(discovery_is_bound and not search_schema_is_full),
            }
        )

    def _response(self) -> AIMessage:
        if self.discovery_name is not None and self.needs_discovery:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.discovery_name,
                        "args": {"query": self.search_name, "max_results": 1},
                        "id": f"policy_discover_{self.operation_id.replace('-', '')[:20]}",
                        "type": "tool_call",
                    }
                ],
            )
        name = self.search_name if self.search_name in self.bound_names else "tool_search"
        call_id = f"policy_{self.operation_id.replace('-', '')[:24]}"
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}],
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return ChatResult(generations=[ChatGeneration(message=self._response())])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        call = self._response().tool_calls[0]
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": call["name"],
                        "args": json.dumps(call["args"], separators=(",", ":")),
                        "id": call["id"],
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            )
        )
