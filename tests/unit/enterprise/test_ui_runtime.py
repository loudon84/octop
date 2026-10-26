from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from langgraph.graph.message import add_messages

from tests.support.enterprise_ui_runtime import (
    CANARY,
    KNOWLEDGE,
    MAX_REQUEST_RECORDS,
    POLICY,
    PUBLIC_QUERY,
    SOURCE_TEST_QUERY,
    AcceptanceChatModel,
    SyntheticUpstreams,
    acceptance_model_factory,
    installed_artifact_hashes,
    prepare_empty_home,
    require_loopback_host,
)


def test_model_messages_do_not_reuse_ids_across_turns() -> None:
    model = AcceptanceChatModel(responses=["synthetic answer"])
    bound = model.bind_tools([type("Tool", (), {"name": "synthetic_search"})()])
    first = bound.invoke("first turn")
    second = bound.invoke("second turn")

    history = cast(list[Any], add_messages([], first))
    history = cast(list[Any], add_messages(history, second))
    streamed = "".join(str(chunk.content) for chunk in bound.stream("stream turn"))

    assert bound is model
    assert first is not second
    assert first.id != second.id
    assert len(history) == 2
    assert streamed == "synthetic answer"
    assert model.bound_tools == ["synthetic_search"]


def test_acceptance_factory_tolerates_model_factory_keywords() -> None:
    model = AcceptanceChatModel(responses=["synthetic answer"])
    get_chat_model = acceptance_model_factory(model)

    assert get_chat_model("openai/gpt-4o", max_retries=1) is model


def test_runtime_refuses_nonempty_home(tmp_path: Path) -> None:
    home = tmp_path / "existing"
    home.mkdir()
    (home / "keep.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="empty directory"):
        prepare_empty_home(home)

    assert (home / "keep.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.0.2.1", "::1"])
def test_runtime_rejects_non_ipv4_loopback_bind(host: str) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        require_loopback_host(host)


def test_runtime_accepts_only_ipv4_loopback_bind() -> None:
    assert require_loopback_host("127.0.0.1") == "127.0.0.1"


def test_installation_hash_check_covers_each_plugin_declared_runtime_file(tmp_path: Path) -> None:
    knowledge_copy = tmp_path / "enterprise-knowledge"
    policy_copy = tmp_path / "enterprise-policy"
    shutil.copytree(
        KNOWLEDGE, knowledge_copy, ignore=shutil.ignore_patterns("__pycache__", "node_modules")
    )
    shutil.copytree(POLICY, policy_copy, ignore=shutil.ignore_patterns("__pycache__"))

    knowledge_hashes = installed_artifact_hashes(KNOWLEDGE, knowledge_copy)
    policy_hashes = installed_artifact_hashes(POLICY, policy_copy)

    assert {"plugin.yaml", "main.py", "ui/dist/index.js", "ui/dist/manifest.json"} <= set(
        knowledge_hashes
    )
    assert set(policy_hashes) == {"plugin.yaml", "main.py"}
    (policy_copy / "main.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="differs from source"):
        installed_artifact_hashes(POLICY, policy_copy)


@pytest.mark.asyncio
async def test_synthetic_upstream_transport_is_closed_for_unknown_urls() -> None:
    upstream = SyntheticUpstreams()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle), trust_env=False)
    try:
        response = await client.get("https://unexpected.synthetic.test/not-a-fixture")
    finally:
        await client.aclose()

    assert response.status_code == 404
    summary = upstream.summary()
    assert summary["request_count"] == 1
    assert summary["recent_requests"] == [
        {
            "sequence": 1,
            "provider": "unknown",
            "method": "GET",
            "path": "/<unrecognized>",
            "contains_canary": False,
        }
    ]
    assert summary["canary_request_counts"] == {"knowledge": 0, "public": 0, "unknown": 0}
    assert response.status_code == 404
    assert summary["egress_blocked"] is True


