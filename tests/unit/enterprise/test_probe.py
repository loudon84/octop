from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from scripts.enterprise_knowledge_probe import REQUIRED_ENV, build_config, run_probe

KEY = "secret-key-123"
QUERY = "private test query"
KB_IDS = ["kb-one", "kb-two"]
DOC_IDS = ["doc-one", "doc-two"]


def valid_env() -> dict[str, str]:
    return {
        "OCTOP_WEKNORA_BASE_URL": "https://weknora.example/api/v1",
        "OCTOP_WEKNORA_API_KEY": KEY,
        "OCTOP_WEKNORA_VERSION": "v1.2.3",
        "OCTOP_WEKNORA_IMAGE_DIGEST": "sha256:abc123",
        "OCTOP_WEKNORA_TEST_KB_IDS": json.dumps(KB_IDS),
        "OCTOP_WEKNORA_TEST_DOCUMENT_IDS": json.dumps(DOC_IDS),
        "OCTOP_WEKNORA_PROBE_QUERY": QUERY,
        "OCTOP_WEKNORA_CONSUMER_IDENTITY": "octop-ci",
    }


def response_for(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("knowledge-bases"):
        data = [{"id": "kb-one"}, {"id": "kb-two"}]
    elif path.endswith("knowledge-search"):
        data = []
    elif "/knowledge/" in path:
        data = {"id": path.rsplit("/", 1)[-1], "kb_id": "kb-one" if "one" in path else "kb-two"}
    else:
        data = [{"chunk_index": 0}]
    return httpx.Response(200, json={"success": True, "data": data}, request=request)


def make_config():
    return build_config(valid_env())


def test_missing_live_and_required_inputs_are_blocked_without_requests(tmp_path):
    env = valid_env()
    missing, report = build_config(
        {k: v for k, v in env.items() if k != REQUIRED_ENV[-1]}, live=False
    )
    assert missing is None
    assert report["status"] == "BLOCKED"
    assert REQUIRED_ENV[-1] in report["missing"]
    assert report["requests"] == 0


def test_invalid_configuration_is_rejected_without_echoing_secrets():
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "https://user:password@example.com/api/v1?key=bad"
    env["OCTOP_WEKNORA_TEST_KB_IDS"] = '["same", "same"]'
    config, report = build_config(env, live=True)
    assert config is None
    assert report == {
        "status": "BLOCKED",
        "missing": [],
        "error": "INVALID_CONFIG",
        "requests": 0,
    }
    assert "password" not in json.dumps(report)
    assert KEY not in json.dumps(report)


@pytest.mark.asyncio
async def test_success_runs_seven_exact_read_only_requests_and_reports_metadata():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_for(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(make_config(), client=client)

    assert report["status"] == "PASS"
    assert report["requests"] == 7
    assert [(r.method, r.url.path.removeprefix("/api/v1")) for r in seen] == [
        ("GET", "/knowledge-bases"),
        ("POST", "/knowledge-search"),
        ("POST", "/knowledge-search"),
        ("GET", "/knowledge/doc-one"),
        ("GET", "/chunks/doc-one"),
        ("GET", "/knowledge/doc-two"),
        ("GET", "/chunks/doc-two"),
    ]
    for request in seen:
        assert request.headers["X-API-Key"] == KEY
        assert "X-Tenant-ID" not in request.headers
        assert "Authorization" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }
    searches = [r for r in seen if r.method == "POST"]
    assert [r.url.params["resource_urls"] for r in searches] == ["handle", "handle"]
    assert [json.loads(r.content) for r in searches] == [
        {"query": QUERY, "knowledge_base_ids": ["kb-one"]},
        {"query": QUERY, "knowledge_base_ids": ["kb-two"]},
    ]
    assert report["remote"]["version"] == "v1.2.3"
    assert report["observed"]["knowledge_bases"] == 2


@pytest.mark.asyncio
async def test_empty_search_is_reported_as_empty_not_failed():
    async with httpx.AsyncClient(transport=httpx.MockTransport(response_for)) as client:
        report = await run_probe(make_config(), client=client)
    assert report["checks"]["search_kb_one"]["status"] == "EMPTY"
    assert report["status"] == "PASS"


@pytest.mark.asyncio
async def test_schema_drift_fails_and_does_not_hide_bad_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("knowledge-bases"):
            data = [{"name": "missing id"}]
        else:
            return response_for(request)
        return httpx.Response(200, json={"success": True, "data": data}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(make_config(), client=client)
    assert report["status"] == "FAILED"
    assert report["checks"]["list"]["error"] == "SCHEMA_DRIFT"
    assert report["requests"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, text="secret-key-123"), "HTTP_401"),
        (httpx.Response(403, text="private test query"), "HTTP_403"),
        (httpx.Response(302, headers={"Location": "https://bad.example"}), "REDIRECT"),
    ],
)
async def test_http_failures_are_sanitized(response, error):
    async def handler(request: httpx.Request) -> httpx.Response:
        response.request = request
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        report = await run_probe(make_config(), client=client)
    assert report["status"] == "FAILED"
    serialized = json.dumps(report)
    assert KEY not in serialized
    assert QUERY not in serialized
    assert error in serialized


