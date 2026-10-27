import type { PluginSetupHost } from "./host";
import { createElement, useRef } from "./host";
import type { PluginSurfaceProps } from "./host";
import { Settings } from "./Settings";
import { Composer } from "./Composer";
import { EvidenceRenderer } from "./EvidenceRenderer";

const PLUGIN_ID = "enterprise-knowledge";

function EnterpriseComposerSurface(props: PluginSurfaceProps) {
  const { context } = props;
  const scopeKey = `${context.generation}:${context.agentId ?? ""}:${
    context.threadId ?? ""
  }`;
  const participation = useRef({ scopeKey, required: false });
  if (participation.current.scopeKey !== scopeKey) {
    participation.current = { scopeKey, required: false };
  }
  const readiness = context.readiness;
  if (readiness?.status === "ready") {
    // Keep a confirmed mount during same-scope polling so the draft survives;
    // Host refreshes readiness before send and remains the admission gate.
    participation.current.required = Object.prototype.hasOwnProperty.call(
      readiness.requiredPlugins,
      PLUGIN_ID,
    );
  }
  if (!participation.current.required) return null;
  return createElement(Composer, props);
}

export function setup(host: PluginSetupHost): () => void {
  host.registerRenderer({
    id: "enterprise_knowledge_evidence",
    tools: ["enterprise_knowledge_search"],
    component: EvidenceRenderer,
  });
  const disposeSettings = host.registerSurface("settings", (props: any) =>
    createElement(Settings, props),
  );
  const disposeComposer = host.registerSurface("composer", (props: any) =>
    createElement(EnterpriseComposerSurface, props),
  );
  return () => {
    disposeSettings();
    disposeComposer();
  };
}
