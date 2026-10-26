from __future__ import annotations

import asyncio
import json
import ssl
import sys
import threading
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from octop.infra.agents.plugins.host_contracts import HostRecord, PluginActor

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_weknora = import_module("enterprise_knowledge.weknora")


def test_search_hit_keeps_official_title_and_only_numeric_locator_pages() -> None:
    hit = _weknora.SearchHit(
        knowledge_id="doc-1",
        knowledge_base_id="kb-1",
        content="text",
        original_length=4,
        truncated=False,
        title="Official title",
        source_page=_weknora.WeKnoraProvider._source_page({"page": 2}),
        source_version=None,
    )
    assert hit.title == "Official title"
    assert hit.source_page == 2
    assert _weknora.WeKnoraProvider._source_page({"page": "p.2"}) is None
    assert _weknora.WeKnoraProvider._source_page({"page": True}) is None


WeKnoraProvider = _weknora.WeKnoraProvider
WeKnoraError = _weknora.WeKnoraError

CONNECTION_ID = "00000000-0000-4000-8000-000000000001"
KEY_CANARY = "synthetic-key-canary"
ACTOR = PluginActor(user_id=8, is_admin=False, agent_id="agent-a", thread_id="thread-a")


class Facade:
    def __init__(self, *, revision: int = 4, blocked: bool = False) -> None:
        self.connection = HostRecord(
            id=CONNECTION_ID,
            namespace="connections",
            logical_key=CONNECTION_ID,
            owner_user_id=3,
            revision=revision,
            payload={
                "base_url": "https://weknora.synthetic/api/v1",
                "tenant_id": "tenant-synthetic",
                "credential_ref": "opaque-credential-ref",
                "credential_owner_user_id": 3,
                "enabled": True,
                "allowed_kb_ids": ["kb-a", "kb-b", "kb-secret"],
            },
        )
        self.blocked = blocked
        self.credential_owners: list[int] = []
        self.extra_records: dict[tuple[str, str], HostRecord] = {}

    async def get_record(self, namespace: str, key: str) -> HostRecord | None:
        if (namespace, key) in self.extra_records:
            return self.extra_records[(namespace, key)]
        if namespace == "connections" and key == CONNECTION_ID:
            return self.connection
        if namespace == "connection_intents" and key == CONNECTION_ID and self.blocked:
            return HostRecord(
                id=CONNECTION_ID,
                namespace=namespace,
                logical_key=key,
                owner_user_id=3,
                revision=1,
                payload={"state": "BLOCKED"},
            )
        return None

    async def resolve_credential(self, credential_ref: str, owner_user_id: int) -> dict[str, str]:
        assert credential_ref == "opaque-credential-ref"
        self.credential_owners.append(owner_user_id)
        return {"api_key": KEY_CANARY}


def make_provider(
    handler,
    *,
    facade: Facade | None = None,
    expected_revision: int = 4,
):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = WeKnoraProvider(
        facade or Facade(),
        ACTOR,
        CONNECTION_ID,
        expected_revision,
        client=client,
    )
    return provider, client


def ok(data, **extra):
    return httpx.Response(200, json={"success": True, "data": data, **extra})


