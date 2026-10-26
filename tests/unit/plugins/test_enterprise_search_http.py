"""HTTP boundary tests for the selected enterprise search providers."""

from __future__ import annotations

import asyncio
import json
import ssl
import sys
import threading
import time
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_search_http = import_module("enterprise_knowledge.search_http")
SearchHttpAttempt = _search_http.SearchHttpAttempt
SearchHttpError = _search_http.SearchHttpError
SearchHttpResult = _search_http.SearchHttpResult
SearchSourceConfig = _search_http.SearchSourceConfig
normalize_search_base_url = _search_http.normalize_search_base_url


def _config(
    provider: str = "SEARXNG",
    *,
    base_url: str = "https://search.example/proxy/",
    credential: str | None = None,
    ca_bundle: str | None = None,
) -> SearchSourceConfig:
    return SearchSourceConfig(
        provider=provider,
        base_url=base_url,
        credential=SecretStr(credential) if credential is not None else None,
        ca_bundle=ca_bundle,
    )


def _searx_row(
    *,
    title: object = "title",
    url: object = "https://result.example/a",
    content: object = "snippet",
    **extra: Any,
) -> dict[str, object]:
    return {"title": title, "url": url, "content": content, **extra}


def _firecrawl_body(results: list[object]) -> dict[str, object]:
    return {"success": True, "data": {"web": results, "ignored": "extra"}, "ignored": True}


def _firecrawl_row(
    *,
    title: object = "title",
    url: object = "https://result.example/a",
    description: object = "snippet",
    **extra: Any,
) -> dict[str, object]:
    return {"title": title, "url": url, "description": description, **extra}


class _CountingMockTransport(httpx.MockTransport):
    def __init__(self, handler: Any) -> None:
        super().__init__(handler)
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        await super().aclose()


async def _run_search(
    config: SearchSourceConfig,
    body: object,
    *,
    query: str = "approved query",
    status: int = 200,
    headers: dict[str, str] | None = None,
    clock: Any = None,
    deadline_at: float | None = None,
    transport_handler: Any = None,
) -> tuple[SearchHttpResult, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        if transport_handler is not None:
            return await transport_handler(request)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=payload, headers=headers, request=request)

    kwargs = {"transport": httpx.MockTransport(handler)}
    if clock is not None:
        kwargs["clock"] = clock
    attempt = SearchHttpAttempt(config, **kwargs)
    try:
        result = await attempt.search(query, deadline_at=deadline_at)
        return result, requests
    finally:
        await attempt.aclose()


@pytest.mark.asyncio
async def test_searxng_posts_exact_form_and_preserves_query_bytes() -> None:
    query = "  查找\nA&B+C%  "
    result, requests = await _run_search(
        _config(), {"results": [_searx_row(extra="ignored")]}, query=query
    )

    assert result.status == "SUCCESS"
    assert result.hits[0].content == "snippet"
    assert result.hits[0].source_kind == "WEB"
    assert result.hits[0].source_version is None and result.hits[0].source_page is None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://search.example/proxy/search"
    assert parse_qs(request.content.decode(), keep_blank_values=True) == {
        "q": [query],
        "format": ["json"],
    }
    assert "q" not in request.url.params
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert not ({"authorization", "x-api-key", "cookie"} & set(request.headers))
    assert 0 < request.extensions["timeout"]["read"] <= 10
    assert set(parse_qs(request.content.decode())) == {"q", "format"}


@pytest.mark.asyncio
async def test_firecrawl_posts_exact_json_and_bearer_only_when_configured() -> None:
    query = "  查找\nA&B+C%  "
    result, requests = await _run_search(
        _config("FIRECRAWL", credential="fire-secret"),
        _firecrawl_body([_firecrawl_row(extra_field="ignored")]),
        query=query,
    )

    assert result.hits[0].provider == "FIRECRAWL"
    assert result.hits[0].content == "snippet"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://search.example/proxy/v2/search"
    assert json.loads(request.content) == {"query": query, "limit": 8, "highlights": False}
    assert request.headers["authorization"] == "Bearer fire-secret"
    assert not ({"cookie", "x-api-key"} & set(request.headers))
    assert "extra_field" not in result.hits[0].model_dump()


