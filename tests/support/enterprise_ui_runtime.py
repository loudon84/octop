"""Start an isolated, real Octop installation for enterprise UI acceptance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import io
import json
import os
import signal
import sys
from collections import deque
from contextlib import redirect_stdout, suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
from langchain_core.language_models.fake_chat_models import FakeListChatModel

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = REPO_ROOT / "plugins" / "enterprise-knowledge"
POLICY = REPO_ROOT / "plugins" / "enterprise-policy"
ADMIN_PASSWORD = "EnterpriseAdmin26"
EMPLOYEE_PASSWORD = "EnterpriseEmployee26"
SYNTHETIC_KEY = "synthetic-weknora-key-only"
SYNTHETIC_SEARCH_KEY = "synthetic-firecrawl-key-only"
PUBLIC_QUERY = "synthetic public acceptance query"
SOURCE_TEST_QUERY = "synthetic source validation query"
CANARY = "SYNTHETIC_PRIVATE_CANARY_DO_NOT_LOG"
SYNTHETIC_ANSWER = "Synthetic acceptance answer"
MAX_REQUEST_RECORDS = 256
ORIGINS = {
    ("https", "weknora.synthetic.test", 443): "weknora",
    ("https", "searxng.synthetic.test", 443): "searxng",
    ("https", "firecrawl.synthetic.test", 443): "firecrawl",
}


class AcceptanceChatModel(FakeListChatModel):
    """Deterministic model that creates a fresh message for every invocation."""

    bound_tools: list[str] = []
    bind_calls: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> AcceptanceChatModel:
        del kwargs
        self.bind_calls += 1
        self.bound_tools = [getattr(tool, "name", "") for tool in tools]
        return self


def acceptance_model_factory(model: AcceptanceChatModel) -> Any:
    """Build the local factory shim used by agent and memory model boundaries."""

    def get_chat_model(_ref: Any, **_kwargs: Any) -> AcceptanceChatModel:
        return model

    return get_chat_model


def prepare_empty_home(home: Path) -> Path:
    """Create a new runtime home, refusing to reuse any existing contents."""
    home = home.expanduser().resolve()
    if home.exists() and any(home.iterdir()):
        raise ValueError(f"--home must be an empty directory: {home}")
    home.mkdir(parents=True, exist_ok=True)
    return home


def require_loopback_host(host: str) -> str:
    if host != "127.0.0.1":
        raise ValueError("acceptance runtime must bind only to 127.0.0.1")
    return host


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def installed_artifact_hashes(source: Path, installed: Path) -> dict[str, str]:
    """Verify the declared plugin runtime files and return safe digest metadata."""
    import yaml

    plugin_manifest = yaml.safe_load((source / "plugin.yaml").read_text(encoding="utf-8"))
    expected_kind = "tool" if plugin_manifest["id"] == "enterprise-knowledge" else "hook"
    if (
        plugin_manifest.get("id") != source.name
        or plugin_manifest.get("kind") != expected_kind
        or not isinstance(plugin_manifest.get("version"), str)
        or not plugin_manifest["version"]
    ):
        raise ValueError("enterprise plugin manifest has unexpected id, kind or version")
    paths = ["plugin.yaml", str(plugin_manifest["entry"])]
    if plugin_manifest["id"] == "enterprise-knowledge":
        ui = plugin_manifest.get("ui")
        if not isinstance(ui, dict) or not isinstance(ui.get("entry"), str):
            raise ValueError("enterprise-knowledge plugin manifest must declare a UI entry")
        entry_path = ui["entry"]
        ui_manifest_path = source / str(ui.get("manifest", ""))
        if not ui_manifest_path.is_file():
            raise FileNotFoundError("enterprise-knowledge UI dist/manifest.json is required")
        ui_manifest = json.loads(ui_manifest_path.read_text(encoding="utf-8"))
        paths.extend((entry_path, str(ui.get("manifest"))))
        # The current manifest declares a single bundled entry. Validate its shape
        # as well, so a missing distribution cannot silently pass as tool fallback.
        if not isinstance(ui_manifest.get("renderers"), list):
            raise ValueError("enterprise-knowledge UI manifest has no renderer list")
    elif plugin_manifest["id"] != "enterprise-policy":
        raise ValueError("unexpected enterprise plugin manifest")

    digests: dict[str, str] = {}
    for relative in dict.fromkeys(paths):
        source_file = source / relative
        installed_file = installed / relative
        if not source_file.is_file() or not installed_file.is_file():
            raise FileNotFoundError(f"required plugin artifact missing: {relative}")
        source_hash = sha256_file(source_file)
        installed_hash = sha256_file(installed_file)
        if source_hash != installed_hash:
            raise ValueError(f"installed plugin artifact differs from source: {relative}")
        digests[relative] = source_hash
    return digests


class SyntheticUpstreams:
    """Contract-shaped upstream fixtures; only count safe request metadata."""

    def __init__(self) -> None:
        self.request_count = 0
        self.request_records: deque[dict[str, Any]] = deque(maxlen=MAX_REQUEST_RECORDS)
        self.canary_requests = {"knowledge": 0, "public": 0, "unknown": 0}
        self.path_counts: dict[str, int] = {}
        self.knowledge_ids = {"kb-private-a": "doc-private-a", "kb-private-b": "doc-private-b"}
        self.chunk_text = ("Synthetic authorized chunk. " * 75).strip()
        self.state_path: Path | None = None
        self.state_metadata: dict[str, Any] = {"status": "starting"}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._handle(request)
        finally:
            if self.state_path is not None:
                self.state_path.write_text(
                    json.dumps(
                        {"ready": self.state_metadata, "upstreams": self.summary()}, indent=2
                    ),
                    encoding="utf-8",
                )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        port = request.url.port
        if port is None:
            port = {"http": 80, "https": 443}.get(request.url.scheme)
        provider = (
            ORIGINS.get((request.url.scheme, request.url.host, port)) if port is not None else None
        )
        path = request.url.path
        body = await request.aread()
        contains_canary = CANARY.encode() in body
        source = "knowledge" if provider == "weknora" else "public"
        if provider is None:
            source = "unknown"
        normalized_path = self._normalized_path(provider, path)
        method = request.method if request.method in {"GET", "POST"} else "OTHER"
        self.request_count += 1
        self.path_counts[f"{provider or 'unknown'}:{method}:{normalized_path}"] = (
            self.path_counts.get(f"{provider or 'unknown'}:{method}:{normalized_path}", 0) + 1
        )
        if contains_canary:
            self.canary_requests[source] += 1
        self.request_records.append(
            {
                "sequence": self.request_count,
                "provider": provider or "unknown",
                "method": method,
                "path": normalized_path,
                "contains_canary": contains_canary,
            }
        )
        if provider == "weknora" and method == "GET" and path == "/api/v1/knowledge-bases":
            rows = [
                {"id": kb_id, "name": f"Acceptance {kb_id}", "knowledge_count": 1, "chunk_count": 4}
                for kb_id in self.knowledge_ids
            ]
            return httpx.Response(200, json={"success": True, "data": rows})
        if provider == "weknora" and method == "POST" and path == "/api/v1/knowledge-search":
            parsed = json.loads(body or b"{}")
            kb_ids = parsed.get("knowledge_base_ids", [])
            kb_id = kb_ids[0] if kb_ids else ""
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "knowledge_id": self.knowledge_ids.get(kb_id, "doc-private-a"),
                            "knowledge_base_id": kb_id,
                            "knowledge_title": f"Acceptance document {kb_id}",
                            "content": self.chunk_text,
                        }
                    ],
                },
            )
        if provider == "firecrawl" and method == "POST" and path == "/v2/search":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "web": [
                            {
                                "title": "Synthetic fallback result",
                                "url": "https://public.synthetic.test/result",
                                "description": "Synthetic result from the secondary source.",
                            }
                        ]
                    },
                },
            )
        if provider == "searxng" and method == "POST" and path == "/search":
            query = parse_qs(body.decode("ascii", errors="ignore")).get("q", [""])[0]
            if query == PUBLIC_QUERY:
                return httpx.Response(503, json={"error": "synthetic primary outage"})
            return httpx.Response(200, json={"results": []})
        if provider == "weknora":
            for kb_id, knowledge_id in self.knowledge_ids.items():
                if method == "GET" and path == f"/api/v1/knowledge/{knowledge_id}":
                    return httpx.Response(
                        200,
                        json={
                            "success": True,
                            "data": {
                                "id": knowledge_id,
                                "knowledge_base_id": kb_id,
                                "title": f"Acceptance document {kb_id}",
                            },
                        },
                    )
                if method == "GET" and path == f"/api/v1/chunks/{knowledge_id}":
                    page = int(request.url.params.get("page", "1"))
                    page_size = int(request.url.params.get("page_size", "10"))
                    start = (page - 1) * page_size
                    rows = [
                        {
                            "id": f"{knowledge_id}-chunk-{index}",
                            "knowledge_id": knowledge_id,
                            "knowledge_base_id": kb_id,
                            "chunk_index": index,
                            "content": f"Chunk {index}. {self.chunk_text}",
                            "chunk_type": "text",
                        }
                        for index in range(start, min(start + page_size, 24))
                    ]
                    return httpx.Response(
                        200,
                        json={
                            "success": True,
                            "data": rows,
                            "total": 24,
                            "page": page,
                            "page_size": page_size,
                        },
                    )
        return httpx.Response(404, json={"success": False, "data": None})

    def _normalized_path(self, provider: str | None, path: str) -> str:
        if provider == "weknora":
            if path in {"/api/v1/knowledge-bases", "/api/v1/knowledge-search"}:
                return path
            if any(
                path == f"/api/v1/{prefix}/{item}"
                for prefix in ("knowledge", "chunks")
                for item in self.knowledge_ids.values()
            ):
                prefix = "knowledge" if "/knowledge/" in path else "chunks"
                return f"/api/v1/{prefix}/{{id}}"
        elif (
            provider == "searxng"
            and path == "/search"
            or provider == "firecrawl"
            and path == "/v2/search"
        ):
            return path
        return "/<unrecognized>"

    def summary(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "path_counts": dict(sorted(self.path_counts.items())),
            "recent_requests": list(self.request_records),
            "canary_request_counts": dict(self.canary_requests),
            "record_limit": MAX_REQUEST_RECORDS,
            "egress_blocked": True,
        }


async def _post_action(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    action: str,
    payload: dict[str, Any],
    *,
    context: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/plugins/enterprise-knowledge/actions/{action}",
        headers=auth,
        json={"api_version": 1, "context": context, "payload": payload},
    )
    response.raise_for_status()
    return cast(dict[str, Any], response.json()["result"])


async def _verify_real_graph_turn(
    client: httpx.AsyncClient,
    *,
    agent_id: str,
    thread_id: str,
    connection_id: str,
    connection_revision: int,
    grant_revision: int,
    binding_revision: int,
    search_revision: int,
    employee_password: str,
    model: Any,
    port: int,
    server: Any,
) -> dict[str, Any]:
    auth_helpers = importlib.import_module("tests.support.auth")
    employee_auth = await auth_helpers.auth_header(
        client, username="acceptance-employee", password=employee_password
    )
    context = {"agent_id": agent_id, "thread_id": thread_id}
    access_token = await auth_helpers.login(
        client, username="acceptance-employee", password=employee_password
    )

    import websockets

    url = f"ws://127.0.0.1:{port}/api/agents/{agent_id}/chat/ws?token={access_token}"
    facade = server.plugin_host.facade("enterprise-knowledge")

    async def send_turn(
        kb_id: str, expected_session_revision: int, expected_grant_revision: int
    ) -> tuple[str, int]:
        saved = await _post_action(
            client,
            employee_auth,
            "session.save",
            {
                "expected_revision": expected_session_revision,
                "selected_kb_ids": [kb_id],
                "mode": "KB_ONLY",
                "source_revisions": {
                    "connection_id": connection_id,
                    "config_revision": connection_revision,
                    "binding_revision": binding_revision,
                    "grant_revision": expected_grant_revision,
                    "search_revision": search_revision,
                },
            },
            context=context,
        )
        prepared = await _post_action(
            client,
            employee_auth,
            "operation.prepare",
            {"expected_session_revision": saved["session"]["revision"]},
            context=context,
        )
        operation_id = prepared["operation"]["operation_id"]
        frames: list[dict[str, Any]] = []
        async with websockets.connect(url, open_timeout=15, close_timeout=5) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "user_turn",
                        "thread_id": thread_id,
                        "text": f"Retrieve an authorized synthetic record from {kb_id}.",
                        "knowledge_base_ids": [kb_id],
                        "plugin_context": {"enterprise-knowledge": {"operation_id": operation_id}},
                        "conversation_mode": "ask",
                    }
                )
            )
            for _ in range(200):
                frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=20))
                if isinstance(frame, dict):
                    frames.append(frame)
                    if frame.get("type") == "done":
                        break
            else:
                raise RuntimeError("real graph acceptance turn did not finish")
        answer = "".join(
            str(frame.get("content") or "") for frame in frames if frame.get("type") == "token"
        )
        operation = await facade.get_record("operations", operation_id)
        evidence = await facade.list_records("evidence", f'["{operation_id}",')
        knowledge_evidence = [
            item for item in evidence if item.payload.get("source_kind") == "KNOWLEDGE"
        ]
        if SYNTHETIC_ANSWER not in answer:
            raise RuntimeError("real graph did not emit the deterministic final answer")
        if operation is None or operation.payload.get("status") != "COMPLETED":
            raise RuntimeError("real graph operation did not complete")
        if not knowledge_evidence:
            raise RuntimeError("real graph did not register authorized knowledge evidence")
        return operation_id, len(knowledge_evidence)

    first_operation_id, first_evidence_count = await send_turn("kb-private-a", 0, grant_revision)

    second_operation_id, second_evidence_count = await send_turn("kb-private-a", 1, grant_revision)
    if first_operation_id == second_operation_id:
        raise RuntimeError("same-thread graph turns reused an operation id")

    history: dict[str, Any] = {}
    for _ in range(80):
        history_response = await client.get(
            f"/api/agents/{agent_id}/threads/{thread_id}/history",
            headers=employee_auth,
            params={"limit": 100},
        )
        history_response.raise_for_status()
        history = cast(dict[str, Any], history_response.json())
        if not history.get("history_loading"):
            break
        await asyncio.sleep(0.25)
    assistant_answers = [
        message
        for message in history.get("messages", [])
        if message.get("role") == "assistant"
        and any(
            isinstance(block, dict) and SYNTHETIC_ANSWER in str(block.get("text") or "")
            for block in message.get("content", [])
        )
    ]
    answer_ids = [message.get("id") for message in assistant_answers]
    if len(answer_ids) < 2 or any(not message_id for message_id in answer_ids):
        raise RuntimeError("real thread history did not preserve both assistant answers")
    if len(set(answer_ids)) != len(answer_ids):
        raise RuntimeError("same-thread graph turns reused an assistant message id")
    if not model.bind_calls:
        raise RuntimeError("final deterministic model was not bound to graph tools")
    if "enterprise_knowledge_search" not in model.bound_tools:
        raise RuntimeError("real graph did not bind the registered enterprise search tool")
    return {
        "graph_answer_seen": True,
        "graph_operation_statuses": ["COMPLETED", "COMPLETED"],
        "graph_operation_ids": [first_operation_id, second_operation_id],
        "knowledge_evidence_counts": [first_evidence_count, second_evidence_count],
        "history_assistant_answer_count": len(answer_ids),
        "history_assistant_message_ids_unique": True,
        "model_bind_tools_calls": model.bind_calls,
    }


async def _provision(
    client: httpx.AsyncClient,
    server: Any,
    home: Path,
    upstreams: SyntheticUpstreams,
    port: int,
) -> dict[str, Any]:
    auth_helpers = importlib.import_module("tests.support.auth")

    database_response = await client.post(
        "/api/setup/database",
        json={"driver": "sqlite", "sqlite_path": "octop.db"},
    )
    database_response.raise_for_status()
    await auth_helpers.bootstrap_admin(client, home, password=ADMIN_PASSWORD)
    admin_auth = await auth_helpers.auth_header(client, password=ADMIN_PASSWORD)
    provider_response = await client.post(
        "/api/admin/providers",
        headers=admin_auth,
        json={
            "name": "openai",
            "kind": "openai",
            "base_url": "https://llm.synthetic.test/v1",
            "api_key": "synthetic-llm-key-only",
            "models": [
                {
                    "id": "gpt-4o",
                    "name": "GPT-4o",
                    "enabled": True,
                    "input": ["text"],
                    "thinking": False,
                }
            ],
        },
    )
    provider_response.raise_for_status()
    plugins = server.plugin_manager
    if plugins is None:
        raise RuntimeError("PluginManager did not start")
    for plugin_id, source in (
        ("enterprise-knowledge", KNOWLEDGE),
        ("enterprise-policy", POLICY),
    ):
        await plugins.mutate(lambda p=source: plugins.install_path(p, force=True))
        await plugins.mutate(lambda p=plugin_id: plugins.set_enabled(p, True))
    installed = plugins.plugins_dir
    hashes = {
        "enterprise-knowledge": installed_artifact_hashes(
            KNOWLEDGE, installed / "enterprise-knowledge"
        ),
        "enterprise-policy": installed_artifact_hashes(POLICY, installed / "enterprise-policy"),
    }

    # These are the actual plugin HTTP client factories. The transports fail closed for
    # all unrecognized URLs and never use ambient proxy configuration.
    search_http: Any = importlib.import_module(
        "harness_plugin_enterprise_knowledge.enterprise_knowledge.search_http"
    )
    weknora: Any = importlib.import_module(
        "harness_plugin_enterprise_knowledge.enterprise_knowledge.weknora"
    )

    transport = httpx.MockTransport(upstreams.handle)
    weknora._build_async_client = lambda _ca_bundle: httpx.AsyncClient(
        transport=transport, verify=False, follow_redirects=False, trust_env=False
    )
    search_http._make_client = lambda _ca_bundle, _transport: httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False
    )

    employee_auth = await auth_helpers.create_user(
        client,
        admin_auth,
        username="acceptance-employee",
        password=EMPLOYEE_PASSWORD,
    )
    user_id = await auth_helpers.resolve_user_id(client, admin_auth, "acceptance-employee")
    agent_id = await auth_helpers.create_agent(
        client, employee_auth, name="Enterprise acceptance expert"
    )
    thread = await client.post(f"/api/agents/{agent_id}/threads", headers=employee_auth)
    thread.raise_for_status()
    thread_id = thread.json()["thread_id"]
    await server.app_runtime.agent_registry.on_provider_changed()
    factory = server.app_runtime.agent_registry._harness_manager._shared_factory
    if factory is None:
        raise RuntimeError("shared LLM factory did not start")
    model = AcceptanceChatModel(responses=[SYNTHETIC_ANSWER])
    factory.get_chat_model = acceptance_model_factory(model)
    await server.app_runtime.agent_registry.reload(agent_id)

    connection = await _post_action(
        client,
        admin_auth,
        "connection.save",
        {
            "name": "Synthetic acceptance knowledge",
            "base_url": "https://weknora.synthetic.test/api/v1",
            "allowed_kb_ids": list(upstreams.knowledge_ids),
            "enabled": True,
            "api_key": SYNTHETIC_KEY,
        },
    )
    connection_id = connection["connection"]["connection_id"]
    connection_revision = connection["connection"]["revision"]
    for component, extra in (
        ("LIST", {}),
        ("SEARCH", {"query": "synthetic acceptance probe", "knowledge_base_ids": ["kb-private-a"]}),
        ("READ", {"knowledge_base_ids": ["kb-private-a"], "knowledge_id": "doc-private-a"}),
    ):
        result = await _post_action(
            client,
            admin_auth,
            "connection.probe",
            {
                "connection_id": connection_id,
                "expected_revision": connection_revision,
                "component": component,
                **extra,
            },
        )
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"connection {component} probe did not succeed")
    grant = await _post_action(
        client,
        admin_auth,
        "grant.save",
        {
            "connection_id": connection_id,
            "user_id": user_id,
            "user_allowed_kb_ids": ["kb-private-a"],
        },
    )
    grant_revision = grant["grant"]["revision"]
    binding = await _post_action(
        client,
        admin_auth,
        "binding.save",
        {
            "connection_ref": connection_id,
            "expert_allowed_kb_ids": ["kb-private-a", "kb-private-b"],
            "default_kb_ids": ["kb-private-a"],
            "web_allowed": True,
            "enabled": True,
        },
        context={"agent_id": agent_id},
    )

    search = await _post_action(
        client,
        admin_auth,
        "search.save",
        {
            "expected_revision": 0,
            "primary": {"base_url": "https://searxng.synthetic.test", "credential_op": "KEEP"},
            "secondary": {
                "base_url": "https://firecrawl.synthetic.test",
                "credential_op": "REPLACE",
                "credential": SYNTHETIC_SEARCH_KEY,
            },
        },
    )
    search_revision = search["revision"]
    for source_role in ("PRIMARY", "SECONDARY"):
        diagnostic = await _post_action(
            client,
            admin_auth,
            "search.test",
            {
                "source_role": source_role,
                "expected_revision": search_revision,
                "public_query": SOURCE_TEST_QUERY,
            },
        )
        if diagnostic["diagnostic"]["status"] not in {"SUCCESS", "EMPTY"}:
            raise RuntimeError(f"{source_role} search source probe did not succeed")

    public_outage = await _post_action(
        client,
        admin_auth,
        "search.test",
        {
            "source_role": "PRIMARY",
            "expected_revision": search_revision,
            "public_query": PUBLIC_QUERY,
        },
    )
    if public_outage["diagnostic"]["status"] != "ERROR":
        raise RuntimeError("fixed public acceptance query did not trigger primary 503")

    graph_result = await _verify_real_graph_turn(
        client,
        agent_id=agent_id,
        thread_id=thread_id,
        connection_id=connection_id,
        connection_revision=connection_revision,
        grant_revision=grant_revision,
        binding_revision=binding["binding"]["revision"],
        search_revision=search_revision,
        employee_password=EMPLOYEE_PASSWORD,
        model=model,
        port=port,
        server=server,
    )

    credentials = home / "acceptance-login.txt"
    credentials.write_text(
        "Synthetic isolated instance credentials\n"
        f"admin: admin / {ADMIN_PASSWORD}\n"
        f"employee: acceptance-employee / {EMPLOYEE_PASSWORD}\n",
        encoding="utf-8",
    )
    return {
        "admin_auth": admin_auth,
        "employee_auth": employee_auth,
        "agent_id": agent_id,
        "thread_id": thread_id,
        "employee_user_id": user_id,
        "expert_agent_id": agent_id,
        "connection_id": connection_id,
        "connection_revision": connection_revision,
        "grant_revision": grant_revision,
        "binding_revision": binding["binding"]["revision"],
        "search_revision": search_revision,
        "public_test_query": PUBLIC_QUERY,
        "public_primary_outage_status": public_outage["diagnostic"]["status"],
        **graph_result,
        "plugin_hashes": hashes,
        "credentials_file": str(credentials),
    }


async def run(home: Path, port: int) -> None:
    home = prepare_empty_home(home)
    require_loopback_host("127.0.0.1")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    os.environ["OCTOP_HOME"] = str(home)
    os.environ["OCTOP_HOST"] = "127.0.0.1"
    os.environ["OCTOP_PORT"] = str(port)
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    for key in tuple(os.environ):
        if key.startswith("OCTOP_DATABASE_") or key in {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
            "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY",
            "ARK_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
            "COHERE_API_KEY",
            "OCTOP_CAPTCHA_PROVIDER",
            "OCTOP_CAPTCHA_SITE_KEY",
            "OCTOP_CAPTCHA_SECRET",
        }:
            os.environ.pop(key, None)

    import uvicorn

    from octop.api.app import build_app
    from octop.infra.server import OctopServer

    server = OctopServer(home=home)
    # Octop emits its one-time setup password to stdout on first boot. Keep that
    # synthetic credential out of test-run logs while allowing warnings on stderr.
    with redirect_stdout(io.StringIO()):
        await server.start()
    app = build_app(server)
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    server_task = asyncio.create_task(uvicorn_server.serve())
    upstreams = SyntheticUpstreams()
    state_path = home / "acceptance-state.json"
    upstreams.state_path = state_path
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
        ) as client:
            for _ in range(100):
                if uvicorn_server.started:
                    break
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Octop HTTP listener did not become ready")
            fixture = await _provision(client, server, home, upstreams, port)
            anonymous = await client.get("/api/admin/overview")
            if anonymous.status_code not in {401, 403}:
                raise RuntimeError("anonymous request unexpectedly accessed protected API")
            fixture["anonymous_protected_api_status"] = anonymous.status_code
        safe = {key: value for key, value in fixture.items() if not key.endswith("_auth")}
        safe.update({"host": "127.0.0.1", "port": port, "status": "ready"})
        safe.update({"admin_username": "admin", "employee_username": "acceptance-employee"})
        upstreams.state_metadata = safe
        state_path.write_text(
            json.dumps({"ready": safe, "upstreams": upstreams.summary()}, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"ready": safe, "state_file": str(state_path)}, ensure_ascii=False))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        uvicorn_server.should_exit = True
        with suppress(asyncio.CancelledError):
            await server_task
        await server.stop()
        upstreams.state_metadata = {**upstreams.state_metadata, "status": "stopped"}
        state_path.write_text(
            json.dumps(
                {"ready": upstreams.state_metadata, "upstreams": upstreams.summary()}, indent=2
            ),
            encoding="utf-8",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--port", default=5190, type=int)
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args.home, args.port))
    except KeyboardInterrupt:
        # On Windows asyncio has no add_signal_handler(); its default Ctrl-C
        # cancellation still runs run()'s finally shutdown/drain path.
        return 0
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(f"enterprise UI runtime failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
