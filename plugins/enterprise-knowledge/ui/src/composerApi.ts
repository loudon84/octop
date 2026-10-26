import { action } from "./api";
import type { PluginHost } from "./host";

export const SCHEMA_ID = "octop.enterprise.knowledge.v1" as const;
export type KnowledgeMode = "KB_ONLY" | "WEB_ONLY" | "HYBRID";
export type ActionTarget = {
  agentId: string;
  threadId: string;
  generation: string;
};

export type SessionCapabilities = {
  can_save_selection: boolean;
  can_select_knowledge: boolean;
  knowledge_ready: boolean;
  web_ready: boolean;
};

export type SessionSnapshot = {
  schema_id: typeof SCHEMA_ID;
  version: number;
  agent_id: string;
  thread_id: string;
  revision: number;
  selection_present: boolean;
  selected_kb_ids: string[];
  mode: KnowledgeMode;
  connection_id: string | null;
  config_revision: number;
  binding_revision: number;
  grant_revision: number;
  search_revision: number;
  invalid: boolean;
  invalid_reason: string | null;
  capabilities: SessionCapabilities;
  search_ready: boolean;
};

export type SessionOutput = {
  schema_id: typeof SCHEMA_ID;
  version: number;
  session: SessionSnapshot;
};

export type SessionSourceRevisions = {
  connection_id: string | null;
  config_revision: number;
  binding_revision: number;
  grant_revision: number;
  search_revision: number;
};

export type CatalogItem = { kb_id: string; name: string };

async function actionAtTarget<T>(
  host: PluginHost,
  name: string,
  payload: object,
  target: ActionTarget,
  signal?: AbortSignal,
): Promise<T> {
  const isCurrent = () => {
    const current = host.getContext();
    return Boolean(
      current &&
        current.agentId === target.agentId &&
        current.generation === target.generation &&
        (!current.threadId || current.threadId === target.threadId),
    );
  };
  if (!target.agentId || !target.threadId || !isCurrent())
    throw new Error("Composer context changed");
  if (signal?.aborted) throw new Error("Composer preparation cancelled");
  const targetHost = {
    getContext: () => ({ agentId: target.agentId, threadId: target.threadId }),
    request: <T = unknown>(path: string, init?: RequestInit) =>
      host.request<T>(path, { ...init, signal }),
  };
  let response: T;
  try {
    response = await action<T>(targetHost, name, payload);
  } catch (error) {
    if (!isCurrent()) throw new Error("Composer context changed");
    if (signal?.aborted) throw new Error("Composer preparation cancelled");
    throw error;
  }
  if (!isCurrent()) throw new Error("Composer context changed");
  if (signal?.aborted) throw new Error("Composer preparation cancelled");
  return response;
}

function sessionAction<T>(
  host: PluginHost,
  name: string,
  payload: object,
  target?: ActionTarget,
  signal?: AbortSignal,
): Promise<T> {
  return target
    ? actionAtTarget(host, name, payload, target, signal)
    : action<T>(host, name, payload);
}

export type CatalogOutput = {
  connection_id: string;
  observed_revision: number;
  knowledge_bases: CatalogItem[];
};

export type OperationRecord = {
  operation_id: string;
  session_revision: number;
  status: string;
};

export type OperationOutput = {
  schema_id: typeof SCHEMA_ID;
  version: number;
  operation: OperationRecord;
};

export type QueryApprovalOutput = {
  approval: {
    approval_id: string;
    operation_id: string;
    public_query: string | null;
    state: "PENDING" | "CONSUMED" | "CANCELLED" | "INVALID";
  };
};

export function getSession(
  host: PluginHost,
  target?: ActionTarget,
  signal?: AbortSignal,
) {
  return sessionAction<SessionOutput>(host, "session.get", {}, target, signal);
}

export function saveSession(
  host: PluginHost,
  payload: {
    expected_revision: number;
    selected_kb_ids: string[];
    mode: KnowledgeMode;
    source_revisions: SessionSourceRevisions;
  },
  target?: ActionTarget,
  signal?: AbortSignal,
) {
  return sessionAction<SessionOutput>(
    host,
    "session.save",
    {
      schema_id: SCHEMA_ID,
      version: 1,
      ...payload,
    },
    target,
    signal,
  );
}

export function loadCatalog(
  host: PluginHost,
  connection_id: string,
  expected_revision: number,
  target?: ActionTarget,
  signal?: AbortSignal,
) {
  return sessionAction<CatalogOutput>(
    host,
    "connection.catalog",
    {
      connection_id,
      expected_revision,
    },
    target,
    signal,
  );
}

export function prepareOperation(
  host: PluginHost,
  expected_session_revision: number,
  target?: ActionTarget,
  signal?: AbortSignal,
) {
  return sessionAction<OperationOutput>(
    host,
    "operation.prepare",
    {
      schema_id: SCHEMA_ID,
      version: 1,
      expected_session_revision,
    },
    target,
    signal,
  );
}

export function approveQuery(
  host: PluginHost,
  operation_id: string,
  public_query: string,
  target?: ActionTarget,
  signal?: AbortSignal,
) {
  return sessionAction<QueryApprovalOutput>(
    host,
    "query.approve",
    {
      operation_id,
      public_query,
    },
    target,
    signal,
  );
}
