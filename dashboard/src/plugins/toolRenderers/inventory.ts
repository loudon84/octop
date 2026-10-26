import { pluginsApi, type InstalledPlugin } from "../../api/modules/plugins";
import { loadInstalledPluginUis } from "./loader";
import { updateToolPluginIndex } from "./toolPluginIndex";

let requestGeneration = 0;
let latestRefresh: Promise<InstalledPlugin[] | null> | null = null;

/** Fetch and reconcile one shared inventory, discarding out-of-order owners. */
export function refreshPluginUiInventory(
  isCurrent: () => boolean = () => true,
): Promise<InstalledPlugin[] | null> {
  const generation = ++requestGeneration;
  const refresh = (async (): Promise<InstalledPlugin[] | null> => {
    const plugins = await pluginsApi.list();
    if (!isCurrent()) return null;
    if (generation !== requestGeneration) {
      const latest = latestRefresh;
      if (!latest) return null;
      const currentInventory = await latest;
      return isCurrent() ? currentInventory : null;
    }

    updateToolPluginIndex(plugins);
    await loadInstalledPluginUis(plugins);
    if (generation !== requestGeneration) {
      const latest = latestRefresh;
      if (!latest) return null;
      const currentInventory = await latest;
      return isCurrent() ? currentInventory : null;
    }
    return isCurrent() ? plugins : null;
  })();
  latestRefresh = refresh;
  return refresh;
}