@pytest.mark.asyncio
async def test_timeout_does_not_expose_exception_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"{KEY} {QUERY} body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(make_config(), client=client)
    assert report["status"] == "FAILED"
    assert "TIMEOUT" in json.dumps(report)
    assert KEY not in json.dumps(report)
    assert QUERY not in json.dumps(report)


def test_config_requires_live_even_when_all_environment_values_exist():
    config, report = build_config(valid_env(), live=False)
    assert config is None
    assert report["status"] == "BLOCKED"
    assert "--live" in report["missing"]
    assert report["requests"] == 0


def test_cli_without_live_writes_blocked_report_and_exits_two(tmp_path, monkeypatch):
    import os

    from scripts.enterprise_knowledge_probe import main

    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    output = tmp_path / "probe.json"
    assert main(["--output", str(output)]) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "BLOCKED"
    assert "--live" in report["missing"]
    assert report["requests"] == 0
    assert os.environ.get("OCTOP_WEKNORA_API_KEY") is None


@pytest.mark.asyncio
async def test_exception_text_containing_key_query_and_body_never_enters_report():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"{KEY} {QUERY} body={request.content!r}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(make_config(), client=client)
    encoded = json.dumps(report)
    assert KEY not in encoded
    assert QUERY not in encoded
    assert "body=" not in encoded


@pytest.mark.asyncio
async def test_async_probe_supports_outer_cancellation():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await run_probe(make_config(), client=client)


