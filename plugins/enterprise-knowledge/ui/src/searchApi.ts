import { action, type ActionHost } from "./api";

export type SearchRole = "PRIMARY" | "SECONDARY";
export type KeepReplaceClear = "KEEP" | "REPLACE" | "CLEAR";

export type SearchSourceView = {
  role: SearchRole;
  provider: "SEARXNG" | "FIRECRAWL";
  base_url: string | null;
  ca_bundle_configured: boolean;
  credential_configured: boolean;
  reported_version: string | null;
  origin_relation: "UNKNOWN";
};

export type SearchDiagnosticView = {
  source_role: SearchRole;
  status: string;
  code: string | null;
  latency_ms: number | null;
  hit_count: number | null;
  results_truncated: boolean | null;
  observed_revision: number;
  tested_at: string | null;
};

export type SearchSettingsAdmin = {
  revision: number;
  recovery_blocked: boolean;
  primary: SearchSourceView;
  secondary: SearchSourceView;
  primary_ready: boolean;
  secondary_ready: boolean;
  web_search_ready: boolean;
  diagnostics: SearchDiagnosticView[];
};

export type SearchSourceSavePart = {
  base_url: string;
  ca_op: KeepReplaceClear;
  ca_bundle?: string | null;
  credential_op: KeepReplaceClear;
  credential?: string | null;
  reported_version?: string | null;
};

export type SearchSaveInput = {
  expected_revision: number;
  primary: SearchSourceSavePart;
  secondary: SearchSourceSavePart;
};

export type SearchTestInput = {
  source_role: SearchRole;
  expected_revision: number;
  public_query: string;
};

export type SearchTestOutput = {
  settings: SearchSettingsAdmin;
  diagnostic: SearchDiagnosticView;
};

export function getSearchSettings(host: ActionHost) {
  return action<SearchSettingsAdmin>(host, "search.get", {});
}

export function saveSearchSettings(host: ActionHost, payload: SearchSaveInput) {
  return action<SearchSettingsAdmin>(host, "search.save", payload);
}

export function testSearchSource(host: ActionHost, payload: SearchTestInput) {
  return action<SearchTestOutput>(host, "search.test", payload);
}

export function recoverSearchSettings(
  host: ActionHost,
  expected_revision: number,
) {
  return action<SearchSettingsAdmin>(host, "search.recover", {
    expected_revision,
  });
}