@pytest.mark.asyncio
async def test_directory_and_metadata_share_one_list_request_and_filter_to_ids():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(
            [
                {
                    "id": "kb-a",
                    "name": "A",
                    "description": "safe",
                    "creator_id": "private",
                    "knowledge_count": 2,
                },
                {"id": "kb-hidden", "name": "Hidden", "creator_id": "secret"},
            ]
        )

    facade = Facade()
    provider, client = make_provider(handler, facade=facade)
    try:
        assert await provider.visible_kb_ids(CONNECTION_ID, ACTOR) == ["kb-a", "kb-hidden"]
        assert await provider.metadata_for(CONNECTION_ID, ["kb-a"], ACTOR) == [
            {"kb_id": "kb-a", "name": "A", "description": "safe", "knowledge_count": 2}
        ]
        assert len(seen) == 1
        assert seen[0].headers["X-API-Key"] == KEY_CANARY
        assert seen[0].headers["X-Tenant-ID"] == "tenant-synthetic"
        assert facade.credential_owners == [3]
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_integrates_with_p05a_scope_policy():
    from enterprise_knowledge.policy import ScopePolicy

    facade = Facade()
    facade.extra_records = {
        ("bindings", "agent-a"): HostRecord(
            id="agent-a",
            namespace="bindings",
            logical_key="agent-a",
            owner_user_id=3,
            revision=2,
            payload={
                "enabled": True,
                "connection_ref": CONNECTION_ID,
                "expert_allowed_kb_ids": ["kb-a", "kb-b"],
            },
        ),
        ("grants", f"{CONNECTION_ID}:8"): HostRecord(
            id="grant-a",
            namespace="grants",
            logical_key=f"{CONNECTION_ID}:8",
            owner_user_id=3,
            revision=3,
            payload={"user_allowed_kb_ids": ["kb-a", "kb-b"]},
        ),
        ("sessions", '[8,"agent-a","thread-a"]'): HostRecord(
            id="session-a",
            namespace="sessions",
            logical_key='[8,"agent-a","thread-a"]',
            owner_user_id=8,
            revision=5,
            payload={
                "selected_kb_ids": ["kb-a"],
                "mode": "KB_ONLY",
                "connection_id": CONNECTION_ID,
                "config_revision": 4,
                "binding_revision": 2,
                "grant_revision": 3,
            },
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/knowledge-bases")
        return ok(
            [
                {"id": "kb-a", "name": "A", "creator_id": "private"},
                {"id": "kb-b", "name": "B"},
                {"id": "kb-secret", "name": "Secret"},
            ]
        )

    provider, client = make_provider(handler, facade=facade)
    try:
        result = await ScopePolicy(facade, provider).catalog(ACTOR)
        assert result == [{"kb_id": "kb-a", "name": "A"}, {"kb_id": "kb-b", "name": "B"}]
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_search_preserves_order_truncates_and_detail_proves_missing_attribution():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("knowledge-search"):
            body = json.loads(request.content)
            assert body == {
                "query": "synthetic question",
                "knowledge_base_ids": ["kb-a"],
                "match_count": 8,
            }
            return ok(
                [
                    {"knowledge_id": "knowledge/1", "content": "x" * 1201, "score": 0.9},
                    {
                        "knowledge_id": "knowledge-2",
                        "knowledge_base_id": "kb-a",
                        "knowledge_title": "official title",
                        "content": "second",
                        "chunk_index": 3,
                        "source_locators": {"page": "p. 4"},
                        "file_hash": "must-not-be-a-version",
                    },
                ]
            )
        assert request.url.raw_path.endswith(b"/knowledge/knowledge%2F1")
        return ok({"id": "knowledge/1", "knowledge_base_id": "kb-a", "title": "synthetic title"})

    provider, client = make_provider(handler)
    try:
        hits = await provider.search("synthetic question", ["kb-a"])
        assert [hit.knowledge_id for hit in hits] == ["knowledge/1", "knowledge-2"]
        assert seen[0].url.params["resource_urls"] == "handle"
        assert len(hits[0].content) == 1200 and hits[0].truncated
        assert not hits[1].truncated and hits[1].chunk_index == 3
        assert hits[1].source_page is None and hits[1].source_version is None
        assert hits[0].title == "synthetic title"
        assert hits[1].title == "official title"
        assert [request.method for request in seen] == ["POST", "GET"]
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_detail_cache_rechecks_the_callers_allowed_scope() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok({"id": "doc-a", "knowledge_base_id": "kb-a", "title": "A"})

    provider, client = make_provider(handler)
    try:
        first = await provider.knowledge_detail("doc-a", ["kb-a", "kb-b"])
        second = await provider.knowledge_detail("doc-a", ["kb-a"])
        assert first == second
        assert len(requests) == 1
        with pytest.raises(WeKnoraError) as caught:
            await provider.knowledge_detail("doc-a", ["kb-b"])
        assert caught.value.code == "UPSTREAM_SCHEMA_INVALID"
        assert len(requests) == 1
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_wrong_detail_id_fails_closed_without_leaking_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return ok([{"knowledge_id": "item-a", "content": "private canary"}])
        return ok({"id": "item-b", "knowledge_base_id": "kb-a", "title": "private canary"})

    provider, client = make_provider(handler)
    try:
        with pytest.raises(WeKnoraError) as caught:
            await provider.search("synthetic", ["kb-a"])
        assert caught.value.code == "UPSTREAM_SCHEMA_INVALID"
        assert "private canary" not in str(caught.value)
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_revision_and_recovery_quarantine_reject_before_http():
    requests: list[httpx.Request] = []
    provider, client = make_provider(
        lambda request: requests.append(request) or ok([]), expected_revision=2
    )
    try:
        with pytest.raises(WeKnoraError) as caught:
            await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        assert caught.value.code == "CONFIG_CONFLICT"
        assert requests == []
    finally:
        await provider.aclose()
        await client.aclose()

    facade = Facade(blocked=True)
    provider, client = make_provider(
        lambda request: requests.append(request) or ok([]), facade=facade
    )
    try:
        with pytest.raises(WeKnoraError) as caught:
            await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        assert caught.value.code == "RECOVERY_BLOCKED"
        assert requests == []
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "test_origin"),
    [
        ("http://weknora.synthetic:0/api/v1", "http://weknora.synthetic"),
        ("http://weknora.synthetic/api/v1", "http://weknora.synthetic:0"),
        ("http://weknora.synthetic:/api/v1", "http://weknora.synthetic"),
        ("http://weknora.synthetic/api/v1", "http://weknora.synthetic:"),
    ],
)
async def test_explicit_port_zero_mismatch_is_rejected_before_mocktransport(
    base_url, test_origin, monkeypatch
):
    monkeypatch.setenv("OCTOP_WEKNORA_TEST_HTTP_ORIGIN", test_origin)
    facade = Facade()
    facade.connection = replace(
        facade.connection,
        payload={**facade.connection.payload, "base_url": base_url},
    )
    requests: list[httpx.Request] = []
    provider, client = make_provider(
        lambda request: requests.append(request) or ok([]), facade=facade
    )
    try:
        with pytest.raises(WeKnoraError) as caught:
            await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        assert caught.value.code == "PLUGIN_NOT_READY"
        assert requests == []
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_budget_is_shared_and_cancellation_stops_subsequent_requests():
    budget = _weknora.OperationBudget(max_requests=1)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok([])

    provider, client = make_provider(handler)
    provider._budget = budget
    try:
        await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        with pytest.raises(WeKnoraError) as caught:
            await provider.search("synthetic", ["kb-a"])
        assert caught.value.code == "UPSTREAM_BUDGET_EXCEEDED"
        assert calls == 1
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_chunks_use_contract_pagination_and_validate_top_level_envelope():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/knowledge/doc-a"):
            return ok({"id": "doc-a", "knowledge_base_id": "kb-a"})
        return ok(
            [
                {
                    "id": "chunk-a",
                    "knowledge_id": "doc-a",
                    "knowledge_base_id": "kb-a",
                    "chunk_index": 1,
                    "content": "body",
                    "chunk_type": "text",
                }
            ],
            total=1,
            page=2,
            page_size=10,
        )

    provider, client = make_provider(handler)
    try:
        page = await provider.chunks("doc-a", ["kb-a"], page=2, page_size=10)
        assert page.page == 2 and page.total == 1 and len(page.items) == 1
        assert dict(requests[1].url.params) == {"page": "2", "page_size": "10"}
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_requests_share_atomic_ten_request_cap():
    requests: list[httpx.Request] = []
    entered_setup = asyncio.Event()
    release_setup = asyncio.Event()
    all_arrived = asyncio.Event()

    class GatedFacade(Facade):
        blocked_once = False

        async def get_record(self, namespace: str, key: str) -> HostRecord | None:
            if namespace == "connections" and key == CONNECTION_ID and not self.blocked_once:
                self.blocked_once = True
                entered_setup.set()
                await release_setup.wait()
            return await super().get_record(namespace, key)

    class ArrivalBudget(_weknora.OperationBudget):
        arrivals = 0

        def remaining_seconds(self) -> float:
            remaining = super().remaining_seconds()
            self.arrivals += 1
            if self.arrivals == 12:
                all_arrived.set()
            return remaining

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok([])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = WeKnoraProvider(
        GatedFacade(),
        ACTOR,
        CONNECTION_ID,
        4,
        client=client,
        budget=ArrivalBudget(),
    )
    try:
        batch = asyncio.gather(
            *(provider.search(f"synthetic {index}", ["kb-a"]) for index in range(12)),
            return_exceptions=True,
        )
        await asyncio.wait_for(entered_setup.wait(), timeout=1)
        await asyncio.wait_for(all_arrived.wait(), timeout=1)
        release_setup.set()
        results = await batch
        assert len(requests) == 10
        errors = [result for result in results if isinstance(result, WeKnoraError)]
        assert [error.code for error in errors] == ["UPSTREAM_BUDGET_EXCEEDED"] * 2
    finally:
        release_setup.set()
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_search_caps_results_at_eight_and_rejects_out_of_scope_attribution():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        rows = [
            {"knowledge_id": f"doc-{index}", "knowledge_base_id": "kb-a", "content": str(index)}
            for index in range(9)
        ]
        rows[0]["knowledge_base_id"] = "kb-secret"
        return ok(rows)

    provider, client = make_provider(handler)
    try:
        with pytest.raises(WeKnoraError) as caught:
            await provider.search("synthetic query canary", ["kb-a"])
        assert caught.value.code == "UPSTREAM_SCHEMA_INVALID"
        assert "synthetic query canary" not in str(caught.value)
        assert len(requests) == 1
    finally:
        await provider.aclose()
        await client.aclose()

    def valid_handler(_request: httpx.Request) -> httpx.Response:
        return ok(
            [
                {"knowledge_id": f"doc-{index}", "knowledge_base_id": "kb-a", "content": str(index)}
                for index in range(9)
            ]
        )

    provider, client = make_provider(valid_handler)
    try:
        assert len(await provider.search("synthetic", ["kb-a"])) == 8
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_operation_propagates_and_blocks_later_requests():
    class HoldingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.requests = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests += 1
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    transport = HoldingTransport()
    client = httpx.AsyncClient(transport=transport)
    provider = WeKnoraProvider(Facade(), ACTOR, CONNECTION_ID, 4, client=client)
    request_task = asyncio.create_task(provider.search("synthetic", ["kb-a"]))
    await transport.entered.wait()
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    with pytest.raises(WeKnoraError) as caught:
        await provider.search("synthetic", ["kb-a"])
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    assert transport.requests == 1
    await provider.aclose()
    await client.aclose()


