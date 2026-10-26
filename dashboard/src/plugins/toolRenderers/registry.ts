import type { ComponentType } from "react";
import type {
  ParsedToolOutput,
  PluginSurfaceComponent,
  PluginSurfaceName,
  PluginSurfaceRegistration,
  ToolRendererRegistration,
  ToolRenderProps,
} from "./types";

const byKey = new Map<string, ToolRendererRegistration>();
const byToolName = new Map<string, ToolRendererRegistration>();
const rendererLoadOwners = new Map<string, object>();
const toolLoadOwners = new Map<string, object>();
const listeners = new Set<() => void>();
const surfaceListeners = new Set<() => void>();
const surfacesByPlugin = new Map<
  string,
  Map<PluginSurfaceName, PluginSurfaceRegistration>
>();
let version = 0;
let surfaceVersion = 0;
let nextSurfaceToken = 0;

function rendererKey(pluginId: string, id: string): string {
  return `${pluginId}::${id}`;
}

function bump(): void {
  version += 1;
  for (const fn of listeners) {
    try {
      fn();
    } catch {
      /* ignore */
    }
  }
}

function bumpSurfaces(): void {
  surfaceVersion += 1;
  for (const fn of surfaceListeners) {
    try {
      fn();
    } catch {
      /* ignore */
    }
  }
}

export function isPluginSurfaceName(
  value: unknown,
): value is PluginSurfaceName {
  return value === "settings" || value === "composer";
}

function isReactComponent(value: unknown): value is PluginSurfaceComponent {
  if (typeof value === "function") return true;
  if (!value || typeof value !== "object") return false;
  const marker = (value as { $$typeof?: unknown }).$$typeof;
  if (marker === Symbol.for("react.memo")) {
    return isReactComponent((value as { type?: unknown }).type);
  }
  return (
    marker === Symbol.for("react.forward_ref") &&
    typeof (value as { render?: unknown }).render === "function"
  );
}

/** Subscribe to registry changes (plugin UI load / unload). */
export function subscribeToolRenderers(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getToolRendererVersion(): number {
  return version;
}

export function subscribePluginSurfaces(listener: () => void): () => void {
  surfaceListeners.add(listener);
  return () => surfaceListeners.delete(listener);
}

export function getPluginSurfaceVersion(): number {
  return surfaceVersion;
}

export function registerPluginSurface(
  pluginId: string,
  surface: PluginSurfaceName,
  component: PluginSurfaceComponent,
  options: {
    loaderGeneration?: number;
    isLoaderActive?: () => boolean;
    registrationToken?: object;
  } = {},
): () => void {
  if (!pluginId) throw new Error("A plugin owner is required for a surface");
  if (!isPluginSurfaceName(surface)) {
    throw new Error(`Unknown plugin surface: ${String(surface)}`);
  }
  if (!isReactComponent(component)) {
    throw new Error("A plugin surface must be a React component");
  }

  const registration: PluginSurfaceRegistration = {
    pluginId,
    surface,
    component,
    loaderGeneration: options.loaderGeneration ?? 0,
    isLoaderActive: options.isLoaderActive ?? (() => true),
    token: ++nextSurfaceToken,
    registrationToken: options.registrationToken,
  };
  let owner = surfacesByPlugin.get(pluginId);
  if (!owner) {
    owner = new Map();
    surfacesByPlugin.set(pluginId, owner);
  }
  owner.set(surface, registration);
  bumpSurfaces();

  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    const current = surfacesByPlugin.get(pluginId);
    if (current?.get(surface) !== registration) return;
    current.delete(surface);
    if (!current.size) surfacesByPlugin.delete(pluginId);
    bumpSurfaces();
  };
}

export function listPluginSurfaces(
  surface: PluginSurfaceName,
): PluginSurfaceRegistration[] {
  const result: PluginSurfaceRegistration[] = [];
  for (const owner of surfacesByPlugin.values()) {
    const registration = owner.get(surface);
    if (registration && isCurrentPluginSurfaceRegistration(registration)) {
      result.push(registration);
    }
  }
  return result;
}