@pytest.mark.asyncio
async def test_firecrawl_without_credential_sends_no_auth_header() -> None:
    _, requests = await _run_search(_config("FIRECRAWL"), _firecrawl_body([_firecrawl_row()]))
    assert len(requests) == 1
    assert not ({"authorization", "x-api-key", "cookie"} & set(requests[0].headers))


@pytest.mark.asyncio
async def test_empty_arrays_are_empty_and_unknown_fields_are_not_projected() -> None:
    searx, first = await _run_search(_config(), {"results": [], "metadata": {"x": 1}})
    firecrawl, second = await _run_search(_config("FIRECRAWL"), _firecrawl_body([]))

    assert searx.status == firecrawl.status == "EMPTY"
    assert searx.hits == firecrawl.hits == ()
    assert not searx.results_truncated and not firecrawl.results_truncated
    assert len(first) == len(second) == 1
    assert "ignored" not in searx.model_dump()
    assert "ignored" not in firecrawl.model_dump()


@pytest.mark.asyncio
async def test_firecrawl_missing_description_normalizes_to_empty_snippet() -> None:
    row = _firecrawl_row()
    row.pop("description")
    for body in (_firecrawl_body([row]), _firecrawl_body([_firecrawl_row(description="")])):
        result, requests = await _run_search(_config("FIRECRAWL"), body)
        assert len(requests) == 1
        assert result.hits[0].content == ""
        assert result.hits[0].original_length == 0
        assert result.hits[0].truncated is False


@pytest.mark.asyncio
async def test_valid_result_url_query_fragment_and_duplicate_rows_are_preserved() -> None:
    duplicate = _searx_row(url="https://result.example/a?x=1#section")
    result, requests = await _run_search(_config(), {"results": [duplicate, duplicate]})
    assert len(requests) == 1
    assert [hit.locator for hit in result.hits] == ["https://result.example/a?x=1#section"] * 2


@pytest.mark.asyncio
async def test_valid_title_and_snippet_are_preserved_as_plain_text() -> None:
    row = _searx_row(title="  <em>title</em>  ", content="<b>snippet</b>", upstream_id="ignored")
    result, requests = await _run_search(_config(), {"results": [row]})
    hit = result.hits[0]
    assert len(requests) == 1
    assert hit.title == "  <em>title</em>  "
    assert hit.content == "<b>snippet</b>"
    assert hit.model_dump().keys() == {
        "provider",
        "source_kind",
        "title",
        "locator",
        "content",
        "original_length",
        "truncated",
        "source_version",
        "source_page",
    }


@pytest.mark.asyncio
async def test_root_provider_endpoints_and_request_timeouts_are_exact() -> None:
    observed: list[tuple[str, dict[str, float]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        observed.append((str(request.url), request.extensions["timeout"]))
        return httpx.Response(
            200,
            json={"results": []} if request.url.path == "/search" else _firecrawl_body([]),
            request=request,
        )

    transport = httpx.MockTransport(handler)
    for provider in ("SEARXNG", "FIRECRAWL"):
        attempt = SearchHttpAttempt(
            _config(provider, base_url="https://search.example/"), transport=transport
        )
        result = await attempt.search("query", deadline_at=time.monotonic() + 30)
        assert result.status == "EMPTY"
        await attempt.aclose()

    assert [url for url, _ in observed] == [
        "https://search.example/search",
        "https://search.example/v2/search",
    ]
    for _, timeouts in observed:
        assert set(timeouts) == {"connect", "read", "write", "pool"}
        assert all(0 < timeout <= 10 for timeout in timeouts.values())


@pytest.mark.asyncio
async def test_caller_deadline_tightens_each_request_timeout() -> None:
    timeouts: list[dict[str, float]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"results": []}, request=request)

    deadline = time.monotonic() + 1
    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    assert (await attempt.search("query", deadline_at=deadline)).status == "EMPTY"
    await attempt.aclose()
    assert len(timeouts) == 1
    assert all(0 < timeout <= 1 for timeout in timeouts[0].values())


@pytest.mark.asyncio
async def test_http_send_deadline_covers_a_blocked_mock_response() -> None:
    request_started = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        request_started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"results": []}, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SearchHttpError) as caught:
            await attempt.search("private-query-sentinel", deadline_at=time.monotonic() + 0.1)
        assert caught.value.code == "TIMEOUT"
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "private-query-sentinel" not in repr(caught.value) + repr(caught.value.args)
        assert request_started.is_set()
        assert len(requests) == 1
    finally:
        await attempt.aclose()


