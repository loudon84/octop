import { useCallback, useEffect, useRef, useState } from "react";
import { pluginsApi, type PluginRequirements } from "../../api/modules/plugins";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { PLUGINS_CHANGED_EVENT } from "../pluginsEvents";
import {
  ensureBuiltinToolRenderers,
  setPluginUiToolContext,
} from "../toolRenderers";
import { isPluginUiLoaded } from "./loader";
import { refreshPluginUiInventory } from "./inventory";
import type { PluginReadinessState } from "./types";

export interface PluginReadinessTarget {
  agentId: string | null;
  threadId: string | null;
}

export type PluginToolUiReadiness = PluginReadinessState;

export interface UsePluginToolUisResult {
  readiness: PluginToolUiReadiness;
  refresh: (target?: PluginReadinessTarget) => Promise<PluginToolUiReadiness>;
  generation: string;
}

function safeError(error: unknown): PluginToolUiReadiness {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return {
    status: "error",
    reason: /\b403\b|forbidden|denied/i.test(message)
      ? "denied"
      : "unavailable",
  };
}

function requirementReadiness(
  result: PluginRequirements,
  plugins: Awaited<ReturnType<typeof pluginsApi.list>>,
): PluginToolUiReadiness {
  const unavailable = new Set(result.unavailable_plugins);
  for (const pluginId of Object.keys(result.required_plugins)) {
    const plugin = plugins.find((item) => item.id === pluginId);
    if (!plugin || plugin.enabled === false || plugin.loaded === false) {
      unavailable.add(pluginId);
    } else if (plugin.ui?.entry && !isPluginUiLoaded(pluginId)) {
      unavailable.add(pluginId);
    }
  }
  return {
    status: "ready",
    requiredPlugins: result.required_plugins,
    unavailablePlugins: [...unavailable],
  };
}

/** Keep plugin UI inventory and generic readiness current for one visible Chat. */
export function usePluginToolUis(opts: {
  agentId?: string | null;
  threadId?: string | null;
  enabled?: boolean;
}): UsePluginToolUisResult {
  const { agentId = null, threadId = null, enabled = true } = opts;
  const principal = useCurrentUser();
  const generationCounter = useRef(0);
  const previousKey = useRef("");
  const principalObservation = principal?.id ?? null;
  const targetKey = `${principalObservation}:${agentId ?? ""}:${
    threadId ?? ""
  }`;
  if (previousKey.current !== targetKey) {
    previousKey.current = targetKey;
    generationCounter.current += 1;
  }
  const generation = `plugin-context-${generationCounter.current}`;
  const currentTarget = useRef<PluginReadinessTarget>({ agentId, threadId });
  currentTarget.current = { agentId, threadId };
  const currentGeneration = useRef(generation);
  currentGeneration.current = generation;
  const mounted = useRef(true);
  const activeRequests = useRef(
    new Map<string, Promise<PluginToolUiReadiness>>(),
  );
  const [stored, setStored] = useState<{
    generation: string;
    readiness: PluginToolUiReadiness;
  }>({ generation, readiness: { status: "loading" } });
  const readiness =
    stored.generation === generation
      ? stored.readiness
      : { status: "loading" as const };

  useEffect(() => {
    ensureBuiltinToolRenderers();
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    setPluginUiToolContext({ agentId, threadId });
  }, [agentId, threadId]);

  const refresh = useCallback((override?: PluginReadinessTarget) => {
    const target = override ?? currentTarget.current;
    const refreshGeneration = currentGeneration.current;
    const publish = !override;
    const requestKey = `${refreshGeneration}:${target.agentId ?? ""}:${
      target.threadId ?? ""
    }`;
    const existing = activeRequests.current.get(requestKey);
    if (existing) return existing;

    if (!target.agentId) {
      const pending: PluginToolUiReadiness = { status: "loading" };
      if (publish && mounted.current) {
        setStored({ generation: refreshGeneration, readiness: pending });
      }
      return Promise.resolve(pending);
    }

    if (publish && mounted.current) {
      setStored({
        generation: refreshGeneration,
        readiness: { status: "loading" },
      });
    }
    const work = (async (): Promise<PluginToolUiReadiness> => {
      try {
        const plugins = await refreshPluginUiInventory(
          () =>
            mounted.current &&
            (!publish ||
              (currentGeneration.current === refreshGeneration &&
                currentTarget.current.agentId === target.agentId &&
                currentTarget.current.threadId === target.threadId)),
        );
        if (!plugins) return { status: "loading" };
        const requirements = await pluginsApi.requirements(
          target.agentId!,
          target.threadId,
        );
        const result = requirementReadiness(requirements, plugins);
        if (
          publish &&
          mounted.current &&
          currentGeneration.current === refreshGeneration &&
          currentTarget.current.agentId === target.agentId &&
          currentTarget.current.threadId === target.threadId
        ) {
          setStored({ generation: refreshGeneration, readiness: result });
        }
        return result;
      } catch (error) {
        const result = safeError(error);
        if (
          publish &&
          mounted.current &&
          currentGeneration.current === refreshGeneration
        ) {
          setStored({ generation: refreshGeneration, readiness: result });
        }
        return result;
      } finally {
        activeRequests.current.delete(requestKey);
      }
    })();
    activeRequests.current.set(requestKey, work);
    return work;
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let interval: ReturnType<typeof setInterval> | undefined;
    const refreshCurrent = () => void refresh();
    const updateInterval = () => {
      if (document.visibilityState === "visible") {
        if (!interval) interval = setInterval(refreshCurrent, 30_000);
        refreshCurrent();
      } else if (interval) {
        clearInterval(interval);
        interval = undefined;
      }
    };
    const handleVisibility = () => {
      if (document.visibilityState === "visible") refreshCurrent();
      updateInterval();
    };
    window.addEventListener(PLUGINS_CHANGED_EVENT, refreshCurrent);
    window.addEventListener("focus", refreshCurrent);
    document.addEventListener("visibilitychange", handleVisibility);
    updateInterval();
    return () => {
      window.removeEventListener(PLUGINS_CHANGED_EVENT, refreshCurrent);
      window.removeEventListener("focus", refreshCurrent);
      document.removeEventListener("visibilitychange", handleVisibility);
      if (interval) clearInterval(interval);
    };
  }, [enabled, refresh, generation]);

  return { readiness, refresh, generation };
}
