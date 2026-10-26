import { request } from "../../api/request";
import * as chatStore from "../../pages/Chat/hooks/chatStore";
import i18n from "../../i18n";
import {
  mergePatchedToolOutput,
  parseOctopToolOutput,
} from "./parseToolOutput";
import { registerComposerPreparation } from "./composerPreparation";
import {
  getAuthSessionEpoch,
  isAuthSessionCurrent,
} from "../../api/authSession";
import {
  isCurrentPluginSurfaceRegistration,
  registerPluginSurface,
  registerToolRenderer,
} from "./registry";
import type {
  ComposerPreparationHandler,
  OctopPluginUIHost,
  PluginSurfaceComponent,
  PluginSurfaceContext,
  PluginSurfaceName,
  PluginSurfaceRegistration,
  PluginJson,
  OpenSidePanelOptions,
  ToolRenderContext,
  ToolRendererRegistration,
} from "./types";

let contextOverride: Partial<ToolRenderContext> = {};
let dockHandlers: {
  openSidePanel?: (opts: {
    callId: string;
    title?: string;
    toolName?: string;
  }) => void;
  closeSidePanel?: (callId: string) => void;
} = {};

/** Chat page wires dock open/close for plugin ``host.openSidePanel``. */
export function setPluginUiDockHandlers(handlers: typeof dockHandlers): void {
  dockHandlers = handlers;
}

/** Chat page sets agent/thread so plugin UIs can call scoped APIs. */
export function setPluginUiToolContext(
  partial: Partial<ToolRenderContext>,
): void {
  contextOverride = { ...contextOverride, ...partial };
}

function readTheme(): "light" | "dark" {
  if (typeof document === "undefined") return "light";
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "dark") return "dark";
  if (document.documentElement.classList.contains("dark")) return "dark";
  return "light";
}

interface HostScope {
  isActive(): boolean;
  enforceOwner?: boolean;
  registrationToken?: object;
  getContext?: () => PluginSurfaceContext | null;
  getLastContext?: () => PluginSurfaceContext | null;
  registerSurface?: (
    surface: PluginSurfaceName,
    component: PluginSurfaceComponent,
  ) => () => void;
  registerComposerPreparation?: (
    handler: ComposerPreparationHandler,
  ) => () => void;
  toolBinding?: {
    callId: string;
    context: ToolRenderContext;
    getPresentationContext?: () => Pick<ToolRenderContext, "locale" | "theme">;
    openSidePanel(
      options: OpenSidePanelOptions & {
        pluginId: string;
        context: ToolRenderContext;
      },
    ): void;
    closeSidePanel(callId: string): void;
    patchResult(
      callId: string,
      nextData: unknown,
      targetSessionId: string,
    ): boolean;
  };
}

function invalidatedHostError(): Error {
  return new Error("Plugin UI host is inactive");
}

function invalidPluginStateError(): Error {
  return new Error("Plugin UI state must be finite JSON");
}

function toolBindingContext(
  binding: NonNullable<HostScope["toolBinding"]>,
): ToolRenderContext {
  const presentation = binding.getPresentationContext?.();
  return {
    agentId: binding.context.agentId,
    threadId: binding.context.threadId,
    locale: presentation?.locale ?? binding.context.locale,
    theme: presentation?.theme ?? binding.context.theme,
  };
}