@pytest.mark.asyncio
async def test_ca_bundle_builds_a_verifying_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    ca_paths: list[str | None] = []
    verify_arguments: list[object] = []
    original_context = ssl.create_default_context
    original_client = httpx.AsyncClient

    def context_factory(*, cafile: str | None = None) -> ssl.SSLContext:
        ca_paths.append(cafile)
        return original_context()

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        verify_arguments.append(kwargs["verify"])
        return original_client(**kwargs)

    monkeypatch.setattr(_search_http.ssl, "create_default_context", context_factory)
    monkeypatch.setattr(_search_http.httpx, "AsyncClient", client_factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(200, json=_firecrawl_body([]), request=request)

    attempt = SearchHttpAttempt(
        _config("FIRECRAWL", ca_bundle="trusted-server-ca.pem"),
        transport=httpx.MockTransport(handler),
    )
    assert (await attempt.search("query")).status == "EMPTY"
    await attempt.aclose()
    assert ca_paths == ["trusted-server-ca.pem"]
    assert len(verify_arguments) == 1
    assert isinstance(verify_arguments[0], ssl.SSLContext)


@pytest.mark.asyncio
async def test_tls_construction_failure_is_sanitized(tmp_path: Path) -> None:
    missing_ca = tmp_path / "missing-private-ca.pem"
    attempt = SearchHttpAttempt(
        _config("FIRECRAWL", ca_bundle=str(missing_ca)),
        transport=httpx.MockTransport(_counting_response([])),
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("private-query-sentinel")
    await attempt.aclose()
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private" not in repr(caught.value) + repr(caught.value.args)


@pytest.mark.asyncio
async def test_pre_send_deadline_gate_prevents_client_request() -> None:
    requests: list[httpx.Request] = []
    clock_values = iter([0.0, 10.0])
    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        ),
        clock=lambda: next(clock_values),
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "TIMEOUT"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deadline_at", [float("nan"), float("inf"), True], ids=["nan", "infinity", "bool"]
)
async def test_nonfinite_or_bool_caller_deadline_is_rejected(deadline_at: float) -> None:
    requests: list[httpx.Request] = []
    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        ),
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query", deadline_at=deadline_at)
    await attempt.aclose()
    assert caught.value.code == "INVALID_QUERY"
    assert requests == []


@pytest.mark.asyncio
async def test_results_are_capped_after_validating_the_complete_array() -> None:
    rows = [
        _searx_row(title=f"title {index}", url=f"https://result.example/{index}")
        for index in range(9)
    ]
    result, requests = await _run_search(_config(), {"results": rows})

    assert len(requests) == 1
    assert len(result.hits) == 8
    assert [hit.title for hit in result.hits] == [f"title {index}" for index in range(8)]
    assert result.results_truncated is True


@pytest.mark.asyncio
async def test_exactly_eight_results_are_not_marked_truncated() -> None:
    rows = [_searx_row(title=f"title {index}") for index in range(8)]
    result, requests = await _run_search(_config(), {"results": rows})
    assert len(requests) == 1
    assert len(result.hits) == 8
    assert result.results_truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_index", [0, 8], ids=["first-row", "ninth-row"])