@pytest.mark.asyncio
async def test_known_path_on_wrong_origin_or_with_wrong_method_fails_closed() -> None:
    upstream = SyntheticUpstreams()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle), trust_env=False)
    try:
        wrong_origin = await client.get("https://unexpected.synthetic.test/api/v1/knowledge-bases")
        wrong_method = await client.get("https://weknora.synthetic.test/api/v1/knowledge-search")
        wrong_search_method = await client.get("https://searxng.synthetic.test/search")
    finally:
        await client.aclose()

    assert [
        wrong_origin.status_code,
        wrong_method.status_code,
        wrong_search_method.status_code,
    ] == [
        404,
        404,
        404,
    ]
    assert upstream.summary()["recent_requests"] == [
        {
            "sequence": 1,
            "provider": "unknown",
            "method": "GET",
            "path": "/<unrecognized>",
            "contains_canary": False,
        },
        {
            "sequence": 2,
            "provider": "weknora",
            "method": "GET",
            "path": "/api/v1/knowledge-search",
            "contains_canary": False,
        },
        {
            "sequence": 3,
            "provider": "searxng",
            "method": "GET",
            "path": "/search",
            "contains_canary": False,
        },
    ]


@pytest.mark.asyncio
async def test_public_test_query_triggers_primary_503_without_query_logging() -> None:
    upstream = SyntheticUpstreams()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle), trust_env=False)
    try:
        ordinary = await client.post(
            "https://searxng.synthetic.test/search", data={"q": SOURCE_TEST_QUERY}
        )
        fixed_test_query = await client.post(
            "https://searxng.synthetic.test/search", data={"q": PUBLIC_QUERY}
        )
    finally:
        await client.aclose()

    assert ordinary.status_code == 200
    assert fixed_test_query.status_code == 503
    assert PUBLIC_QUERY not in str(upstream.summary())
    assert upstream.summary()["recent_requests"][-1] == {
        "sequence": 2,
        "provider": "searxng",
        "method": "POST",
        "path": "/search",
        "contains_canary": False,
    }


@pytest.mark.asyncio
async def test_synthetic_upstreams_cover_two_kbs_long_chunks_and_primary_outage() -> None:
    upstream = SyntheticUpstreams()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle), trust_env=False)
    try:
        bases = await client.get("https://weknora.synthetic.test/api/v1/knowledge-bases")
        chunks = await client.get(
            "https://weknora.synthetic.test/api/v1/chunks/doc-private-a",
            params={"page": 2, "page_size": 5},
        )
        primary = await client.post(
            "https://searxng.synthetic.test/search", data={"q": PUBLIC_QUERY}
        )
    finally:
        await client.aclose()

    assert [row["id"] for row in bases.json()["data"]] == ["kb-private-a", "kb-private-b"]
    assert chunks.json()["page"] == 2
    assert len(chunks.json()["data"]) == 5
    assert len(chunks.json()["data"][0]["content"]) > 1200
    assert primary.status_code == 503
    assert upstream.summary()["canary_request_counts"]["knowledge"] == 0
    assert upstream.summary()["canary_request_counts"]["public"] == 0


@pytest.mark.asyncio
async def test_request_summary_preserves_bounded_order_and_separates_canary_sources() -> None:
    async def record_sequence(requests: list[httpx.Request]) -> dict[str, Any]:
        upstream = SyntheticUpstreams()
        for request in requests:
            await upstream.handle(request)
        return upstream.summary()

    knowledge = httpx.Request(
        "POST",
        "https://weknora.synthetic.test/api/v1/knowledge-search",
        json={"query": CANARY, "knowledge_base_ids": ["kb-private-a"]},
    )
    public = httpx.Request(
        "POST",
        "https://searxng.synthetic.test/search",
        content=f"q={PUBLIC_QUERY.replace(' ', '+')}&format=json".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    first = await record_sequence([knowledge, public])
    second = await record_sequence([public, knowledge])
    bounded = await record_sequence([public] * (MAX_REQUEST_RECORDS + 1))

    assert first["recent_requests"] != second["recent_requests"]
    assert [row["sequence"] for row in first["recent_requests"]] == [1, 2]
    assert [row["contains_canary"] for row in first["recent_requests"]] == [True, False]
    assert first["canary_request_counts"] == {"knowledge": 1, "public": 0, "unknown": 0}
    assert second["canary_request_counts"] == {"knowledge": 1, "public": 0, "unknown": 0}
    assert PUBLIC_QUERY not in str(first)
    assert CANARY not in str(first)
    assert len(bounded["recent_requests"]) == MAX_REQUEST_RECORDS
    assert bounded["recent_requests"][0]["sequence"] == 2
    assert bounded["recent_requests"][-1]["sequence"] == MAX_REQUEST_RECORDS + 1
