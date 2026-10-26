import type { ComponentType } from "react";

/** Parsed ``octop_ui`` envelope from a tool JSON string output. */
export interface OctopUiHint {
  renderer: string;
  version?: number;
}

export interface ParsedToolOutput {
  /** True when ``output`` parsed as JSON object. */
  isJson: boolean;
  raw: unknown;
  octopUi?: OctopUiHint;
  data?: unknown;
  text?: string;
}

export type ToolRenderStatus = "running" | "done" | "error";

export interface ToolRenderContext {
  agentId: string | null;
  threadId: string | null;
  locale: string;
  theme: "light" | "dark";
}

export interface ToolPresentation {
  location: "inline" | "side-panel";
  active: boolean;
  state?: PluginJson;
}

export type PluginSurfaceName = "settings" | "composer";
export type PluginSurfaceLocation = "connectors" | "expert" | "chat";

export type PluginReadinessState =
  | { readonly status: "loading" }
  | {
      readonly status: "ready";
      readonly requiredPlugins: Readonly<Record<string, string>>;
      readonly unavailablePlugins: readonly string[];
    }
  | {
      readonly status: "error";
      readonly reason: "unavailable" | "denied";
    };

export interface PluginSurfaceContext extends ToolRenderContext {
  readonly generation: string;
  readonly timeZone: string;
  readonly readiness: PluginReadinessState;
}

export interface PluginSurfaceProps {
  host: OctopPluginUIHost;
  context: PluginSurfaceContext;
  location: PluginSurfaceLocation;
}

export type PluginSurfaceComponent = ComponentType<PluginSurfaceProps>;

export interface PluginSurfaceRegistration {
  readonly pluginId: string;
  readonly surface: PluginSurfaceName;
  readonly component: PluginSurfaceComponent;
  readonly loaderGeneration: number;
  readonly isLoaderActive: () => boolean;
  readonly token: number;
  readonly registrationToken?: object;
}

export type PluginJson =
  | null
  | boolean
  | number
  | string
  | readonly PluginJson[]
  | { readonly [key: string]: PluginJson };

export type PluginJsonObject = { readonly [key: string]: PluginJson };

export type ComposerSelectionCapture =
  | { readonly state: "uninitialized" }
  | { readonly state: "selected"; readonly value: PluginJsonObject };

export interface ComposerPreparationHandler {
  captureSelection(): ComposerSelectionCapture;
  prepare(input: {
    readonly selection: ComposerSelectionCapture;
    readonly target: { readonly agentId: string; readonly threadId: string };
    readonly text: string;
    readonly attachments: readonly unknown[];
    readonly generation: string;
    readonly signal: AbortSignal;
  }): Promise<PluginJsonObject>;
  restoreSelection(selection: ComposerSelectionCapture): Promise<void>;
}

export interface CapturedComposerPreparation {
  readonly pluginId: string;
  readonly generation: string;
  readonly selection: ComposerSelectionCapture;
  readonly isCurrent: () => boolean;
  readonly prepare: ComposerPreparationHandler["prepare"];
  readonly restoreSelection: ComposerPreparationHandler["restoreSelection"];
}

export interface ToolRenderProps {
  pluginId: string;
  toolName: string;
  displayName?: string;
  callId?: string;
  status: ToolRenderStatus;
  args: unknown;
  /** Parsed ``data`` field, or full parsed object / raw string. */
  data: unknown;
  textFallback?: string;
  host: OctopPluginUIHost;
  presentation?: ToolPresentation;
  /** Original tool output string (for builtins that need media parsing). */
  output?: string;
  isStreaming: boolean;
  hideMediaPreview?: boolean;
  onAcpPermissionSelect?: (message: string) => void;
  agentId?: string | null;
}

export interface ToolRendererRegistration {
  /** Plugin-local renderer id (matches ``octop_ui.renderer``). */
  id: string;
  /** Owning plugin id (``builtin`` for first-party renderers). */
  pluginId: string;
  /** Tool names this renderer handles when ``octop_ui`` is absent. */
  tools?: string[];
  component: ComponentType<ToolRenderProps>;
}

export interface OpenSidePanelOptions {
  callId: string;
  title?: string;
  toolName?: string;
  state?: PluginJson;
}

export interface OctopPluginUIHost {
  registerRenderer(
    reg: Omit<ToolRendererRegistration, "pluginId"> & { pluginId?: string },
  ): void;
  registerSurface(
    surface: PluginSurfaceName,
    component: PluginSurfaceComponent,
  ): () => void;
  getContext(): PluginSurfaceContext | null;
  registerComposerPreparation(handler: ComposerPreparationHandler): () => void;
  getToolContext(): ToolRenderContext;
  /** L2: update display payload for a tool call without re-running the LLM. */
  patchResult(callId: string, nextData: unknown): void;
  /** Authenticated Octop API request (path starts with ``/`` under ``/api``). */
  request<T = unknown>(path: string, init?: RequestInit): Promise<T>;
  /** Open this tool call's UI in the chat side dock (one tab per callId). */
  openSidePanel(opts: OpenSidePanelOptions): void;
  /** Close the side dock tab for this tool call, if open. */
  closeSidePanel(callId: string): void;
}

/** Shape expected from ``ui/dist/index.js``. */
export interface PluginUiModule {
  setup?: (host: OctopPluginUIHost) => void | (() => void);
  default?: { setup?: (host: OctopPluginUIHost) => void | (() => void) };
}

export interface PluginUiManifest {
  renderers?: Array<{
    id: string;
    tools?: string[];
  }>;
}
