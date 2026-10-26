from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langchain.agents.middleware import before_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from octop_harness import HarnessAgentConfig
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.manager import HarnessAgentManager
from octop_harness.plugins import LoadedPlugin, PluginManifest, PluginRegistry
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.registry import MiddlewareRegistration

from octop.infra.agents.manager import AgentManager
from octop.infra.gateway.process import stream_project
from octop.infra.gateway.process.stream_project import _project_chunks


@pytest.fixture(autouse=True)
def _clean_plugin_registry():
    registry = PluginRegistry()
    registry.clear()
    yield
    registry.clear()


def _append_synthetic_message(
    state: dict[str, object], request_text: str
) -> dict[str, object] | None:
    messages = state.get("messages", [])
    if not messages or getattr(messages[-1], "content", "") != request_text:
        return None
    answer = f"{request_text} answer"
    return {"messages": [AIMessage(content=answer)], "jump_to": "end"}


def _sync_synthetic_before_agent(
    state: dict[str, object], _runtime: object
) -> dict[str, object] | None:
    return _append_synthetic_message(state, "synthetic-sync")


async def _async_synthetic_before_agent(
    state: dict[str, object], _runtime: object
) -> dict[str, object] | None:
    return _append_synthetic_message(state, "synthetic-async")


def _unrelated_custom_before_agent(state: dict[str, object], _runtime: object) -> None:
    messages = state.get("messages", [])
    if not messages or getattr(messages[-1], "content", "") != "unrelated-custom":
        return
    writer = get_stream_writer()
    writer(
        {
            "kind": "octop.assistant_text.v1",
            "api_version": 1,
            "event_id": str(uuid4()),
            "content": "must remain custom",
        }
    )
    writer({"kind": "unrelated", "detail": "preserve"})


def _stream_manager(
    workspace: Path, *, hook: Any
) -> tuple[AgentManager, HarnessAgentManager, FakeMessagesListChatModel]:
    providers = [
        ProviderConfig(
            id="test-provider",
            base_url="http://localhost",
            api_key="unused",
            models=[ModelConfig(id="test-model")],
        )
    ]
    sdk = HarnessAgentManager(providers=providers, file_logging=False)
    factory = sdk._shared_factory
    assert factory is not None

    class NoNetworkModel(FakeMessagesListChatModel):
        model_calls: int = 0

        def bind_tools(self, _tools: object, **_kwargs: object) -> NoNetworkModel:
            return self

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            self.model_calls += 1
            return super()._generate(*args, **kwargs)

    model = NoNetworkModel(responses=[AIMessage(content="ordinary answer")])
    factory.get_chat_model = lambda _ref: model

    manifest = PluginManifest(
        id="stream-text-test",
        version="1.0.0",
        name="Stream text test",
        kind="hook",
        entry="plugin.py",
    )
    context = PluginContext(manifest=manifest, source_path=Path("plugin"))
    PluginRegistry().register(
        LoadedPlugin(
            manifest=manifest,
            source_path=Path("plugin"),
            middleware=[MiddlewareRegistration("stream-text-test", hook)],
            context=context,
        )
    )

    @tool("查询工具")
    async def plugin_tool() -> str:
        """Return an unused plugin tool result."""
        return "unused"

    tools = [plugin_tool]
    from octop.infra.agents.plugins.plugin_tool_names import sanitize_plugin_tool_names

    sanitize_plugin_tool_names(tools, reserved={"tool_search"})
    assert tools[0].name.isascii()

    config = replace(
        HarnessAgentConfig(),
        workspace_dir=workspace,
        providers=providers,
        default_model="test-provider/test-model",
        memory=None,
        todos_enabled=False,
        model_retry_enabled=False,
        memory_enabled=False,
        session_log_enabled=False,
        checkpointer=InMemorySaver(),
        middleware=PluginRegistry().build_middleware_chain(),
        tools=tools,
    )
    sdk.create_agent(config, agent_id="agent", init_workspace=False)

    manager = object.__new__(AgentManager)
    manager._harness_manager = sdk
    manager._plugin_host = None
    manager._repos = SimpleNamespace(
        agent_repo=SimpleNamespace(
            get=lambda _agent_id: SimpleNamespace(config_json="{}", skill_package_ids=None)
        )
    )
    manager._thread_execution_locks = {}
    manager._active_invocations = {}
    manager._invocation_waiters = {}
    manager._history_backfills = {}
    manager._reclaim_holds = {}
    manager._reclaim_pending = set()
    manager._bootstrap_graph_refresh_pending = set()
    return manager, sdk, model