def test_budget_clamps_to_contract_and_counts_pre_request_operation_time():
    now = [100.0]
    budget = _weknora.OperationBudget(
        max_requests=100,
        operation_seconds=900,
        request_seconds=300,
        clock=lambda: now[0],
    )
    assert budget.max_requests == 10
    assert budget.operation_seconds == 90
    assert budget.request_seconds == 30
    now[0] += 90
    with pytest.raises(TimeoutError):
        asyncio.run(budget.reserve())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(401, text="private body"), "UPSTREAM_AUTH_FAILED"),
        (httpx.Response(403, text="private body"), "UPSTREAM_AUTH_FAILED"),
        (httpx.Response(302, headers={"Location": "/redirect"}), "UPSTREAM_HTTP_ERROR"),
        (httpx.Response(200, text="private body"), "UPSTREAM_SCHEMA_INVALID"),
        (httpx.Response(200, json={"success": False, "data": []}), "UPSTREAM_SCHEMA_INVALID"),
    ],
)
async def test_upstream_failures_are_sanitized_and_never_retried(response, expected_code):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    provider, client = make_provider(handler)
    try:
        with pytest.raises(WeKnoraError) as caught:
            await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        assert caught.value.code == expected_code
        assert "private body" not in str(caught.value)
        assert KEY_CANARY not in str(caught.value)
        assert calls == 1
    finally:
        await provider.aclose()
        await client.aclose()


