"""Admin search source configuration, recovery, and single-source diagnostics."""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
from pydantic import (
    Field,
    RootModel,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .i18n import error_message
from .models import SCHEMA_ID, StrictDto
from .search_http import (
    SearchHttpAttempt,
    SearchHttpError,
    SearchSourceConfig,
    normalize_search_base_url,
)

SEARCH_SOURCES = "search_sources"
SEARCH_INTENTS = "search_intents"
SEARCH_RECOVERY = "search_recovery"
SEARCH_DIAGNOSTICS = "search_diagnostics"
CONFIG_KEY = "default"
ORIGIN_RELATION: Literal["UNKNOWN"] = "UNKNOWN"
type SearchRole = Literal["PRIMARY", "SECONDARY"]
type CredentialOp = Literal["KEEP", "REPLACE", "CLEAR"]
type CaOp = Literal["KEEP", "REPLACE", "CLEAR"]
_ROLE_PROVIDER: dict[SearchRole, Literal["SEARXNG", "FIRECRAWL"]] = {
    "PRIMARY": "SEARXNG",
    "SECONDARY": "FIRECRAWL",
}
_EMPLOYEE_GET_KEYS = frozenset(
    {
        "revision",
        "recovery_blocked",
        "primary_ready",
        "secondary_ready",
        "web_search_ready",
    }
)


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _host_error(exc: PluginHostError, actor: PluginActor) -> PluginActionError:
    if exc.code == "conflict":
        return _error(actor, "CONFIG_CONFLICT", 409)
    if exc.code == "unavailable":
        return _error(actor, "PLUGIN_NOT_READY", 409)
    if exc.code == "denied":
        return _error(actor, "ACCESS_DENIED", 403)
    if exc.code == "not_found":
        return _error(actor, "NOT_FOUND", 404)
    return _error(actor, "CONFIG_INVALID", 400)


class SearchSourceView(StrictDto):
    role: SearchRole
    provider: Literal["SEARXNG", "FIRECRAWL"]
    base_url: StrictStr | None = None
    ca_bundle_configured: StrictBool = False
    credential_configured: StrictBool = False
    reported_version: StrictStr | None = None
    origin_relation: Literal["UNKNOWN"] = ORIGIN_RELATION


class SearchDiagnosticView(StrictDto):
    source_role: SearchRole
    status: StrictStr
    code: StrictStr | None = None
    latency_ms: StrictInt | None = None
    hit_count: StrictInt | None = None
    results_truncated: StrictBool | None = None
    observed_revision: StrictInt
    tested_at: StrictStr | None = None


class SearchSettingsOutput(StrictDto):
    revision: StrictInt
    recovery_blocked: StrictBool
    primary: SearchSourceView
    secondary: SearchSourceView
    primary_ready: StrictBool
    secondary_ready: StrictBool
    web_search_ready: StrictBool
    diagnostics: tuple[SearchDiagnosticView, ...] = ()


class SearchSettingsEmployeeOutput(StrictDto):
    """Authenticated non-admin search.get projection — readiness summary only."""

    revision: StrictInt
    recovery_blocked: StrictBool
    primary_ready: StrictBool
    secondary_ready: StrictBool
    web_search_ready: StrictBool


class SearchSettingsGetOutput(RootModel[SearchSettingsOutput | SearchSettingsEmployeeOutput]):
    """search.get action output — admin full view or employee whitelist."""


class SearchSourceSavePart(StrictDto):
    base_url: StrictStr
    ca_op: CaOp = "KEEP"
    ca_bundle: StrictStr | None = None
    credential_op: CredentialOp = "KEEP"
    credential: SecretStr | None = Field(default=None, repr=False)
    reported_version: StrictStr | None = None

    @model_validator(mode="after")
    def _credential_and_ca_rules(self) -> SearchSourceSavePart:
        secret = self.credential.get_secret_value() if self.credential is not None else None
        if self.credential_op == "REPLACE" and (
            secret is None
            or not secret
            or secret != secret.strip()
            or "\r" in secret
            or "\n" in secret
        ):
            raise ValueError("REPLACE requires a non-empty credential")
        if self.credential_op != "REPLACE" and self.credential is not None:
            raise ValueError("credential is only allowed with REPLACE")
        if self.ca_op == "REPLACE":
            if self.ca_bundle is None or not self.ca_bundle:
                raise ValueError("REPLACE requires a non-empty ca_bundle")
        elif self.ca_bundle is not None:
            raise ValueError("ca_bundle is only allowed with REPLACE")
        return self


class SearchSaveInput(StrictDto):
    expected_revision: StrictInt
    primary: SearchSourceSavePart
    secondary: SearchSourceSavePart


class SearchTestInput(StrictDto):
    source_role: SearchRole
    expected_revision: StrictInt
    public_query: StrictStr

    @model_validator(mode="after")
    def _query_non_empty(self) -> SearchTestInput:
        if not self.public_query:
            raise ValueError("public_query must be non-empty")
        return self


class SearchRecoverInput(StrictDto):
    expected_revision: StrictInt


class SearchTestOutput(StrictDto):
    settings: SearchSettingsOutput
    diagnostic: SearchDiagnosticView


class SearchSettingsService:
    """Host-backed search source configuration and admin diagnostics."""

    def __init__(
        self,
        facade: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._facade = facade
        self._transport = transport

    async def get_settings(
        self, actor: PluginActor
    ) -> SearchSettingsOutput | SearchSettingsEmployeeOutput:
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        blocked = intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
        diagnostics: list[SearchDiagnosticView] = []
        if actor.is_admin:
            for role in ("PRIMARY", "SECONDARY"):
                record = await self._facade.get_record(SEARCH_DIAGNOSTICS, role)
                if record is not None:
                    diagnostics.append(self._diagnostic_view(record.payload, sources))
            return self._project(sources, blocked=blocked, diagnostics=tuple(diagnostics))
        return self._project_employee(sources, blocked=blocked)

    async def save(self, actor: PluginActor, request: SearchSaveInput) -> SearchSettingsOutput:
        actor = await self._fresh_admin(actor)
        old = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        self._check_expected_revision(actor, old, request.expected_revision)
        await self._assert_available(actor)

        primary_payload = self._build_source_payload(
            actor, "PRIMARY", request.primary, old.payload if old is not None else None
        )
        secondary_payload, secondary_secret, secondary_op = self._build_secondary(
            actor, request.secondary, old.payload if old is not None else None
        )

        owner_user_id = old.owner_user_id if old is not None else actor.user_id
        credential_owner = actor.user_id
        old_secondary_ref = (
            cast(str | None, old.payload.get("secondary", {}).get("credential_ref"))
            if old is not None
            else None
        )
        planned_staged_ref: str | None = None
        if secondary_op == "REPLACE":
            planned_staged_ref = str(uuid4())
            credential_owner = actor.user_id

        try:
            intent, recovery = await self._start_intent(
                owner_user_id,
                old,
                old_secondary_ref,
                planned_staged_ref,
                credential_owner if planned_staged_ref is not None else None,
                actor=actor,
            )
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

        staged_ref: str | None = None
        activations: tuple[CredentialActivation, ...] = ()
        deletions: list[CredentialDeletion] = []
        secondary_ref = secondary_payload.get("credential_ref")

        if secondary_op == "REPLACE":
            assert secondary_secret is not None and planned_staged_ref is not None
            try:
                staged_ref = await self._facade.stage_credential(
                    credential_owner,
                    {
                        "schema_id": SCHEMA_ID,
                        "version": 1,
                        "api_key": secondary_secret,
                    },
                    credential_id=planned_staged_ref,
                )
            except PluginHostError as exc:
                try:
                    await self._finish_intent(intent, recovery, "ABORTED", None)
                except PluginHostError:
                    raise _error(actor, "ROLLBACK_FAILED", 500) from None
                raise _host_error(exc, actor) from None
            secondary_ref = staged_ref
            activations = (CredentialActivation(staged_ref, credential_owner, 1),)
            if old_secondary_ref is not None and old_secondary_ref != staged_ref:
                old_owner = int(
                    (old.payload.get("secondary") or {}).get(
                        "credential_owner_user_id", owner_user_id
                    )
                )
                deletions.append(CredentialDeletion(old_secondary_ref, old_owner, 2))
        elif secondary_op == "CLEAR" and old_secondary_ref is not None:
            old_owner = int(
                (old.payload.get("secondary") or {}).get("credential_owner_user_id", owner_user_id)
            )
            deletions.append(CredentialDeletion(old_secondary_ref, old_owner, 2))
            secondary_ref = None

        secondary_payload["credential_ref"] = secondary_ref
        if secondary_ref is not None:
            secondary_payload["credential_owner_user_id"] = credential_owner
        else:
            secondary_payload.pop("credential_owner_user_id", None)

        payload = {
            "schema_id": SCHEMA_ID,
            "version": 1,
            "origin_relation": ORIGIN_RELATION,
            "primary": primary_payload,
            "secondary": secondary_payload,
        }
        self._project_candidate(payload, request.expected_revision + 1, blocked=False)

        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        SEARCH_SOURCES,
                        CONFIG_KEY,
                        owner_user_id,
                        payload,
                        request.expected_revision,
                    ),
                    RecordWrite(
                        SEARCH_INTENTS,
                        CONFIG_KEY,
                        intent.owner_user_id,
                        self._intent_payload(intent.payload, "RESOLVED", staged_ref),
                        intent.revision,
                    ),
                    RecordWrite(
                        SEARCH_RECOVERY,
                        recovery.logical_key,
                        recovery.owner_user_id,
                        self._intent_payload(recovery.payload, "RESOLVED", staged_ref),
                        recovery.revision,
                    ),
                ),
                activations,
                credential_deletions=tuple(deletions),
            )
        except PluginHostError as exc:
            if staged_ref is not None:
                if await self._release_staged(staged_ref, credential_owner):
                    try:
                        await self._finish_intent(intent, recovery, "ABORTED", None)
                    except PluginHostError:
                        raise _error(actor, "ROLLBACK_FAILED", 500) from None
                    raise _host_error(exc, actor) from None
                with suppress(PluginHostError):
                    await self._finish_intent(intent, recovery, "BLOCKED", staged_ref)
                raise _error(actor, "ROLLBACK_FAILED", 500) from None
            try:
                await self._finish_intent(intent, recovery, "ABORTED", None)
            except PluginHostError:
                raise _error(actor, "ROLLBACK_FAILED", 500) from None
            raise _host_error(exc, actor) from None
        return self._project(saved[0], blocked=False, diagnostics=())

    async def recover(
        self, actor: PluginActor, request: SearchRecoverInput
    ) -> SearchSettingsOutput:
        actor = await self._fresh_admin(actor)
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        self._check_expected_revision(actor, sources, request.expected_revision)
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        if intent is None or intent.payload.get("state") not in {"PREPARING", "BLOCKED"}:
            raise _error(actor, "CONFIG_INVALID", 400)
        recovery = await self._facade.get_record(
            SEARCH_RECOVERY, str(intent.payload.get("recovery_id") or "")
        )
        if recovery is None:
            recovery_list = await self._facade.list_records(SEARCH_RECOVERY)
            recovery = next(
                (
                    item
                    for item in recovery_list
                    if item.payload.get("recovery_id") == intent.payload.get("recovery_id")
                ),
                None,
            )
        if recovery is None:
            raise _error(actor, "NOT_FOUND", 404)

        staged_ref = intent.payload.get("staged_credential_ref")
        staged_owner = intent.payload.get("staged_credential_owner_user_id")
        released = True
        if isinstance(staged_ref, str) and isinstance(staged_owner, int):
            released = await self._release_staged(staged_ref, staged_owner)
        if not released:
            with suppress(PluginHostError):
                await self._finish_intent(intent, recovery, "BLOCKED", cast(str, staged_ref))
            raise _error(actor, "ROLLBACK_FAILED", 500)

        try:
            await self._finish_intent(intent, recovery, "ABORTED", None)
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None
        refreshed = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        return self._project(refreshed, blocked=False, diagnostics=())

    async def test(self, actor: PluginActor, request: SearchTestInput) -> SearchTestOutput:
        actor = await self._fresh_admin(actor)
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        self._check_expected_revision(actor, sources, request.expected_revision)
        if sources is None:
            raise _error(actor, "NOT_FOUND", 404)
        await self._assert_available(actor)

        role = request.source_role
        provider = _ROLE_PROVIDER[role]
        part = cast(
            dict[str, Any], sources.payload.get("primary" if role == "PRIMARY" else "secondary")
        )
        base_url = part.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise _error(actor, "CONFIG_INVALID", 400)

        credential: SecretStr | None = None
        if provider == "FIRECRAWL":
            ref = part.get("credential_ref")
            owner = part.get("credential_owner_user_id")
            if isinstance(ref, str) and isinstance(owner, int):
                try:
                    material = await self._facade.resolve_credential(ref, owner)
                except PluginHostError as exc:
                    raise _host_error(exc, actor) from None
                secret = material.get("api_key") if isinstance(material, Mapping) else None
                if isinstance(secret, str) and secret:
                    credential = SecretStr(secret)

        config = SearchSourceConfig(
            provider=provider,
            base_url=base_url,
            credential=credential,
            ca_bundle=cast(str | None, part.get("ca_bundle")),
        )
        observed_revision = sources.revision
        started = time.monotonic()

        async def pre_send() -> None:
            fresh = await self._fresh_admin(actor)
            current = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
            if current is None or current.revision != observed_revision:
                raise _error(fresh, "CONFIG_CONFLICT", 409)
            await self._assert_available(fresh)

        attempt = SearchHttpAttempt(config, transport=self._transport)
        diagnostic: SearchDiagnosticView
        try:
            try:
                result = await attempt.search(request.public_query, pre_send=pre_send)
                latency_ms = int((time.monotonic() - started) * 1000)
                diagnostic = SearchDiagnosticView(
                    source_role=role,
                    status=result.status,
                    code=None,
                    latency_ms=latency_ms,
                    hit_count=len(result.hits),
                    results_truncated=result.results_truncated,
                    observed_revision=observed_revision,
                    tested_at=datetime.now(UTC).isoformat(),
                )
            except SearchHttpError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                diagnostic = SearchDiagnosticView(
                    source_role=role,
                    status="ERROR",
                    code=exc.code,
                    latency_ms=latency_ms,
                    hit_count=None,
                    results_truncated=None,
                    observed_revision=observed_revision,
                    tested_at=datetime.now(UTC).isoformat(),
                )
            except PluginActionError:
                raise
        finally:
            with suppress(SearchHttpError):
                await attempt.aclose()

        diag_payload = {
            "schema_id": SCHEMA_ID,
            "version": 1,
            "source_role": diagnostic.source_role,
            "status": diagnostic.status,
            "code": diagnostic.code,
            "latency_ms": diagnostic.latency_ms,
            "hit_count": diagnostic.hit_count,
            "results_truncated": diagnostic.results_truncated,
            "observed_revision": diagnostic.observed_revision,
            "tested_at": diagnostic.tested_at,
        }
        old_diag = await self._facade.get_record(SEARCH_DIAGNOSTICS, role)
        try:
            await self._facade.commit(
                (
                    RecordWrite(
                        SEARCH_DIAGNOSTICS,
                        role,
                        actor.user_id,
                        diag_payload,
                        old_diag.revision if old_diag is not None else 0,
                    ),
                )
            )
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

        sources_after = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        blocked = intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
        diagnostics: list[SearchDiagnosticView] = []
        for diag_role in ("PRIMARY", "SECONDARY"):
            record = await self._facade.get_record(SEARCH_DIAGNOSTICS, diag_role)
            if record is not None:
                diagnostics.append(self._diagnostic_view(record.payload, sources_after))
        settings = self._project(sources_after, blocked=blocked, diagnostics=tuple(diagnostics))
        return SearchTestOutput(settings=settings, diagnostic=diagnostic)

    async def _fresh_admin(self, actor: PluginActor) -> PluginActor:
        try:
            return await self._facade.reauthorize_actor(actor, "admin")
        except PluginHostError as exc:
            raise _host_error(exc, actor) from None

    async def _assert_available(self, actor: PluginActor) -> None:
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
            raise _error(actor, "RECOVERY_BLOCKED", 409)

    def _check_expected_revision(
        self, actor: PluginActor, record: Any | None, expected: int
    ) -> None:
        actual = record.revision if record is not None else 0
        if actual != expected:
            raise _error(actor, "CONFIG_CONFLICT", 409)

    @staticmethod
    def _resolve_ca_bundle(
        part: SearchSourceSavePart, old_part: Mapping[str, Any] | None
    ) -> str | None:
        if part.ca_op == "KEEP":
            previous = (old_part or {}).get("ca_bundle")
            return previous if isinstance(previous, str) else None
        if part.ca_op == "CLEAR":
            return None
        return part.ca_bundle

    def _build_source_payload(
        self,
        actor: PluginActor,
        role: SearchRole,
        part: SearchSourceSavePart,
        old_root: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        provider = _ROLE_PROVIDER[role]
        if role == "PRIMARY" and (part.credential_op == "REPLACE" or part.credential is not None):
            raise _error(actor, "UNSUPPORTED_CONFIG", 400)
        # CLEAR is a no-op for SearXNG (no credential ever stored).
        try:
            base_url = normalize_search_base_url(provider, part.base_url)
        except SearchHttpError:
            raise _error(actor, "CONFIG_INVALID", 400) from None
        old_primary = cast(dict[str, Any], (old_root or {}).get("primary") or {})
        return {
            "provider": provider,
            "base_url": base_url,
            "ca_bundle": self._resolve_ca_bundle(part, old_primary),
            "reported_version": part.reported_version,
            "credential_ref": None,
        }

    def _build_secondary(
        self,
        actor: PluginActor,
        part: SearchSourceSavePart,
        old_root: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None, CredentialOp]:
        old_secondary = cast(dict[str, Any], (old_root or {}).get("secondary") or {})
        try:
            base_url = normalize_search_base_url("FIRECRAWL", part.base_url)
        except SearchHttpError:
            raise _error(actor, "CONFIG_INVALID", 400) from None
        payload: dict[str, Any] = {
            "provider": "FIRECRAWL",
            "base_url": base_url,
            "ca_bundle": self._resolve_ca_bundle(part, old_secondary),
            "reported_version": part.reported_version,
            "credential_ref": old_secondary.get("credential_ref"),
            "credential_owner_user_id": old_secondary.get("credential_owner_user_id"),
        }
        secret: str | None = None
        op = part.credential_op
        if op == "REPLACE":
            assert part.credential is not None
            secret = part.credential.get_secret_value()
            payload["credential_ref"] = None
        elif op == "CLEAR":
            payload["credential_ref"] = None
            payload.pop("credential_owner_user_id", None)
        elif op == "KEEP":
            pass
        else:
            raise _error(actor, "CONFIG_INVALID", 400)
        return payload, secret, op

    async def _start_intent(
        self,
        owner_user_id: int,
        old: Any | None,
        old_credential_ref: str | None,
        staged_credential_ref: str | None,
        staged_credential_owner_user_id: int | None,
        *,
        actor: PluginActor,
    ) -> tuple[Any, Any]:
        prior = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        if prior is not None and prior.payload.get("state") in {"PREPARING", "BLOCKED"}:
            raise _error(actor, "RECOVERY_BLOCKED", 409)
        expected = prior.revision if prior is not None else 0
        recovery_id = str(uuid4())
        payload = {
            "recovery_id": recovery_id,
            "state": "PREPARING",
            "started_at": datetime.now(UTC).isoformat(),
            "retain_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "t0": {
                "sources_revision": old.revision if old is not None else 0,
                "secondary_credential_ref": old_credential_ref,
            },
            "staged_credential_ref": staged_credential_ref,
            "staged_credential_owner_user_id": staged_credential_owner_user_id,
        }
        saved = await self._facade.commit(
            (
                RecordWrite(
                    SEARCH_INTENTS,
                    CONFIG_KEY,
                    prior.owner_user_id if prior is not None else owner_user_id,
                    payload,
                    expected,
                ),
                RecordWrite(SEARCH_RECOVERY, recovery_id, owner_user_id, payload, 0),
            ),
            checks=(
                RecordCheck(
                    SEARCH_SOURCES,
                    CONFIG_KEY,
                    old.revision if old is not None else 0,
                ),
            ),
        )
        return saved[0], saved[1]

    async def _finish_intent(
        self, intent: Any, recovery: Any, state: str, staged_ref: str | None
    ) -> tuple[Any, Any]:
        payload = self._intent_payload(intent.payload, state, staged_ref)
        saved = await self._facade.commit(
            (
                RecordWrite(
                    SEARCH_INTENTS,
                    intent.logical_key,
                    intent.owner_user_id,
                    payload,
                    intent.revision,
                ),
                RecordWrite(
                    SEARCH_RECOVERY,
                    recovery.logical_key,
                    recovery.owner_user_id,
                    payload,
                    recovery.revision,
                ),
            )
        )
        return saved[0], saved[1]

    @staticmethod
    def _intent_payload(
        existing: Mapping[str, Any], state: str, staged_ref: str | None
    ) -> dict[str, Any]:
        return {
            **dict(existing),
            "state": state,
            "staged_credential_ref": (
                (staged_ref or existing.get("staged_credential_ref"))
                if state in {"BLOCKED", "ABORTED"}
                else None
            ),
            "staged_credential_owner_user_id": existing.get("staged_credential_owner_user_id")
            if state in {"BLOCKED", "ABORTED"}
            else None,
            "activated_credential_ref": staged_ref if state == "RESOLVED" else None,
        }

    async def _release_staged(self, credential_ref: str, owner_user_id: int) -> bool:
        try:
            released = await self._facade.release_staged_credential(credential_ref, owner_user_id)
        except PluginHostError:
            released = False
        return cast(bool, released)

    def _project(
        self,
        sources: Any | None,
        *,
        blocked: bool,
        diagnostics: tuple[SearchDiagnosticView, ...],
    ) -> SearchSettingsOutput:
        payload = sources.payload if sources is not None else {}
        revision = sources.revision if sources is not None else 0
        primary = self._source_view("PRIMARY", cast(dict[str, Any], payload.get("primary") or {}))
        secondary = self._source_view(
            "SECONDARY", cast(dict[str, Any], payload.get("secondary") or {})
        )
        primary_ready = bool(primary.base_url) and not blocked
        secondary_ready = bool(secondary.base_url) and not blocked
        return SearchSettingsOutput(
            revision=revision,
            recovery_blocked=blocked,
            primary=primary,
            secondary=secondary,
            primary_ready=primary_ready,
            secondary_ready=secondary_ready,
            web_search_ready=primary_ready and secondary_ready,
            diagnostics=diagnostics,
        )

    def _project_employee(
        self, sources: Any | None, *, blocked: bool
    ) -> SearchSettingsEmployeeOutput:
        admin = self._project(sources, blocked=blocked, diagnostics=())
        return SearchSettingsEmployeeOutput(
            revision=admin.revision,
            recovery_blocked=admin.recovery_blocked,
            primary_ready=admin.primary_ready,
            secondary_ready=admin.secondary_ready,
            web_search_ready=admin.web_search_ready,
        )

    def _project_candidate(
        self, payload: Mapping[str, Any], revision: int, *, blocked: bool
    ) -> SearchSettingsOutput:
        class _Rec:
            def __init__(self) -> None:
                self.revision = revision
                self.payload = dict(payload)

        return self._project(_Rec(), blocked=blocked, diagnostics=())

    @staticmethod
    def _source_view(role: SearchRole, part: Mapping[str, Any]) -> SearchSourceView:
        provider = _ROLE_PROVIDER[role]
        base_url = part.get("base_url")
        return SearchSourceView(
            role=role,
            provider=provider,
            base_url=base_url if isinstance(base_url, str) else None,
            ca_bundle_configured=bool(part.get("ca_bundle")),
            credential_configured=bool(part.get("credential_ref")),
            reported_version=cast(str | None, part.get("reported_version")),
            origin_relation=ORIGIN_RELATION,
        )

    @staticmethod
    def _diagnostic_view(payload: Mapping[str, Any], sources: Any | None) -> SearchDiagnosticView:
        return SearchDiagnosticView(
            source_role=cast(SearchRole, payload["source_role"]),
            status=str(payload["status"]),
            code=cast(str | None, payload.get("code")),
            latency_ms=cast(int | None, payload.get("latency_ms")),
            hit_count=cast(int | None, payload.get("hit_count")),
            results_truncated=cast(bool | None, payload.get("results_truncated")),
            observed_revision=int(payload["observed_revision"]),
            tested_at=cast(str | None, payload.get("tested_at")),
        )