@pytest.mark.asyncio
async def test_request_budget_stops_before_exceeding_configured_limit(monkeypatch):
    import scripts.enterprise_knowledge_probe as probe

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_for(request)

    monkeypatch.setattr(probe, "MAX_REQUESTS", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(make_config(), client=client)
    assert report["status"] == "FAILED"
    assert report["requests"] == 1
    assert len(seen) == 1
    assert "REQUEST_LIMIT" in json.dumps(report)


@pytest.mark.asyncio
async def test_overall_budget_expires_without_waiting(monkeypatch):
    seen = []
    timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_for(request)

    @asynccontextmanager
    async def expired_timeout(seconds):
        timeouts.append(seconds)
        raise TimeoutError
        yield

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(make_config(), client=client, timeout_factory=expired_timeout)
    assert report["status"] == "FAILED"
    assert report["requests"] == 0
    assert seen == []
    assert report["checks"]["overall"]["error"] == "TIMEOUT"
    assert timeouts == [90.0]


def test_production_http_and_unsafe_path_ids_are_blocked():
    for field, value in (
        ("OCTOP_WEKNORA_BASE_URL", "http://weknora.example/api/v1"),
        ("OCTOP_WEKNORA_BASE_URL", "http://192.168.1.2/api/v1"),
        ("OCTOP_WEKNORA_BASE_URL", "http://weknora.local/api/v1"),
        ("OCTOP_WEKNORA_BASE_URL", "https://weknora.example/other/api/v1"),
        ("OCTOP_WEKNORA_BASE_URL", "https://weknora.example/api/v1?"),
        ("OCTOP_WEKNORA_BASE_URL", "https://weknora.example/api/v1#"),
        ("OCTOP_WEKNORA_TEST_KB_IDS", '["../escape", "kb-two"]'),
        ("OCTOP_WEKNORA_TEST_DOCUMENT_IDS", '["doc/one", "doc-two"]'),
    ):
        env = valid_env()
        env[field] = value
        parsed = build_config(env, live=True)
        assert isinstance(parsed, tuple)
        config, report = parsed
        assert config is None
        assert report["status"] == "BLOCKED"
        assert report["requests"] == 0


def test_https_allows_private_and_local_names():
    for host in ("192.168.1.2", "weknora.local"):
        env = valid_env()
        env["OCTOP_WEKNORA_BASE_URL"] = f"https://{host}/api/v1"
        config = build_config(env)
        assert config.base_url == f"https://{host}/api/v1"


def test_exact_test_http_origin_allows_only_matching_remote_http_origin():
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "http://weknora.example/api/v1"
    env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = "http://weknora.example:80"
    config = build_config(env)
    assert not isinstance(config, tuple)
    assert config.base_url == "http://weknora.example/api/v1"
    assert config.test_http_exception_origin == "http://weknora.example"


def test_remote_http_rejects_idna2003_collision_with_authorized_ascii_host():
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "http://fa\u00df.example/api/v1"
    env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = "http://fass.example"
    config, report = build_config(env)
    assert config is None
    assert report["status"] == "BLOCKED"
    assert report["requests"] == 0


@pytest.mark.asyncio
async def test_http_idna_origin_matches_final_mocktransport_request_host():
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "http://fa\u00df.example/api/v1"
    env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = "http://fa\u00df.example"
    config = build_config(env)
    assert not isinstance(config, tuple)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_for(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_probe(config, client=client)

    assert report["status"] == "PASS"
    assert config.test_http_exception_origin == "http://xn--fa-hia.example"
    authorized = httpx.URL(config.test_http_exception_origin)
    assert len(seen) == 7
    for request in seen:
        assert request.url.scheme == authorized.scheme
        assert request.url.raw_host == authorized.raw_host
        assert (request.url.port or 80) == (authorized.port or 80)


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://other.example",
        "http://api.weknora.example",
        "http://weknora.example:81",
        "http://weknora.example:",
        "https://weknora.example",
        "http://weknora.example/path",
        "http://user@weknora.example",
        "http://weknora.example?query=1",
        "http://weknora.example#fragment",
    ],
)
def test_remote_http_requires_exact_pure_test_origin(origin):
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "http://weknora.example/api/v1"
    if origin is None:
        env.pop("OCTOP_WEKNORA_TEST_HTTP_ORIGIN", None)
    else:
        env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = origin
    config, report = build_config(env)
    assert config is None
    assert report["status"] == "BLOCKED"
    assert report["requests"] == 0


def test_https_does_not_depend_on_test_http_origin():
    env = valid_env()
    env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = "not an origin"
    config = build_config(env)
    assert not isinstance(config, tuple)
    assert config.base_url == "https://weknora.example/api/v1"
    assert config.test_http_exception_origin is None


def test_base_url_normalizes_api_v1_suffix_with_test_http_exception():
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "http://weknora.example/api/v1/api/v1/"
    env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = "http://weknora.example"
    config = build_config(env)
    assert not isinstance(config, tuple)
    assert config.base_url == "http://weknora.example/api/v1"


@pytest.mark.asyncio
async def test_test_http_exception_is_reported_without_key_or_tls_claim():
    env = valid_env()
    env["OCTOP_WEKNORA_BASE_URL"] = "http://weknora.example/api/v1"
    env["OCTOP_WEKNORA_TEST_HTTP_ORIGIN"] = "http://weknora.example:80"
    config = build_config(env)
    assert not isinstance(config, tuple)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response_for)) as client:
        report = await run_probe(config, client=client)
    assert report["status"] == "PASS"
    assert report["test_http_exception"] is True
    assert report["test_http_exception_origin"] == "http://weknora.example"
    assert "tls_verified" not in report
    assert KEY not in json.dumps(report)


@pytest.mark.asyncio
async def test_local_sha_subprocess_runs_off_event_loop_thread(monkeypatch):
    import scripts.enterprise_knowledge_probe as probe

    loop_thread = threading.get_ident()
    subprocess_thread = []

    def fake_run(*args, **kwargs):
        subprocess_thread.append(threading.get_ident())
        return SimpleNamespace(stdout="a" * 40)

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response_for)) as client:
        report = await run_probe(make_config(), client=client)
    assert report["local_octop_sha"] == "a" * 40
    assert subprocess_thread
    assert subprocess_thread[0] != loop_thread