def test_segment_ids_are_encoded_as_single_segments():
    assert quote("a/b", safe="") == "a%2Fb"


@pytest.mark.asyncio
@pytest.mark.parametrize("knowledge_id", [".", ".."])
async def test_dot_path_ids_are_rejected_before_any_mocktransport_request(knowledge_id):
    requests: list[httpx.Request] = []
    provider, client = make_provider(lambda request: requests.append(request) or ok({}))
    try:
        with pytest.raises(WeKnoraError) as detail_error:
            await provider.knowledge_detail(knowledge_id, ["kb-a"])
        with pytest.raises(WeKnoraError) as chunks_error:
            await provider.chunks(knowledge_id, ["kb-a"])
        assert detail_error.value.code == "UPSTREAM_SCHEMA_INVALID"
        assert chunks_error.value.code == "UPSTREAM_SCHEMA_INVALID"
        assert requests == []
    finally:
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["host", "credential"])
async def test_operation_deadline_covers_blocked_host_and_credential_preflight(stage):
    entered = asyncio.Event()
    never = asyncio.Event()

    class BlockedFacade(Facade):
        async def get_record(self, namespace: str, key: str) -> HostRecord | None:
            if stage == "host":
                entered.set()
                await never.wait()
            return await super().get_record(namespace, key)

        async def resolve_credential(self, credential_ref: str, owner_user_id: int):
            if stage == "credential":
                entered.set()
                await never.wait()
            return await super().resolve_credential(credential_ref, owner_user_id)

    requests: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: requests.append(request) or ok([]))
    )
    provider = WeKnoraProvider(
        BlockedFacade(),
        ACTOR,
        CONNECTION_ID,
        4,
        client=client,
        budget=_weknora.OperationBudget(operation_seconds=0.02),
    )
    try:
        request_task = asyncio.create_task(provider.visible_kb_ids(CONNECTION_ID, ACTOR))
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(WeKnoraError) as caught:
            await request_task
        assert caught.value.code == "UPSTREAM_BUDGET_EXCEEDED"
        assert requests == []
        with pytest.raises(WeKnoraError):
            await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        assert requests == []
    finally:
        never.set()
        await provider.aclose()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ca_bundle", [None, "synthetic-ca.pem"])
