import type { PluginSetupHost } from "./host";
import { createElement } from "./host";
import { Settings } from "./Settings";
import { EvidenceRenderer } from "./EvidenceRenderer";

export function setup(host: PluginSetupHost): () => void {
  host.registerRenderer({
    id: "enterprise_knowledge_evidence",
    tools: ["enterprise_knowledge_search"],
    component: EvidenceRenderer,
  });
  return host.registerSurface("settings", (props: any) =>
    createElement(Settings, props),
  );
}