async def test_any_invalid_row_in_full_array_rejects_the_response(bad_index: int) -> None:
    rows: list[object] = [_searx_row(title=f"title {index}") for index in range(9)]
    rows[bad_index] = {"title": "missing URL"}
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"results": rows}, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_all_invalid_rows_are_schema_invalid_instead_of_empty() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"results": [None, {}, {"title": 5}]}, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "length", "truncated"),
    [
        (None, 0, False),
        ("", 0, False),
        ("a" * 1200, 1200, False),
        ("a" * 1201, 1201, True),
        ("🙂" * 1201, 1201, True),
    ],
    ids=["missing", "empty", "at-cap", "over-cap", "unicode-codepoints"],
)
async def test_snippet_is_plain_text_with_codepoint_cap(
    content: str | None, length: int, truncated: bool
) -> None:
    row = _searx_row()
    if content is None:
        row.pop("content")
    else:
        row["content"] = content
    result, requests = await _run_search(_config(), {"results": [row]})

    assert len(requests) == 1
    assert result.hits[0].content == (content or "")[:1200]
    assert result.hits[0].original_length == length
    assert result.hits[0].truncated is truncated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_content", [None, False, 2, {}, []], ids=["null", "bool", "number", "object", "array"]
)
async def test_present_non_string_snippet_is_invalid(bad_content: object) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(
            200, json={"results": [_searx_row(content=bad_content)]}, request=request
        )

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        _searx_row(title="   "),
        _searx_row(title=1),
        _searx_row(title="bad\x00title"),
        _searx_row(title="bad\x85title"),
        _searx_row(url="relative/path"),
        _searx_row(url="//result.example/path"),
        _searx_row(url="javascript:alert(1)"),
        _searx_row(url="https://user:pass@result.example/path"),
        _searx_row(url="https://result.example:bad/path"),
        _searx_row(url="https://result.example/a b"),
        _searx_row(url="https://result.example/%0d%0aX"),
        _searx_row(url="https://result.example/%"),
        _searx_row(url="https://result.example/a%2Fb"),
        _searx_row(url="https://result.example/a%252Fb"),
        _searx_row(url="https://result.example/a%5Cb"),
        _searx_row(url="https://result.example/\ud800"),
    ],
    ids=[
        "blank-title",
        "nonstring-title",
        "control-title",
        "control-c1-title",
        "relative-url",
        "network-path",
        "scheme",
        "userinfo",
        "port",
        "space",
        "encoded-crlf",
        "bad-percent-escape",
        "encoded-slash",
        "double-encoded-slash",
        "encoded-backslash",
        "invalid-unicode",
    ],
)
async def test_invalid_result_rows_reject_the_whole_response(row: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        payload = json.dumps({"results": [row]}, ensure_ascii=True).encode("utf-8")
        return httpx.Response(200, content=payload, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b'{"results":[],"results":[]}',
        b'{"results":[NaN]}',
        b"\xff",
        b"[]",
        b'{"results":{}}',
    ],
    ids=[
        "malformed-json",
        "duplicate-key",
        "nonfinite",
        "invalid-utf8",
        "wrong-root",
        "wrong-container",
    ],
)
async def test_malformed_provider_payload_is_schema_invalid(payload: bytes) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, content=payload, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "success", [False, 1, "true", None], ids=["false", "int-one", "string", "null"]
)
async def test_firecrawl_requires_literal_true_success(success: object) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"success": success, "data": {"web": []}}, request=request)

    attempt = SearchHttpAttempt(_config("FIRECRAWL"), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "data": {"web": {}}},
        {"success": True, "data": []},
        {"success": True},
    ],
    ids=["web-container", "data-container", "missing-data"],
)
async def test_firecrawl_invalid_containers_are_schema_invalid(payload: object) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json=payload, request=request)

    attempt = SearchHttpAttempt(_config("FIRECRAWL"), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "SCHEMA_INVALID"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "RATE_LIMITED"),
        (500, "UPSTREAM_5XX"),
        (599, "UPSTREAM_5XX"),
        (401, "AUTH_REJECTED"),
        (403, "AUTH_REJECTED"),
        (302, "HTTP_REJECTED"),
        (204, "HTTP_REJECTED"),
    ],
)
async def test_http_status_is_classified_without_retry_or_redirect(
    status: int, expected: str
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(
            status, headers={"Location": "https://outside.example/"}, request=request
        )

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == expected
    assert caught.value.status == status
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ConnectError("private-connect-sentinel"), "UNAVAILABLE"),
        (httpx.ReadTimeout("private-timeout-sentinel"), "TIMEOUT"),
    ],
)
async def test_transport_errors_are_safe_and_sent_once(
    exc: Exception,
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        raise exc

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("private-query-sentinel")
    await attempt.aclose()
    assert caught.value.code == expected
    assert len(requests) == 1
    assert "private" not in repr(caught.value)
    assert "private" not in repr(caught.value.args)
    assert "private" not in caplog.text
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["private-auth-sentinel", ""], ids=["configured", "empty"])
async def test_searxng_rejects_credentials_before_sending(credential: str) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"results": []}, request=request)

    with pytest.raises(SearchHttpError) as caught:
        SearchHttpAttempt(
            _config(credential=credential),
            transport=httpx.MockTransport(handler),
        )
    assert caught.value.code == "UNSUPPORTED_CONFIG"
    assert requests == []