async def test_default_and_custom_client_factory_runs_off_loop_with_gate(monkeypatch, ca_bundle):
    loop_thread = threading.get_ident()
    factory_started = threading.Event()
    release_factory = threading.Event()
    ssl_threads: list[int] = []
    client_threads: list[int] = []
    ca_arguments: list[str | None] = []
    clients: list[httpx.AsyncClient] = []
    async_client_factory = httpx.AsyncClient

    def gated_ssl_factory(*, cafile: str | None) -> ssl.SSLContext:
        ssl_threads.append(threading.get_ident())
        ca_arguments.append(cafile)
        factory_started.set()
        assert release_factory.wait(timeout=2)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def tracked_client_factory(**kwargs) -> httpx.AsyncClient:
        client_threads.append(threading.get_ident())
        client = async_client_factory(
            transport=httpx.MockTransport(lambda _request: ok([])), **kwargs
        )
        clients.append(client)
        return client

    monkeypatch.setattr(_weknora.ssl, "create_default_context", gated_ssl_factory)
    monkeypatch.setattr(_weknora.httpx, "AsyncClient", tracked_client_factory)
    provider = WeKnoraProvider(Facade(), ACTOR, CONNECTION_ID, 4, ca_bundle=ca_bundle)
    try:
        setup_task = asyncio.create_task(provider._ensure_ready())
        assert await asyncio.wait_for(asyncio.to_thread(factory_started.wait, 1), timeout=2)
        assert not setup_task.done()
        assert threading.get_ident() == loop_thread
        release_factory.set()
        await setup_task
        assert ssl_threads and ssl_threads[0] != loop_thread
        assert client_threads and client_threads[0] != loop_thread
        assert ca_arguments == [ca_bundle]
        assert len(clients) == 1
    finally:
        release_factory.set()
        await provider.aclose()


@pytest.mark.asyncio
async def test_deadline_during_executor_client_init_cleans_late_client_and_blocks_requests(
    monkeypatch,
):
    factory_started = threading.Event()
    release_factory = threading.Event()
    requests: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []
    async_client_factory = httpx.AsyncClient

    def gated_ssl_factory(*, cafile: str | None) -> ssl.SSLContext:
        factory_started.set()
        assert release_factory.wait(timeout=2)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def tracked_client_factory(**kwargs) -> httpx.AsyncClient:
        client = async_client_factory(
            transport=httpx.MockTransport(lambda request: requests.append(request) or ok([])),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(_weknora.ssl, "create_default_context", gated_ssl_factory)
    monkeypatch.setattr(_weknora.httpx, "AsyncClient", tracked_client_factory)
    provider = WeKnoraProvider(
        Facade(),
        ACTOR,
        CONNECTION_ID,
        4,
        budget=_weknora.OperationBudget(operation_seconds=0.02),
    )
    try:
        request_task = asyncio.create_task(provider.visible_kb_ids(CONNECTION_ID, ACTOR))
        assert await asyncio.wait_for(asyncio.to_thread(factory_started.wait, 1), timeout=2)
        with pytest.raises(WeKnoraError) as caught:
            await request_task
        assert caught.value.code == "UPSTREAM_BUDGET_EXCEEDED"
        assert requests == []
        with pytest.raises(WeKnoraError):
            await provider.visible_kb_ids(CONNECTION_ID, ACTOR)
        assert requests == []
        release_factory.set()
        await asyncio.gather(*tuple(provider._client_cleanup_tasks))
        assert len(clients) == 1 and clients[0].is_closed
    finally:
        release_factory.set()
        await provider.aclose()


@pytest.mark.asyncio
async def test_request_deadline_wraps_the_async_transport_wall_time():
    class HoldingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.requests = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests += 1
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    transport = HoldingTransport()
    client = httpx.AsyncClient(transport=transport)
    provider = WeKnoraProvider(
        Facade(),
        ACTOR,
        CONNECTION_ID,
        4,
        client=client,
        budget=_weknora.OperationBudget(operation_seconds=0.5, request_seconds=0.02),
    )
    try:
        request_task = asyncio.create_task(provider.visible_kb_ids(CONNECTION_ID, ACTOR))
        await transport.entered.wait()
        with pytest.raises(WeKnoraError) as caught:
            await request_task
        assert caught.value.code == "UPSTREAM_TIMEOUT"
        assert transport.requests == 1
        assert provider._budget.requests_started == 1
    finally:
        await provider.aclose()
        await client.aclose()
