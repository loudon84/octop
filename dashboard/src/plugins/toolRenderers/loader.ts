import { getApiUrl } from "../../api/config";
import { getAuthToken } from "../../api/request";
import type { InstalledPlugin } from "../../api/modules/plugins";
import { createPluginUiRegistrationHost } from "./host";
import { unregisterPluginUi, unregisterPluginUiRegistration } from "./registry";
import type { OctopPluginUIHost, PluginUiModule } from "./types";

interface LoadedPluginUi {
  fingerprint: string;
  generation: number;
  capability: { active: boolean };
  loadToken: PendingPluginUi;
  setupDisposer?: () => void;
  blobUrl: string;
}

interface PendingPluginUi {
  fingerprint: string;
  generation: number;
  promise: Promise<void>;
  blobUrl?: string;
}

const loadedPlugins = new Map<string, LoadedPluginUi>();
const pendingPlugins = new Map<string, PendingPluginUi>();
const eligibleInventory = new Map<string, string>();
let loaderGeneration = 0;

function fingerprint(plugin: InstalledPlugin): string {
  return JSON.stringify([
    plugin.id,
    plugin.version ?? "",
    plugin.ui?.entry ?? "",
  ]);
}

function isEligible(plugin: InstalledPlugin): boolean {
  return (
    !plugin.error &&
    plugin.enabled !== false &&
    plugin.loaded !== false &&
    Boolean(plugin.ui?.entry)
  );
}

async function fetchAuthedText(path: string): Promise<string> {
  const url = getApiUrl(path);
  const token = getAuthToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(url, { headers, cache: "no-store" });
  if (!res.ok) {
    throw new Error(`plugin UI fetch failed: ${res.status} ${path}`);
  }
  return res.text();
}

function moduleApiPath(pluginId: string, entryRel: string): string {
  const normalized = entryRel.replace(/^\/+/, "");
  const underUi = normalized.startsWith("ui/")
    ? normalized.slice(3)
    : normalized;
  return `/plugins/${encodeURIComponent(pluginId)}/ui/${underUi}`;
}

function revokeBlobUrl(pluginId: string, blobUrl: string): void {
  try {
    URL.revokeObjectURL(blobUrl);
  } catch (error) {
    console.warn(`[plugin-ui] blob cleanup failed for ${pluginId}:`, error);
  }
}

async function importPluginEsm(
  pluginId: string,
  entryRel: string,
  isCurrent: () => boolean,
  onBlobUrl: (url: string) => void,
): Promise<{ module: PluginUiModule; blobUrl: string } | null> {
  const source = await fetchAuthedText(moduleApiPath(pluginId, entryRel));
  if (!isCurrent()) return null;

  const blob = new Blob([source], { type: "text/javascript" });
  const blobUrl = URL.createObjectURL(blob);
  onBlobUrl(blobUrl);
  const module = (await import(/* @vite-ignore */ blobUrl)) as PluginUiModule;
  return { module, blobUrl };
}

function releasePendingBlobUrl(
  pluginId: string,
  record: PendingPluginUi,
): void {
  const blobUrl = record.blobUrl;
  record.blobUrl = undefined;
  if (blobUrl) revokeBlobUrl(pluginId, blobUrl);
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
  return (
    (typeof value === "object" || typeof value === "function") &&
    value !== null &&
    typeof (value as { then?: unknown }).then === "function"
  );
}

function cleanupPluginUi(pluginId: string): void {
  const record = loadedPlugins.get(pluginId);
  if (record) {
    loadedPlugins.delete(pluginId);
    record.capability.active = false;
    try {
      record.setupDisposer?.();
    } catch (error) {
      console.warn(`[plugin-ui] cleanup failed for ${pluginId}:`, error);
    }
    try {
      unregisterPluginUiRegistration(pluginId, record.loadToken);
    } catch (error) {
      console.warn(
        `[plugin-ui] registration cleanup failed for ${pluginId}:`,
        error,
      );
    }
    revokeBlobUrl(pluginId, record.blobUrl);
    return;
  }
  unregisterPluginUi(pluginId);
}

function isCurrentLoad(
  pluginId: string,
  expectedFingerprint: string,
  generation: number,
  record: PendingPluginUi,
): boolean {
  const loaded = loadedPlugins.get(pluginId);
  return (
    loaderGeneration === generation &&
    eligibleInventory.get(pluginId) === expectedFingerprint &&
    (pendingPlugins.get(pluginId) === record ||
      (loaded?.loadToken === record && loaded.capability.active))
  );
}