@pytest.mark.asyncio
async def test_invalid_attempt_base_url_fails_before_tls_or_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_make_client = _search_http._make_client

    def count_client_build(
        ca_bundle: str | None, transport: httpx.AsyncBaseTransport | None
    ) -> httpx.AsyncClient:
        events.append("client")
        return original_make_client(ca_bundle, transport)

    async def handler(request: httpx.Request) -> httpx.Response:
        events.append("request")
        await request.aread()
        return httpx.Response(200, json={"results": []}, request=request)

    monkeypatch.setattr(_search_http, "_make_client", count_client_build)
    with pytest.raises(SearchHttpError) as caught:
        SearchHttpAttempt(
            _config(base_url="https://private-origin.example/search"),
            transport=httpx.MockTransport(handler),
        )
    assert caught.value.code == "INVALID_CONFIG"
    assert events == []


@pytest.mark.asyncio
async def test_config_and_error_repr_do_not_disclose_secrets_or_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-auth-sentinel"
    config = _config("FIRECRAWL", credential=secret)
    assert secret not in repr(config)
    attempt = SearchHttpAttempt(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(403))
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("private-query-sentinel")
    await attempt.aclose()
    combined = str(caught.value) + repr(caught.value) + repr(caught.value.args) + caplog.text
    assert secret not in combined
    assert "private-query-sentinel" not in combined
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("validate_method", ["construct", "model_validate"])
def test_invalid_config_validation_does_not_retain_input_or_exception(
    validate_method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {
        "provider": "private-provider-sentinel",
        "base_url": "https://private-host-sentinel/",
        "credential": "private-auth-sentinel",
    }
    with pytest.raises(SearchHttpError) as caught:
        if validate_method == "construct":
            SearchSourceConfig(**payload)
        else:
            SearchSourceConfig.model_validate(payload)
    error = caught.value
    combined = str(error) + repr(error) + repr(error.args) + repr(error.__dict__) + caplog.text
    assert error.provider is None
    assert error.code == "INVALID_CONFIG"
    assert error.__cause__ is None and error.__context__ is None
    assert all(sentinel not in combined for sentinel in payload.values())


def test_invalid_unhashable_provider_is_sanitized() -> None:
    with pytest.raises(SearchHttpError) as caught:
        SearchSourceConfig(
            provider=["private-provider-sentinel"],
            base_url="https://search.example",
        )
    assert caught.value.provider is None
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private-provider-sentinel" not in repr(caught.value) + repr(caught.value.args)


@pytest.mark.parametrize("validate_method", ["construct", "model_validate"])
def test_invalid_secret_field_does_not_leak_validation_context(validate_method: str) -> None:
    payload = {
        "provider": "FIRECRAWL",
        "base_url": "https://search.example",
        "credential": " private-auth-sentinel ",
    }
    with pytest.raises(SearchHttpError) as caught:
        if validate_method == "construct":
            SearchSourceConfig(**payload)
        else:
            SearchSourceConfig.model_validate(payload)
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private-auth-sentinel" not in repr(caught.value) + repr(caught.value.args)


@pytest.mark.parametrize(
    "credential",
    ["", " private-auth-sentinel ", "private\rsecret", "private\nsecret"],
)
def test_firecrawl_rejects_malformed_credentials_safely(credential: str) -> None:
    with pytest.raises(SearchHttpError) as caught:
        _config("FIRECRAWL", credential=credential)
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private-auth-sentinel" not in repr(caught.value) + repr(caught.value.args)


@pytest.mark.asyncio
async def test_poison_client_defaults_and_set_cookie_do_not_reach_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Headers] = []
    transport = httpx.MockTransport(lambda request: _cookie_response(request, observed))
    original_make_client = _search_http._make_client

    def poisoned_client(
        ca_bundle: str | None, provided_transport: httpx.AsyncBaseTransport | None
    ) -> httpx.AsyncClient:
        actual_transport = provided_transport if provided_transport is not None else transport
        # Use the normal configured verification and network policy, but supply
        # hostile client defaults to prove the actual Request boundary overrides them.
        return httpx.AsyncClient(
            transport=actual_transport,
            verify=ssl.create_default_context(cafile=ca_bundle),
            follow_redirects=False,
            trust_env=False,
            headers={"X-Poison": "poison-default", "Cookie": "poison-cookie"},
            auth=("poison-user", "poison-password"),
            cookies={"poison": "cookie"},
        )

    monkeypatch.setattr(_search_http, "_make_client", poisoned_client)
    attempt = SearchHttpAttempt(_config("FIRECRAWL"), transport=transport)
    result = await attempt.search("query")
    await attempt.aclose()
    monkeypatch.setattr(_search_http, "_make_client", original_make_client)
    assert result.status == "EMPTY"
    assert len(observed) == 1
    assert not ({"authorization", "cookie", "x-poison", "x-api-key"} & set(observed[0]))