export function isCurrentPluginSurfaceRegistration(
  registration: PluginSurfaceRegistration,
): boolean {
  return (
    surfacesByPlugin.get(registration.pluginId)?.get(registration.surface) ===
      registration && registration.isLoaderActive()
  );
}

export function unregisterPluginSurfaces(pluginId: string): void {
  if (surfacesByPlugin.delete(pluginId)) bumpSurfaces();
}

/** Remove all plugin-owned UI registrations while retaining builtin renderers. */
export function unregisterPluginUi(pluginId: string): void {
  unregisterPluginRenderers(pluginId);
  unregisterPluginSurfaces(pluginId);
}

export function clearToolRenderers(): void {
  byKey.clear();
  byToolName.clear();
  rendererLoadOwners.clear();
  toolLoadOwners.clear();
  bump();
}

export function registerToolRenderer(
  reg: ToolRendererRegistration,
  registrationToken?: object,
): void {
  const key = rendererKey(reg.pluginId, reg.id);
  byKey.set(key, reg);
  if (registrationToken) rendererLoadOwners.set(key, registrationToken);
  else rendererLoadOwners.delete(key);
  for (const tool of reg.tools ?? []) {
    const name = tool.trim();
    if (!name) continue;
    byToolName.set(name, reg);
    if (registrationToken) toolLoadOwners.set(name, registrationToken);
    else toolLoadOwners.delete(name);
  }
  bump();
}

export function unregisterPluginRenderers(pluginId: string): void {
  let changed = false;
  for (const [key, reg] of [...byKey.entries()]) {
    if (reg.pluginId === pluginId) {
      byKey.delete(key);
      rendererLoadOwners.delete(key);
      changed = true;
    }
  }
  for (const [tool, reg] of [...byToolName.entries()]) {
    if (reg.pluginId === pluginId) {
      byToolName.delete(tool);
      toolLoadOwners.delete(tool);
      changed = true;
    }
  }
  if (changed) bump();
}

/** Remove only registrations created by one loader setup token. */
export function unregisterPluginUiRegistration(
  pluginId: string,
  registrationToken: object,
): void {
  let rendererChanged = false;
  for (const [key, registration] of [...byKey]) {
    if (
      registration.pluginId === pluginId &&
      rendererLoadOwners.get(key) === registrationToken
    ) {
      byKey.delete(key);
      rendererLoadOwners.delete(key);
      for (const [toolName, toolRegistration] of [...byToolName]) {
        if (toolRegistration === registration) byToolName.delete(toolName);
      }
      rendererChanged = true;
    }
  }
  for (const [toolName, registrationTokenOwner] of [...toolLoadOwners]) {
    if (registrationTokenOwner === registrationToken) {
      byToolName.delete(toolName);
      toolLoadOwners.delete(toolName);
      rendererChanged = true;
    }
  }
  if (rendererChanged) bump();

  const surfaces = surfacesByPlugin.get(pluginId);
  if (!surfaces) return;
  let surfaceChanged = false;
  for (const [surface, registration] of [...surfaces]) {
    if (registration.registrationToken === registrationToken) {
      surfaces.delete(surface);
      surfaceChanged = true;
    }
  }
  if (!surfaces.size) surfacesByPlugin.delete(pluginId);
  if (surfaceChanged) bumpSurfaces();
}

export function resolveToolRenderer(opts: {
  toolName?: string;
  pluginId?: string | null;
  parsed: ParsedToolOutput;
}): ToolRendererRegistration | null {
  const { toolName, pluginId, parsed } = opts;
  const hint = parsed.octopUi;
  if (hint?.renderer) {
    if (pluginId) {
      const hit = byKey.get(rendererKey(pluginId, hint.renderer));
      if (hit) return hit;
    }
    for (const reg of byKey.values()) {
      if (reg.id === hint.renderer) return reg;
    }
  }
  if (toolName) {
    const byTool = byToolName.get(toolName);
    if (byTool) return byTool;
  }
  return null;
}

export function listRegisteredRenderers(): ToolRendererRegistration[] {
  return [...byKey.values()];
}

export type { ComponentType, ToolRenderProps };
