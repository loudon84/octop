"""Explicit, read-only WeKnora probes for enterprise acceptance evidence."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import ssl
import subprocess
import sys
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx

REQUIRED_ENV = (
    "OCTOP_WEKNORA_BASE_URL",
    "OCTOP_WEKNORA_API_KEY",
    "OCTOP_WEKNORA_VERSION",
    "OCTOP_WEKNORA_IMAGE_DIGEST",
    "OCTOP_WEKNORA_TEST_KB_IDS",
    "OCTOP_WEKNORA_TEST_DOCUMENT_IDS",
    "OCTOP_WEKNORA_PROBE_QUERY",
    "OCTOP_WEKNORA_CONSUMER_IDENTITY",
)
MAX_REQUESTS = 10
REQUEST_TIMEOUT_SECONDS = 30.0
OVERALL_TIMEOUT_SECONDS = 90.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class ProbeConfigError(ValueError):
    """Configuration validation failure without exposing user supplied values."""


@dataclass(frozen=True)
class ProbeConfig:
    base_url: str
    api_key: str
    version: str
    image_digest: str
    kb_ids: tuple[str, str]
    document_ids: tuple[str, str]
    query: str
    consumer_identity: str
    tenant_id: str | None
    ca_bundle: str | None
    ssl_context: ssl.SSLContext | None
    test_http_exception_origin: str | None


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID.fullmatch(value)) and ".." not in value


def _id_pair(environ: dict[str, str], key: str) -> tuple[str, str]:
    try:
        parsed = json.loads(environ[key])
    except (KeyError, json.JSONDecodeError):
        raise ProbeConfigError("INVALID_CONFIG") from None
    if (
        not isinstance(parsed, list)
        or len(parsed) != 2
        or not all(_safe_id(item) for item in parsed)
        or parsed[0] == parsed[1]
    ):
        raise ProbeConfigError("INVALID_CONFIG")
    return parsed[0], parsed[1]


def _origin(parts: SplitResult) -> tuple[tuple[str, str, int], str]:
    url = httpx.URL(f"{parts.scheme}://{parts.netloc}")
    host = url.raw_host.decode("ascii").lower()
    port = url.port
    scheme = url.scheme
    default_port = 80 if scheme == "http" else 443
    effective_port = port if port is not None else default_port
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = f":{effective_port}" if effective_port != default_port else ""
    return (scheme, host, effective_port), f"{scheme}://{rendered_host}{rendered_port}"


def _normalize_test_http_origin(raw: str) -> tuple[tuple[str, str, int], str]:
    try:
        parts = urlsplit(raw)
        if (
            any(char.isspace() for char in raw)
            or "\\" in raw
            or parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.netloc.endswith(":")
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or "?" in raw
            or "#" in raw
        ):
            raise ProbeConfigError("INVALID_CONFIG")
        return _origin(parts)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise ProbeConfigError("INVALID_CONFIG") from None


def _normalize_url(raw: str, test_http_origin: str | None = None) -> tuple[str, str | None]:
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        _ = parts.port
        local = hostname in {"localhost", "127.0.0.1", "::1"} or bool(
            hostname and hostname.endswith(".localhost")
        )
        if hostname:
            with suppress(ValueError):
                local = local or ipaddress.ip_address(hostname).is_loopback
        valid_path = parts.path in {"", "/"} or bool(re.fullmatch(r"(?:/api/v1)+/?", parts.path))
        if (
            parts.scheme not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or "?" in raw
            or "#" in raw
            or not valid_path
        ):
            raise ProbeConfigError("INVALID_CONFIG")
        actual, canonical_origin = _origin(parts)
        exception_origin = None
        if parts.scheme == "http" and not local:
            if not test_http_origin:
                raise ProbeConfigError("INVALID_CONFIG")
            expected, exception_origin = _normalize_test_http_origin(test_http_origin)
            if actual != expected:
                raise ProbeConfigError("INVALID_CONFIG")
            exception_origin = canonical_origin
        return f"{canonical_origin}/api/v1", exception_origin
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise ProbeConfigError("INVALID_CONFIG") from None


def build_config(
    environ: dict[str, str], *, live: bool = True
) -> tuple[ProbeConfig | None, dict[str, Any]] | ProbeConfig:
    """Parse and validate all inputs without creating a network client."""
    missing = [key for key in REQUIRED_ENV if not environ.get(key)]
    if not live:
        missing.insert(0, "--live")
    if missing:
        return None, {"status": "BLOCKED", "missing": missing, "requests": 0}
    try:
        raw_base_url = environ["OCTOP_WEKNORA_BASE_URL"]
        test_http_origin = environ.get("OCTOP_WEKNORA_TEST_HTTP_ORIGIN") or None
        base_url, exception_origin = _normalize_url(
            raw_base_url,
            test_http_origin if raw_base_url.lower().startswith("http://") else None,
        )
        kb_ids = _id_pair(environ, "OCTOP_WEKNORA_TEST_KB_IDS")
        document_ids = _id_pair(environ, "OCTOP_WEKNORA_TEST_DOCUMENT_IDS")
        ca_bundle = environ.get("OCTOP_WEKNORA_CA_BUNDLE") or None
        ssl_context = None
        if ca_bundle:
            # Fail before any network activity if the supplied trust bundle is unusable.
            ssl_context = ssl.create_default_context(cafile=ca_bundle)
        query = environ["OCTOP_WEKNORA_PROBE_QUERY"]
        if not query or not environ["OCTOP_WEKNORA_API_KEY"]:
            raise ProbeConfigError("INVALID_CONFIG")
        return ProbeConfig(
            base_url=base_url,
            api_key=environ["OCTOP_WEKNORA_API_KEY"],
            version=environ["OCTOP_WEKNORA_VERSION"],
            image_digest=environ["OCTOP_WEKNORA_IMAGE_DIGEST"],
            kb_ids=kb_ids,
            document_ids=document_ids,
            query=query,
            consumer_identity=environ["OCTOP_WEKNORA_CONSUMER_IDENTITY"],
            tenant_id=environ.get("OCTOP_WEKNORA_TENANT_ID") or None,
            ca_bundle=ca_bundle,
            ssl_context=ssl_context,
            test_http_exception_origin=exception_origin,
        )
    except (ProbeConfigError, OSError, ssl.SSLError):
        return None, {"status": "BLOCKED", "missing": [], "error": "INVALID_CONFIG", "requests": 0}


def _local_sha() -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            revision = "unavailable"
    except (OSError, subprocess.SubprocessError):
        revision = "unavailable"
    return revision


async def _report_base(config: ProbeConfig) -> dict[str, Any]:
    revision = await asyncio.get_running_loop().run_in_executor(None, _local_sha)
    report = {
        "status": "FAILED",
        "local_octop_sha": revision,
        "remote": {
            "version": config.version,
            "image_digest": config.image_digest,
            "consumer_identity": config.consumer_identity,
        },
        "checks": {},
        "observed": {},
        "requests": 0,
        "elapsed_seconds": 0.0,
    }
    if config.test_http_exception_origin is not None:
        report["test_http_exception"] = True
        report["test_http_exception_origin"] = config.test_http_exception_origin
    return report


async def run_probe(
    config: ProbeConfig,
    *,
    client: httpx.AsyncClient | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_factory: Callable[[float | None], asyncio.Timeout] = asyncio.timeout,
) -> dict[str, Any]:
    """Run seven bounded, read-only requests using an injectable HTTP client."""
    report = await _report_base(config)
    own_client = client is None
    if own_client:
        verify: ssl.SSLContext | bool = config.ssl_context or True
        client = httpx.AsyncClient(
            verify=verify,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    assert client is not None
    headers = {"Accept": "application/json", "X-API-Key": config.api_key}
    if config.tenant_id:
        headers["X-Tenant-ID"] = config.tenant_id
    started = monotonic()

    async def request(
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if report["requests"] >= MAX_REQUESTS:
            raise _ProbeFailure("REQUEST_LIMIT")
        report["requests"] += 1
        try:
            response = await client.request(
                method,
                f"{config.base_url}{path}",
                params=params,
                json=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise _ProbeFailure("TIMEOUT") from None
        except httpx.HTTPError:
            raise _ProbeFailure("HTTP_ERROR") from None
        if 300 <= response.status_code < 400:
            raise _ProbeFailure("REDIRECT")
        if response.status_code in {401, 403}:
            raise _ProbeFailure(f"HTTP_{response.status_code}")
        if not response.is_success:
            raise _ProbeFailure("HTTP_ERROR")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise _ProbeFailure("SCHEMA_DRIFT") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise _ProbeFailure("SCHEMA_DRIFT")
        return payload.get("data")

    async def check(name: str, operation: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        try:
            value = await operation()
            report["checks"][name] = {"status": "PASS"}
            return value
        except _ProbeFailure as exc:
            report["checks"][name] = {"status": "FAILED", "error": exc.code}
            raise

    try:
        async with timeout_factory(OVERALL_TIMEOUT_SECONDS):
            bases = await check("list", lambda: request("GET", "/knowledge-bases"))
            if not isinstance(bases, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                for row in bases
            ):
                raise _ProbeFailure("SCHEMA_DRIFT", check_name="list")
            visible = {row["id"] for row in bases}
            report["observed"]["knowledge_bases"] = len(bases)
            report["observed"]["knowledge_base_fields"] = sorted(
                {field for row in bases for field in row}
            )
            absent = [item for item in config.kb_ids if item not in visible]
            if absent:
                report["checks"]["list"] = {"status": "FAILED", "error": "KB_NOT_VISIBLE"}
                raise _ProbeFailure("KB_NOT_VISIBLE")
            for index, kb_id in enumerate(config.kb_ids):
                name = f"search_kb_{'one' if index == 0 else 'two'}"
                results = await check(
                    name,
                    partial(
                        request,
                        "POST",
                        "/knowledge-search",
                        params={"resource_urls": "handle"},
                        body={"query": config.query, "knowledge_base_ids": [kb_id]},
                    ),
                )
                if not isinstance(results, list) or any(
                    not isinstance(row, dict) for row in results
                ):
                    raise _ProbeFailure("SCHEMA_DRIFT", check_name=name)
                report["checks"][name]["status"] = "PASS" if results else "EMPTY"
                report["observed"][name] = len(results)
            for index, document_id in enumerate(config.document_ids):
                kb_id = config.kb_ids[index]
                name = f"document_{'one' if index == 0 else 'two'}"
                detail = await check(
                    f"{name}_detail",
                    partial(request, "GET", f"/knowledge/{document_id}"),
                )
                if not isinstance(detail, dict):
                    raise _ProbeFailure("SCHEMA_DRIFT", check_name=f"{name}_detail")
                report["observed"][f"{name}_detail_fields"] = sorted(detail)
                associated_kb = detail.get("kb_id") or detail.get("knowledge_base_id")
                if associated_kb is not None and associated_kb != kb_id:
                    raise _ProbeFailure("DOCUMENT_KB_MISMATCH", check_name=f"{name}_detail")
                chunks = await check(
                    f"{name}_chunks",
                    partial(
                        request,
                        "GET",
                        f"/chunks/{document_id}",
                        params={"page": 1, "page_size": 20},
                    ),
                )
                if not isinstance(chunks, list) or any(not isinstance(row, dict) for row in chunks):
                    raise _ProbeFailure("SCHEMA_DRIFT", check_name=f"{name}_chunks")
                report["observed"][f"{name}_chunks"] = len(chunks)
                report["observed"][f"{name}_chunk_fields"] = sorted(
                    {field for row in chunks for field in row}
                )
            report["status"] = "PASS"
    except TimeoutError:
        report["checks"].setdefault("overall", {"status": "FAILED", "error": "TIMEOUT"})
    except _ProbeFailure as exc:
        report["checks"][exc.check_name or "request"] = {
            "status": "FAILED",
            "error": exc.code,
        }
    except asyncio.CancelledError:
        raise
    except Exception:
        report["checks"].setdefault("probe", {"status": "FAILED", "error": "INTERNAL_ERROR"})
    finally:
        report["elapsed_seconds"] = round(max(0.0, monotonic() - started), 3)
        if own_client:
            await client.aclose()
    return report


class _ProbeFailure(Exception):
    def __init__(self, code: str, *, check_name: str | None = None):
        self.code = code
        self.check_name = check_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly allow HTTP requests")
    parser.add_argument("--output", type=Path, help="write JSON evidence report to this file")
    args = parser.parse_args(argv)
    parsed = build_config(dict(__import__("os").environ), live=args.live)
    if isinstance(parsed, tuple):
        _, report = parsed
        exit_code = 2
    else:
        try:
            report = asyncio.run(run_probe(parsed))
            exit_code = 0 if report["status"] == "PASS" else 1
        except Exception:
            report = {"status": "FAILED", "requests": 0, "error": "INTERNAL_ERROR"}
            exit_code = 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