async def _cookie_response(request: httpx.Request, observed: list[httpx.Headers]) -> httpx.Response:
    await request.aread()
    observed.append(request.headers)
    return httpx.Response(
        200, json=_firecrawl_body([]), headers={"Set-Cookie": "session=poison"}, request=request
    )


def _counting_response(requests: list[httpx.Request]) -> Any:
    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"results": []}, request=request)

    return handler


@pytest.mark.parametrize(
    ("provider", "value", "expected"),
    [
        ("SEARXNG", "https://search.example/", "https://search.example"),
        ("SEARXNG", "https://SEARCH.example/proxy/", "https://search.example/proxy"),
        ("SEARXNG", "https://search.example:443/proxy", "https://search.example/proxy"),
        ("SEARXNG", "https://search.example:8443/proxy", "https://search.example:8443/proxy"),
        ("SEARXNG", "https://search.example/proxy%20name/", "https://search.example/proxy%20name"),
        ("FIRECRAWL", "https://search.example/proxy/", "https://search.example/proxy"),
        ("SEARXNG", "http://localhost:8080/root", "http://localhost:8080/root"),
        ("SEARXNG", "http://127.2.3.4/root", "http://127.2.3.4/root"),
        ("SEARXNG", "http://[::1]/root", "http://[::1]/root"),
    ],
)
def test_search_base_url_normalizes_origin_and_keeps_prefix(
    provider: str, value: str, expected: str
) -> None:
    assert normalize_search_base_url(provider, value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://search.example",
        "https://user@search.example",
        "https://search.example?",
        "https://search.example#",
        "https://search.example:",
        "https://search.example:bad",
        "https://search.example/search",
        "https://search.example/v2",
        "https://search.example/v2/search",
        "https://search.example/a/../b",
        "https://search.example/%2e%2e/b",
        "https://search.example/a%2fb",
        "https://search.example/a%252fb",
        "https://search.example/a%5cb",
        "https://search.example/a\\b",
        "https://search.example/a b",
        "https://search.example/\npath",
        "https://search.example/%GG",
        "https://search.example/\ud800",
    ],
)
def test_unsafe_search_base_url_is_rejected(value: str) -> None:
    with pytest.raises(SearchHttpError) as caught:
        normalize_search_base_url("SEARXNG", value)
    assert caught.value.code == "INVALID_CONFIG"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", None, 12], ids=["empty", "none", "wrong-type"])
async def test_invalid_query_fails_without_request(query: object) -> None:
    requests: list[httpx.Request] = []
    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        ),
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search(query)  # type: ignore[arg-type]
    await attempt.aclose()
    assert caught.value.code == "INVALID_QUERY"
    assert requests == []


