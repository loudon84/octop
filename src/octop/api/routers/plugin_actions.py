"""Authenticated, typed HTTP bridge to registered plugin host capabilities."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import current_user, get_server
from octop.infra.agents.plugins.host import PluginHostFacade
from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    PluginHostError,
    PluginHostExecutionError,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.utils.locale import resolve_locale


class _SafePluginRoute(APIRoute):
    """Do not let FastAPI's default 422 response echo plugin request input."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise OctopError.localized(ErrorCode.PLUGIN_ACTION_INVALID) from None

        return safe_handler


router = APIRouter(route_class=_SafePluginRoute)


class PluginActionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, description="Agent scope requested by the client")
    thread_id: str | None = Field(default=None, description="Thread scope requested by the client")
    operation_id: str | None = Field(default=None, description="Opaque operation correlation id")


class PluginActionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal[1] = Field(description="Plugin Host API version")
    context: PluginActionContext | None = Field(
        default=None, description="Untrusted scope identifiers checked against the current user"
    )
    payload: dict[str, Any] = Field(description="Action input matching its registered input schema")


class PluginActionResult(BaseModel):
    api_version: Literal[1]
    result: dict[str, Any]


class PluginActionDescriptor(BaseModel):
    name: str
    summary: str
    permission: Literal["authenticated", "admin", "agent_owner"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class PluginActionCatalog(BaseModel):
    api_version: Literal[1]
    actions: list[PluginActionDescriptor]


class PluginRequirementReadinessResponse(BaseModel):
    api_version: Literal[1] = Field(description="Plugin Host API version")
    required_plugins: dict[str, str] = Field(
        description="Effective plugin ID to exact version requirements for the requested Agent scope"
    )
    unavailable_plugins: list[str] = Field(
        description="Required plugin IDs missing, version-mismatched, or disabled for this Agent"
    )


def _facade(server: OctopServer, plugin_id: str) -> PluginHostFacade:
    host = server.plugin_host
    if host is None:
        raise OctopError.localized(ErrorCode.PLUGIN_HOST_UNAVAILABLE) from None
    try:
        return host.facade(plugin_id)
    except PluginHostError as exc:
        raise _host_error(exc) from None


def _host_error(error: PluginHostError) -> OctopError:
    mapping = {
        "unavailable": ErrorCode.PLUGIN_HOST_UNAVAILABLE,
        "conflict": ErrorCode.PLUGIN_STATE_CONFLICT,
        "denied": ErrorCode.FORBIDDEN,
        "invalid": ErrorCode.PLUGIN_ACTION_INVALID,
        "not_found": ErrorCode.NOT_FOUND,
    }
    return OctopError.localized(mapping[error.code])


def _plugin_error(error: PluginActionError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        },
    )


def _actor(user: Any, request: Request, context: PluginActionContext | None) -> PluginActor:
    headers = request.headers
    accept_language = headers.get("accept-language") or headers.get("Accept-Language")
    explicit_locale = None
    if accept_language:
        explicit_locale = accept_language.split(",")[0].strip().split(";")[0]
    return PluginActor(
        user_id=user.id,
        is_admin=user.is_admin,
        permissions=frozenset(user.permissions),
        agent_id=context.agent_id if context else None,
        thread_id=context.thread_id if context else None,
        operation_id=context.operation_id if context else None,
        locale=str(resolve_locale(user_locale=user.locale, explicit=explicit_locale)),
    )


