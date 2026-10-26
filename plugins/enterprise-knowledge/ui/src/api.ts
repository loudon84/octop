import type {
  PluginHost,
  PluginSurfaceContext,
  PluginToolContext,
  PluginToolHost,
} from "./host";

export type ActionHost = Pick<PluginHost, "request"> & {
  getContext(): Pick<PluginSurfaceContext, "agentId" | "threadId"> | null;
};

export async function action<T>(
  host: ActionHost,
  name: string,
  payload: object,
): Promise<T> {
  const context = host.getContext();
  const response = await host.request<{ api_version: number; result: T }>(
    `/plugins/enterprise-knowledge/actions/${name}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_version: 1,
        context: {
          agent_id: context?.agentId ?? null,
          thread_id: context?.threadId ?? null,
        },
        payload,
      }),
    },
  );
  if (response.api_version !== 1 || !response.result)
    throw new Error("Invalid plugin action response");
  return response.result;
}

export type EvidenceActionName =
  | "evidence.list"
  | "evidence.read"
  | "evidence.original";

export class ToolActionError extends Error {
  constructor(
    readonly status: number | null,
    readonly code: string | null,
  ) {
    super("The evidence request could not be completed.");
    this.name = "ToolActionError";
  }
}

function sameToolTarget(
  host: PluginToolHost,
  target: Pick<PluginToolContext, "agentId" | "threadId">,
): boolean {
  const current = host.getToolContext();
  return Boolean(
    current?.agentId &&
      current?.threadId &&
      current.agentId === target.agentId &&
      current.threadId === target.threadId,
  );
}

export async function toolAction<T>(
  host: PluginToolHost,
  target: Pick<PluginToolContext, "agentId" | "threadId">,
  name: EvidenceActionName | string,
  payload: object,
  signal?: AbortSignal,
): Promise<T> {
  if (
    name !== "evidence.list" &&
    name !== "evidence.read" &&
    name !== "evidence.original"
  ) {
    throw new Error("Unsupported evidence action");
  }
  if (!target.agentId || !target.threadId || !sameToolTarget(host, target))
    throw new Error("Evidence context changed");
  if (signal?.aborted) throw new Error("Evidence request cancelled");
  let response: { api_version?: unknown; result?: unknown };
  try {
    response = await host.request<{ api_version?: unknown; result?: unknown }>(
      `/plugins/enterprise-knowledge/actions/${name}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_version: 1,
          context: { agent_id: target.agentId, thread_id: target.threadId },
          payload,
        }),
        signal,
      },
    );
  } catch (error) {
    if (!sameToolTarget(host, target))
      throw new Error("Evidence context changed");
    const classified = classifyTransportError(error);
    throw new ToolActionError(classified.status, classified.code);
  }
  if (!sameToolTarget(host, target))
    throw new Error("Evidence context changed");
  if (response?.api_version !== 1 || response.result === undefined)
    throw new Error("Invalid evidence action response");
  return response.result as T;
}

export function classifyTransportError(error: unknown): {
  status: number | null;
  code: string | null;
} {
  const message = error instanceof Error ? error.message : "";
  const status =
    Number(message.match(/Request failed:\s*(\d{3})/)?.[1]) || null;
  const bodyStart = message.indexOf(" - ");
  if (bodyStart < 0) return { status, code: null };
  try {
    const body = JSON.parse(message.slice(bodyStart + 3)) as {
      error?: { code?: unknown };
    };
    const code =
      typeof body.error?.code === "string" &&
      /^[A-Z0-9_]{1,64}$/.test(body.error.code)
        ? body.error.code
        : null;
    return { status, code };
  } catch {
    return { status, code: null };
  }
}

export function contextKey(
  context: PluginSurfaceContext,
  location: string,
): string {
  return `${context.generation}:${context.agentId ?? ""}:${location}`;
}