@pytest.mark.asyncio
async def test_new_close_uses_no_client_and_prevents_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    client_builds: list[None] = []
    original_make_client = _search_http._make_client

    def count_client_build(
        ca_bundle: str | None, transport: httpx.AsyncBaseTransport | None
    ) -> httpx.AsyncClient:
        client_builds.append(None)
        return original_make_client(ca_bundle, transport)

    monkeypatch.setattr(_search_http, "_make_client", count_client_build)
    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        ),
    )
    await attempt.aclose()
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "INVALID_USAGE"
    assert requests == []
    assert client_builds == []


@pytest.mark.asyncio
async def test_done_close_is_idempotent_and_closes_transport_once() -> None:
    requests: list[httpx.Request] = []
    transport = _CountingMockTransport(_counting_response(requests))
    attempt = SearchHttpAttempt(_config(), transport=transport)
    assert (await attempt.search("query")).status == "EMPTY"
    await asyncio.gather(attempt.aclose(), attempt.aclose(), attempt.aclose())
    await attempt.aclose()
    assert len(requests) == 1
    assert transport.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("abort_kind", ["timeout", "cancel"], ids=["deadline", "caller-cancel"])
async def test_tls_executor_timeout_or_cancel_drains_late_client(
    abort_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tls_started = threading.Event()
    tls_release = threading.Event()
    requests: list[httpx.Request] = []
    transport = _CountingMockTransport(_counting_response(requests))
    original_make_client = _search_http._make_client

    def blocked_tls(
        ca_bundle: str | None, provided_transport: httpx.AsyncBaseTransport | None
    ) -> httpx.AsyncClient:
        tls_started.set()
        tls_release.wait()
        return original_make_client(ca_bundle, provided_transport)

    monkeypatch.setattr(_search_http, "_make_client", blocked_tls)
    attempt = SearchHttpAttempt(_config(), transport=transport)
    search_task = asyncio.create_task(attempt.search("query", deadline_at=time.monotonic() + 0.1))
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(asyncio.to_thread(tls_started.wait), timeout=2)
        if abort_kind == "timeout":
            with pytest.raises(SearchHttpError) as caught:
                await search_task
            assert caught.value.code == "TIMEOUT"
        else:
            search_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await search_task
        assert requests == []
        close_task = asyncio.create_task(attempt.aclose())
        await asyncio.sleep(0)
        assert not close_task.done()
    finally:
        tls_release.set()
        if not search_task.done():
            search_task.cancel()
            await asyncio.gather(search_task, return_exceptions=True)
        if close_task is not None:
            await close_task
        else:
            await attempt.aclose()
    assert requests == []
    assert transport.close_count == 1


@pytest.mark.asyncio
async def test_cancelled_parser_waits_for_single_close_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_started = threading.Event()
    parser_release = threading.Event()
    requests: list[httpx.Request] = []
    transport = _CountingMockTransport(_counting_response(requests))
    original_parser = _search_http._parse_response

    def blocked_parser(provider: str, payload: bytes) -> SearchHttpResult:
        parser_started.set()
        parser_release.wait()
        return original_parser(provider, payload)

    monkeypatch.setattr(_search_http, "_parse_response", blocked_parser)
    attempt = SearchHttpAttempt(_config(), transport=transport)
    search_task = asyncio.create_task(attempt.search("query"))
    close_tasks: list[asyncio.Task[None]] = []
    try:
        await asyncio.wait_for(asyncio.to_thread(parser_started.wait), timeout=2)
        search_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search_task
        close_tasks = [asyncio.create_task(attempt.aclose()) for _ in range(3)]
        await asyncio.sleep(0)
        assert all(not task.done() for task in close_tasks)
        close_tasks[0].cancel()
        close_tasks[0].cancel()
        await asyncio.gather(close_tasks[0], return_exceptions=True)
    finally:
        parser_release.set()
        if not search_task.done():
            search_task.cancel()
            await asyncio.gather(search_task, return_exceptions=True)
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        else:
            await attempt.aclose()
    assert len(requests) == 1
    assert transport.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_response", [False, True], ids=["late-valid", "late-invalid"])
async def test_parser_timeout_keeps_late_future_private_and_close_drains(
    bad_response: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_started = threading.Event()
    parser_release = threading.Event()
    requests: list[httpx.Request] = []
    original_parser = _search_http._parse_response

    def blocked_parser(provider: str, payload: bytes) -> SearchHttpResult:
        parser_started.set()
        parser_release.wait()
        return original_parser(provider, payload)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        payload: object = {"results": [None]} if bad_response else {"results": [_searx_row()]}
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(_search_http, "_parse_response", blocked_parser)
    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(attempt.search("query", deadline_at=time.monotonic() + 0.1))
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(asyncio.to_thread(parser_started.wait), timeout=2)
        with pytest.raises(SearchHttpError) as caught:
            await task
        assert caught.value.code == "TIMEOUT"
        assert len(requests) == 1
        close_task = asyncio.create_task(attempt.aclose())
        await asyncio.sleep(0)
        assert not close_task.done()
    finally:
        parser_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if close_task is not None:
            await close_task
        else:
            await attempt.aclose()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_final_clock_gate_rejects_result_after_parser_finishes() -> None:
    requests: list[httpx.Request] = []
    clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 11.0])

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"results": []}, request=request)

    attempt = SearchHttpAttempt(
        _config(), transport=httpx.MockTransport(handler), clock=lambda: next(clock_values)
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("query")
    await attempt.aclose()
    assert caught.value.code == "TIMEOUT"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_running_close_rejects_without_interfering_with_search() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"results": [_searx_row()]}, request=request)

    transport = _CountingMockTransport(handler)
    attempt = SearchHttpAttempt(_config(), transport=transport)
    task = asyncio.create_task(attempt.search("query"))
    await entered.wait()
    with pytest.raises(SearchHttpError) as caught:
        await attempt.aclose()
    assert caught.value.code == "INVALID_USAGE"
    assert not task.done()
    assert len(requests) == 1
    assert transport.close_count == 0
    release.set()
    assert (await task).status == "SUCCESS"
    await asyncio.gather(attempt.aclose(), attempt.aclose())
    assert transport.close_count == 1