async def _gateway_events(manager: AgentManager, chunks: list[dict[str, Any]]) -> list[Any]:
    async def source() -> AsyncIterator[dict[str, Any]]:
        for chunk in chunks:
            yield chunk

    return [
        event
        async for event in _project_chunks(
            source(),
            agent_manager=manager,
            agent_id="agent",
            locale="en",
            usage_tracker=None,
            history_tracker=None,
            projection_state=None,
            hitl_coordinator=None,
            hitl_ctx=None,
        )
    ]


@pytest.mark.parametrize(
    ("hook", "request_text", "answer"),
    [
        (
            before_agent(can_jump_to=["end"])(_sync_synthetic_before_agent),
            "synthetic-sync",
            "synthetic-sync answer",
        ),
        (
            before_agent(can_jump_to=["end"])(_async_synthetic_before_agent),
            "synthetic-async",
            "synthetic-async answer",
        ),
    ],
)
@pytest.mark.asyncio
async def test_public_stream_projects_native_synthetic_state_once_with_matching_call_and_history(
    hook: Any,
    request_text: str,
    answer: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_project, "harness_workspace_for_agent", lambda *_args: None)
    manager, sdk, model = _stream_manager(tmp_path / "agent", hook=hook)
    try:
        chunks = [
            chunk
            async for chunk in manager.stream(
                "agent",
                {
                    "thread_id": "synthetic-stream",
                    "messages": [{"role": "user", "content": request_text}],
                },
            )
        ]
        events = await _gateway_events(manager, chunks)
        stream_history = await sdk.get_history("agent", "synthetic-stream")
        call_result = await manager.call(
            "agent",
            {
                "thread_id": "synthetic-call",
                "messages": [{"role": "user", "content": request_text}],
            },
        )
        call_history = await sdk.get_history("agent", "synthetic-call")

        assert model.model_calls == 0
        assert [chunk.get("content") for chunk in chunks if chunk["type"] == "token"] == [answer]
        assert not [chunk for chunk in chunks if chunk["type"] == "custom"]
        assert [
            "".join(block.text for block in event.content if hasattr(block, "text"))
            for event in events
            if event.type == "delta"
        ] == [answer]
        assert [
            message.content for message in stream_history if isinstance(message, AIMessage)
        ] == [answer]
        assert [
            message.content for message in call_result["messages"] if isinstance(message, AIMessage)
        ] == [answer]
        assert [message.content for message in call_history if isinstance(message, AIMessage)] == [
            answer
        ]
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_public_stream_preserves_ordinary_tokens_and_unrelated_custom_frames(
    tmp_path: Path,
) -> None:
    hook = before_agent(_unrelated_custom_before_agent)
    manager, sdk, model = _stream_manager(tmp_path / "agent", hook=hook)
    try:
        chunks = [
            chunk
            async for chunk in manager.stream(
                "agent",
                {
                    "thread_id": "ordinary",
                    "messages": [{"role": "user", "content": "unrelated-custom"}],
                },
            )
        ]

        custom_frames = [chunk for chunk in chunks if chunk["type"] == "custom"]
        assert model.model_calls == 1
        assert "ordinary answer" in "".join(
            chunk.get("content", "") for chunk in chunks if chunk["type"] == "token"
        )
        assert [chunk["data"] for chunk in custom_frames] == [
            {
                "kind": "octop.assistant_text.v1",
                "api_version": 1,
                "event_id": custom_frames[0]["data"]["event_id"],
                "content": "must remain custom",
            },
            {"kind": "unrelated", "detail": "preserve"},
        ]
    finally:
        sdk.close()