function clonePluginJson(
  value: unknown,
  ancestors = new Set<object>(),
): PluginJson {
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "object") throw invalidPluginStateError();
  if (ancestors.has(value)) throw invalidPluginStateError();
  ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      const result: PluginJson[] = [];
      for (let index = 0; index < value.length; index += 1) {
        if (!(index in value)) throw invalidPluginStateError();
        result.push(clonePluginJson(value[index], ancestors));
      }
      return result;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw invalidPluginStateError();
    }
    if (Object.getOwnPropertySymbols(value).length)
      throw invalidPluginStateError();
    const result: Record<string, PluginJson> = {};
    for (const [key, descriptor] of Object.entries(
      Object.getOwnPropertyDescriptors(value),
    )) {
      if (!descriptor.enumerable || !("value" in descriptor)) {
        throw invalidPluginStateError();
      }
      Object.defineProperty(result, key, {
        value: clonePluginJson(descriptor.value, ancestors),
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
    return result;
  } finally {
    ancestors.delete(value);
  }
}

function createHost(
  defaultPluginId: string,
  scope?: HostScope,
): OctopPluginUIHost {
  const assertActive = () => {
    if (scope && !scope.isActive()) throw invalidatedHostError();
  };
  return {
    registerRenderer(reg) {
      assertActive();
      const pluginId = scope?.enforceOwner
        ? defaultPluginId
        : reg.pluginId ?? defaultPluginId;
      const full: ToolRendererRegistration = {
        id: reg.id,
        pluginId,
        tools: reg.tools,
        component: reg.component,
      };
      registerToolRenderer(full, scope?.registrationToken);
    },
    registerSurface(surface, component) {
      assertActive();
      if (!scope?.registerSurface) {
        throw new Error(
          "Plugin surfaces can only be registered during plugin setup",
        );
      }
      return scope.registerSurface(surface, component);
    },
    getContext(): PluginSurfaceContext | null {
      if (!scope?.getContext || !scope.isActive()) return null;
      return scope.getContext();
    },
    registerComposerPreparation(handler) {
      assertActive();
      if (!scope?.registerComposerPreparation) {
        throw new Error(
          "Composer preparation can only be registered by a live composer surface",
        );
      }
      return scope.registerComposerPreparation(handler);
    },
    getToolContext(): ToolRenderContext {
      assertActive();
      if (scope?.toolBinding) return toolBindingContext(scope.toolBinding);
      const scopedContext =
        scope?.getContext?.() ??
        scope?.getLastContext?.() ??
        scope?.toolBinding?.context;
      if (scopedContext) {
        return {
          agentId: scopedContext.agentId,
          threadId: scopedContext.threadId,
          locale: scopedContext.locale,
          theme: scopedContext.theme,
        };
      }
      return {
        agentId: contextOverride.agentId ?? null,
        threadId: contextOverride.threadId ?? null,
        locale: contextOverride.locale ?? i18n.language ?? "en",
        theme: contextOverride.theme ?? readTheme(),
      };
    },
    patchResult(callId: string, nextData: unknown) {
      assertActive();
      if (!callId) return;
      if (scope?.toolBinding) {
        if (
          callId !== scope.toolBinding.callId ||
          !scope.toolBinding.context.threadId ||
          !scope.toolBinding.patchResult(
            callId,
            nextData,
            scope.toolBinding.context.threadId,
          )
        )
          throw invalidatedHostError();
        return;
      }
      chatStore.patchToolResultData(callId, nextData);
    },
    request<T = unknown>(path: string, init?: RequestInit) {
      assertActive();
      const result = request<T>(path, init);
      if (!scope) return result;
      return result.then(
        (value) => {
          assertActive();
          return value;
        },
        (error) => {
          assertActive();
          throw error;
        },
      );
    },
    openSidePanel(opts) {
      assertActive();
      if (scope?.toolBinding) {
        if (opts.callId !== scope.toolBinding.callId)
          throw invalidatedHostError();
        const state = Object.prototype.hasOwnProperty.call(opts, "state")
          ? clonePluginJson(opts.state)
          : undefined;
        assertActive();
        scope.toolBinding.openSidePanel({
          callId: scope.toolBinding.callId,
          pluginId: defaultPluginId,
          context: toolBindingContext(scope.toolBinding),
          ...(opts.title === undefined ? {} : { title: opts.title }),
          ...(opts.toolName === undefined ? {} : { toolName: opts.toolName }),
          ...(Object.prototype.hasOwnProperty.call(opts, "state")
            ? { state }
            : {}),
        });
        return;
      }
      if (defaultPluginId !== "builtin") throw invalidatedHostError();
      dockHandlers.openSidePanel?.(opts);
    },
    closeSidePanel(callId) {
      assertActive();
      if (scope?.toolBinding) {
        if (callId !== scope.toolBinding.callId) throw invalidatedHostError();
        scope.toolBinding.closeSidePanel(scope.toolBinding.callId);
        return;
      }
      if (defaultPluginId !== "builtin") throw invalidatedHostError();
      dockHandlers.closeSidePanel?.(callId);
    },
  };
}

/** Host bound to ``builtin`` for first-party renderers. */
export const builtinPluginHost: OctopPluginUIHost = createHost("builtin");

/** Build a host scoped to an installed plugin id (for dynamic UI modules). */
export function createPluginUiHost(pluginId: string): OctopPluginUIHost {
  return createHost(pluginId);
}

/** Create a one-activation tool Host bound to its actual chat and renderer. */
export function createPluginUiToolHost(
  pluginId: string,
  context: ToolRenderContext,
  binding: {
    callId: string;
    isActive(): boolean;
    openSidePanel(
      options: OpenSidePanelOptions & {
        pluginId: string;
        context: ToolRenderContext;
      },
    ): void;
    closeSidePanel(callId: string): void;
    getPresentationContext?: () => Pick<ToolRenderContext, "locale" | "theme">;
  },
): OctopPluginUIHost {
  const authEpoch = getAuthSessionEpoch();
  const isActive = () =>
    isAuthSessionCurrent(authEpoch) &&
    !!context.agentId &&
    !!context.threadId &&
    binding.isActive();
  return createHost(pluginId, {
    isActive,
    toolBinding: {
      ...binding,
      context: { ...context },
      patchResult: (callId, nextData, targetSessionId) => {
        if (
          !isActive() ||
          !context.threadId ||
          targetSessionId !== context.threadId
        )
          return false;
        return chatStore.patchToolResultData(callId, nextData, targetSessionId);
      },
    },
  });
}

/** Host used only while the loader owns an active module registration. */
export function createPluginUiRegistrationHost(
  pluginId: string,
  loaderGeneration: number,
  isLoaderActive: () => boolean,
  registrationToken: object,
): OctopPluginUIHost {
  return createHost(pluginId, {
    isActive: isLoaderActive,
    enforceOwner: true,
    registrationToken,
    registerSurface(surface, component) {
      return registerPluginSurface(pluginId, surface, component, {
        loaderGeneration,
        isLoaderActive,
        registrationToken,
      });
    },
  });
}

/** Host bound to one live registration and one mounted context generation. */
export function createPluginUiSurfaceHost(
  registration: PluginSurfaceRegistration,
  getContext: () => PluginSurfaceContext,
  isMounted: () => boolean,
): OctopPluginUIHost {
  const authEpoch = getAuthSessionEpoch();
  const isActive = () =>
    isAuthSessionCurrent(authEpoch) &&
    isMounted() &&
    isCurrentPluginSurfaceRegistration(registration);
  return createHost(registration.pluginId, {
    isActive,
    enforceOwner: true,
    registrationToken: registration.registrationToken,
    getContext: () => (isActive() ? getContext() : null),
    getLastContext: getContext,
    registerComposerPreparation(handler) {
      if (registration.surface !== "composer") {
        throw new Error(
          "Composer preparation can only be registered by a live composer surface",
        );
      }
      const context = getContext();
      return registerComposerPreparation(
        registration.pluginId,
        registration,
        context.generation,
        handler,
        isActive,
      );
    },
  });
}

export { parseOctopToolOutput, mergePatchedToolOutput };