@pytest.mark.asyncio
async def test_second_search_is_rejected_without_a_second_send() -> None:
    requests: list[httpx.Request] = []
    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(200, json={"results": []}, request=request)
            )
        ),
    )
    assert (await attempt.search("first")).status == "EMPTY"
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search("second")
    await attempt.aclose()
    assert caught.value.code == "INVALID_USAGE"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_close_drains() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"results": []}, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(attempt.search("query"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await attempt.aclose()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_pre_send_callback_runs_before_http_and_allows_send() -> None:
    requests: list[httpx.Request] = []
    order: list[str] = []

    async def pre_send() -> None:
        order.append("pre_send")

    async def handler(request: httpx.Request) -> httpx.Response:
        order.append("send")
        requests.append(request)
        await request.aread()
        return httpx.Response(200, json={"results": []}, request=request)

    attempt = SearchHttpAttempt(_config(), transport=httpx.MockTransport(handler))
    result = await attempt.search("exact-query", pre_send=pre_send)
    await attempt.aclose()
    assert result.status == "EMPTY"
    assert order == ["pre_send", "send"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_pre_send_rejection_prevents_client_request() -> None:
    requests: list[httpx.Request] = []

    async def pre_send() -> None:
        raise RuntimeError("denied-sentinel")

    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"results": []})
        ),
    )
    with pytest.raises(RuntimeError, match="denied-sentinel"):
        await attempt.search("query", pre_send=pre_send)
    await attempt.aclose()
    assert requests == []


@pytest.mark.asyncio
async def test_pre_send_timeout_maps_to_timeout_with_zero_send() -> None:
    requests: list[httpx.Request] = []

    async def pre_send() -> None:
        await asyncio.sleep(60)

    attempt = SearchHttpAttempt(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"results": []})
        ),
    )
    with pytest.raises(SearchHttpError) as caught:
        await attempt.search(
            "query",
            deadline_at=time.monotonic() + 0.05,
            pre_send=pre_send,
        )
    await attempt.aclose()
    assert caught.value.code == "TIMEOUT"
    assert requests == []
