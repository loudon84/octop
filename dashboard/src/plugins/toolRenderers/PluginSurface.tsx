import { useLayoutEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { ToolUiErrorBoundary } from "./ToolUiErrorBoundary";
import { createPluginUiSurfaceHost } from "./host";
import {
  getAuthSessionEpoch,
  subscribeAuthSession,
} from "../../api/authSession";
import {
  getPluginSurfaceVersion,
  isCurrentPluginSurfaceRegistration,
  listPluginSurfaces,
  subscribePluginSurfaces,
} from "./registry";
import type {
  PluginReadinessState,
  PluginSurfaceContext,
  PluginSurfaceLocation,
  PluginSurfaceName,
  PluginSurfaceRegistration,
  ToolRenderProps,
} from "./types";

function snapshotContext(context: PluginSurfaceContext): PluginSurfaceContext {
  let readiness: PluginReadinessState;
  if (context.readiness.status === "ready") {
    readiness = Object.freeze({
      status: "ready",
      requiredPlugins: Object.freeze({ ...context.readiness.requiredPlugins }),
      unavailablePlugins: Object.freeze([
        ...context.readiness.unavailablePlugins,
      ]),
    });
  } else {
    readiness = Object.freeze({ ...context.readiness });
  }
  return Object.freeze({ ...context, readiness });
}

function isValidLocation(
  surface: PluginSurfaceName,
  location: PluginSurfaceLocation,
): boolean {
  return (
    (surface === "settings" &&
      (location === "connectors" || location === "expert")) ||
    (surface === "composer" && location === "chat")
  );
}

function MountedPluginSurface(props: {
  registration: PluginSurfaceRegistration;
  context: PluginSurfaceContext;
  location: PluginSurfaceLocation;
}) {
  const { registration, context, location } = props;
  const contextRef = useRef(snapshotContext(context));
  contextRef.current = snapshotContext(context);
  const mountedRef = useRef(true);
  const host = useMemo(
    () =>
      createPluginUiSurfaceHost(
        registration,
        () => contextRef.current,
        () => mountedRef.current,
      ),
    [registration],
  );

  useLayoutEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  if (!isCurrentPluginSurfaceRegistration(registration)) return null;
  const fallbackProps: ToolRenderProps = {
    pluginId: registration.pluginId,
    toolName: registration.surface,
    status: "error",
    args: null,
    data: null,
    host,
    isStreaming: false,
  };
  const Component = registration.component;
  return (
    <ToolUiErrorBoundary propsForFallback={fallbackProps}>
      <Component host={host} context={contextRef.current} location={location} />
    </ToolUiErrorBoundary>
  );
}

/** Render plugins registered for one of the host's fixed surface locations. */
export function PluginSurface(props: {
  surface: PluginSurfaceName;
  location: PluginSurfaceLocation;
  context: PluginSurfaceContext;
}) {
  const { surface, location, context } = props;
  useSyncExternalStore(
    subscribePluginSurfaces,
    getPluginSurfaceVersion,
    getPluginSurfaceVersion,
  );
  const authEpoch = useSyncExternalStore(
    subscribeAuthSession,
    getAuthSessionEpoch,
    getAuthSessionEpoch,
  );
  if (!isValidLocation(surface, location)) return null;

  return (
    <>
      {listPluginSurfaces(surface).map((registration) => (
        <MountedPluginSurface
          key={`${registration.pluginId}:${registration.token}:${
            context.generation
          }:${context.agentId ?? ""}:${context.threadId ?? ""}:${authEpoch}`}
          registration={registration}
          context={context}
          location={location}
        />
      ))}
    </>
  );
}