async function loadOne(
  plugin: InstalledPlugin,
  expectedFingerprint: string,
  record: PendingPluginUi,
): Promise<void> {
  const pluginId = plugin.id;
  const isCurrent = () =>
    isCurrentLoad(pluginId, expectedFingerprint, record.generation, record);
  let setupDisposer: (() => void) | undefined;
  const capability = { active: false };
  try {
    const imported = await importPluginEsm(
      pluginId,
      plugin.ui!.entry,
      isCurrent,
      (url) => {
        record.blobUrl = url;
      },
    );
    if (!imported || !isCurrent()) {
      releasePendingBlobUrl(pluginId, record);
      return;
    }
    const host: OctopPluginUIHost = createPluginUiRegistrationHost(
      pluginId,
      record.generation,
      () => capability.active && isCurrent(),
      record,
    );
    capability.active = true;
    const setup = imported.module.setup ?? imported.module.default?.setup;
    if (typeof setup === "function") {
      const disposer = setup(host);
      if (isThenable(disposer)) {
        void Promise.resolve(disposer).catch(() => undefined);
        throw new Error("Plugin UI setup must be synchronous");
      }
      if (typeof disposer === "function") setupDisposer = disposer;
    }
    if (!isCurrent()) {
      capability.active = false;
      const dispose = setupDisposer;
      setupDisposer = undefined;
      try {
        dispose?.();
      } finally {
        unregisterPluginUiRegistration(pluginId, record);
        releasePendingBlobUrl(pluginId, record);
      }
      return;
    }

    loadedPlugins.set(pluginId, {
      fingerprint: expectedFingerprint,
      generation: record.generation,
      capability,
      loadToken: record,
      setupDisposer,
      blobUrl: imported.blobUrl,
    });
    record.blobUrl = undefined;
  } catch (error) {
    capability.active = false;
    const dispose = setupDisposer;
    setupDisposer = undefined;
    try {
      dispose?.();
    } catch (cleanupError) {
      console.warn(`[plugin-ui] cleanup failed for ${pluginId}:`, cleanupError);
    }
    unregisterPluginUiRegistration(pluginId, record);
    releasePendingBlobUrl(pluginId, record);
    console.warn(`[plugin-ui] failed to load ${pluginId}:`, error);
  }
}

/** Reconcile UI modules with one fresh installed-plugin inventory. */
export async function loadInstalledPluginUis(
  plugins: InstalledPlugin[],
): Promise<void> {
  const nextEligible = new Map<string, string>();
  for (const plugin of plugins) {
    if (isEligible(plugin)) nextEligible.set(plugin.id, fingerprint(plugin));
  }

  eligibleInventory.clear();
  for (const [pluginId, value] of nextEligible)
    eligibleInventory.set(pluginId, value);

  for (const [pluginId, loaded] of [...loadedPlugins]) {
    if (nextEligible.get(pluginId) !== loaded.fingerprint) {
      cleanupPluginUi(pluginId);
    }
  }
  for (const [pluginId, pending] of [...pendingPlugins]) {
    if (
      pending.generation !== loaderGeneration ||
      nextEligible.get(pluginId) !== pending.fingerprint
    ) {
      pendingPlugins.delete(pluginId);
      releasePendingBlobUrl(pluginId, pending);
    }
  }

  const loads: Promise<void>[] = [];
  for (const plugin of plugins) {
    if (!isEligible(plugin)) continue;
    const expectedFingerprint = fingerprint(plugin);
    if (
      loadedPlugins.get(plugin.id)?.fingerprint === expectedFingerprint &&
      loadedPlugins.get(plugin.id)?.generation === loaderGeneration
    )
      continue;
    const existing = pendingPlugins.get(plugin.id);
    if (
      existing &&
      existing.generation === loaderGeneration &&
      existing.fingerprint === expectedFingerprint
    ) {
      loads.push(existing.promise);
      continue;
    }

    const record: PendingPluginUi = {
      fingerprint: expectedFingerprint,
      generation: loaderGeneration,
      promise: Promise.resolve(),
    };
    const promise = loadOne(plugin, expectedFingerprint, record).finally(() => {
      if (pendingPlugins.get(plugin.id) === record) {
        pendingPlugins.delete(plugin.id);
      }
    });
    record.promise = promise;
    pendingPlugins.set(plugin.id, record);
    loads.push(promise);
  }
  await Promise.all(loads);
}

/** Force re-fetch and re-register UI modules (after install/uninstall in Admin). */
export async function reloadPluginToolUis(
  plugins: InstalledPlugin[],
): Promise<void> {
  resetPluginUiLoader();
  await loadInstalledPluginUis(plugins);
}

/** Revoke loader capabilities, clean registrations and release loaded blob URLs. */
export function resetPluginUiLoader(): void {
  loaderGeneration += 1;
  eligibleInventory.clear();
  for (const [pluginId, pending] of pendingPlugins) {
    releasePendingBlobUrl(pluginId, pending);
  }
  pendingPlugins.clear();
  for (const pluginId of [...loadedPlugins.keys()]) cleanupPluginUi(pluginId);
}

export function isPluginUiLoaded(pluginId: string): boolean {
  return loadedPlugins.has(pluginId);
}
