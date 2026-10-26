import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Alert, Button, Spin } from "antd";
import { useTranslation } from "react-i18next";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import {
  getAuthSessionEpoch,
  subscribeAuthSession,
} from "../../api/authSession";
import i18n from "../../i18n";
import { PLUGINS_CHANGED_EVENT } from "../pluginsEvents";
import { ensureBuiltinToolRenderers } from "./ensureBuiltins";
import { refreshPluginUiInventory } from "./inventory";
import { PluginSurface } from "./PluginSurface";
import type {
  PluginReadinessState,
  PluginSurfaceContext,
  PluginSurfaceLocation,
} from "./types";

function currentTheme(): "light" | "dark" {
  if (typeof document === "undefined") return "light";
  return document.documentElement.getAttribute("data-theme") === "dark" ||
    document.documentElement.classList.contains("dark")
    ? "dark"
    : "light";
}

/** Fixed settings slot; it reconciles module inventory without touching Chat context. */
export function PluginSettingsSurface(props: {
  agentId: string | null;
  location: Extract<PluginSurfaceLocation, "connectors" | "expert">;
}) {
  const { agentId, location } = props;
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const authEpoch = useSyncExternalStore(
    subscribeAuthSession,
    getAuthSessionEpoch,
    getAuthSessionEpoch,
  );
  const generationCounter = useRef(0);
  const lastTarget = useRef<{
    agentId: string | null;
    authEpoch: number;
  }>({ agentId, authEpoch });
  if (
    lastTarget.current.agentId !== agentId ||
    lastTarget.current.authEpoch !== authEpoch
  ) {
    generationCounter.current += 1;
    lastTarget.current = { agentId, authEpoch };
  }
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const requestGeneration = useRef(0);

  useEffect(() => {
    let mounted = true;
    const refreshInventory = async () => {
      const requestId = ++requestGeneration.current;
      setStatus((current) => (current === "ready" ? current : "loading"));
      try {
        const plugins = await refreshPluginUiInventory(
          () => mounted && requestId === requestGeneration.current,
        );
        if (!plugins) {
          if (mounted && requestId === requestGeneration.current) {
            setStatus("error");
          }
          return;
        }
        if (mounted && requestId === requestGeneration.current) {
          setStatus("ready");
        }
      } catch {
        if (mounted && requestId === requestGeneration.current) {
          setStatus("error");
        }
      }
    };
    const handleLifecycle = () => void refreshInventory();
    ensureBuiltinToolRenderers();
    window.addEventListener(PLUGINS_CHANGED_EVENT, handleLifecycle);
    void refreshInventory();
    return () => {
      mounted = false;
      window.removeEventListener(PLUGINS_CHANGED_EVENT, handleLifecycle);
    };
  }, []);

  const readiness: PluginReadinessState = { status: "loading" };
  const context: PluginSurfaceContext = {
    agentId,
    threadId: null,
    locale: i18n.language || "en",
    theme: currentTheme(),
    generation: `settings-context-${generationCounter.current}`,
    timeZone,
    readiness,
  };

  return (
    <section aria-label={t("plugins.surface.title")}>
      {status === "loading" ? (
        <div role="status" aria-live="polite">
          <Spin size="small" /> {t("plugins.surface.loading")}
        </div>
      ) : status === "error" ? (
        <Alert
          type="error"
          showIcon
          message={t("plugins.surface.error")}
          action={
            <Button
              size="small"
              onClick={() =>
                window.dispatchEvent(new Event(PLUGINS_CHANGED_EVENT))
              }
            >
              {t("common.retry")}
            </Button>
          }
        />
      ) : (
        <PluginSurface
          surface="settings"
          location={location}
          context={context}
        />
      )}
    </section>
  );
}