@router.get(
    "/requirements",
    response_model=PluginRequirementReadinessResponse,
    summary="Read plugin requirement readiness",
    description=(
        "Returns persisted requirements visible to an authenticated user with access to the Agent. "
        "Without thread_id, only Agent declarations apply; with a real thread_id, its declarations "
        "are merged with Agent declarations and the thread must belong to the current user. Missing "
        "plugins remain listed. Availability is a local hint; turn admission still uses the Host gate."
    ),
)
async def read_plugin_requirements(
    request: Request,
    agent_id: str = Query(description="Opaque public Agent identifier"),
    thread_id: str | None = Query(
        default=None, description="Optional real thread identifier owned by the current user"
    ),
    server: OctopServer = Depends(get_server),
    user: Any = Depends(current_user),
) -> PluginRequirementReadinessResponse:
    host = server.plugin_host
    if host is None:
        raise OctopError.localized(ErrorCode.PLUGIN_HOST_UNAVAILABLE) from None
    actor = _actor(
        user,
        request,
        PluginActionContext(agent_id=agent_id, thread_id=thread_id),
    )
    try:
        readiness = await host.read_requirements(actor)
    except PluginHostError as exc:
        raise _host_error(exc) from None
    return PluginRequirementReadinessResponse(
        api_version=1,
        required_plugins=dict(readiness.required_plugins),
        unavailable_plugins=list(readiness.unavailable_plugins),
    )


@router.get(
    "/{plugin_id}/actions",
    response_model=PluginActionCatalog,
    summary="List registered plugin actions",
    description="Returns action summaries, permission metadata, and input/output schemas. Dynamic payloads follow these registered schemas.",
)
async def list_plugin_actions(
    plugin_id: str,
    server: OctopServer = Depends(get_server),
    _user: Any = Depends(current_user),
) -> dict[str, Any]:
    facade = _facade(server, plugin_id)
    try:
        descriptors = facade.descriptors()
    except PluginHostError as exc:
        raise _host_error(exc) from None
    return {"api_version": 1, "actions": descriptors["actions"]}


@router.post(
    "/{plugin_id}/actions/{action}",
    response_model=PluginActionResult,
    summary="Run a registered plugin action",
    description="The caller identity comes from JWT authentication. The payload schema is defined by the registered action catalog.",
)
async def run_plugin_action(
    plugin_id: str,
    action: str,
    body: PluginActionEnvelope,
    request: Request,
    server: OctopServer = Depends(get_server),
    user: Any = Depends(current_user),
) -> Any:
    facade = _facade(server, plugin_id)
    try:
        result = await facade.invoke_action(
            action, _actor(user, request, body.context), body.payload
        )
    except PluginActionError as exc:
        return _plugin_error(exc)
    except PluginHostExecutionError:
        raise OctopError.localized(ErrorCode.INTERNAL_ERROR) from None
    except PluginHostError as exc:
        raise _host_error(exc) from None
    return {"api_version": 1, "result": result}


@router.get(
    "/{plugin_id}/resources/{resource_token}",
    summary="Read a registered plugin resource",
    description="The opaque token is interpreted by the plugin. The response contains only its body and media type; arbitrary headers and redirects are not forwarded.",
    response_model=None,
)
async def get_plugin_resource(
    plugin_id: str,
    resource_token: str,
    request: Request,
    agent_id: str | None = Query(default=None, description="Optional checked agent scope"),
    thread_id: str | None = Query(default=None, description="Optional checked thread scope"),
    operation_id: str | None = Query(default=None, description="Optional operation correlation id"),
    server: OctopServer = Depends(get_server),
    user: Any = Depends(current_user),
) -> Response:
    context = PluginActionContext(
        agent_id=agent_id,
        thread_id=thread_id,
        operation_id=operation_id,
    )
    facade = _facade(server, plugin_id)
    try:
        resource = await facade.invoke_resource(_actor(user, request, context), resource_token)
    except PluginActionError as exc:
        return _plugin_error(exc)
    except PluginHostExecutionError:
        raise OctopError.localized(ErrorCode.INTERNAL_ERROR) from None
    except PluginHostError as exc:
        raise _host_error(exc) from None

    headers: dict[str, str] = {"Cache-Control": "private, no-store"}
    if resource.filename is not None:
        headers["Content-Disposition"] = f'inline; filename="{resource.filename}"'
    if isinstance(resource.body, bytes):
        return Response(content=resource.body, media_type=resource.media_type, headers=headers)
    return StreamingResponse(
        facade.stream_resource(resource),
        media_type=resource.media_type,
        headers=headers,
    )
