import { action, type ActionHost } from "./api";

export const SCHEMA_ID = "octop.enterprise.knowledge.v1" as const;
export type KnowledgeMode = "KB_ONLY" | "WEB_ONLY" | "HYBRID";

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

export function getSession(host: ActionHost) {
  return action<SessionOutput>(host, "session.get", {});
}

export function saveSession(
  host: ActionHost,
  payload: {
    expected_revision: number;
    selected_kb_ids: string[];
    mode: KnowledgeMode;
    source_revisions: SessionSourceRevisions;
  },
) {
  return action<SessionOutput>(host, "session.save", {
    schema_id: SCHEMA_ID,
    version: 1,
    ...payload,
  });
}

export function loadCatalog(
  host: ActionHost,
  connection_id: string,
  expected_revision: number,
) {
  return action<CatalogOutput>(host, "connection.catalog", {
    connection_id,
    expected_revision,
  });
}

export function prepareOperation(host: ActionHost, expected_session_revision: number) {
  return action<OperationOutput>(host, "operation.prepare", {
    schema_id: SCHEMA_ID,
    version: 1,
    expected_session_revision,
  });
}

export function approveQuery(
  host: ActionHost,
  operation_id: string,
  public_query: string,
) {
  return action<QueryApprovalOutput>(host, "query.approve", {
    operation_id,
    public_query,
  });
}
