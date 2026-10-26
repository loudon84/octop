import * as React from "./host";
import type { PluginHost, PluginSurfaceContext } from "./host";
import { classifyTransportError } from "./api";
import { t, type MessageKey } from "./i18n";
import {
  getSearchSettings,
  recoverSearchSettings,
  saveSearchSettings,
  testSearchSource,
  type KeepReplaceClear,
  type SearchDiagnosticView,
  type SearchRole,
  type SearchSettingsAdmin,
} from "./searchApi";

type Props = {
  host: PluginHost;
  context: PluginSurfaceContext;
};

type SourceDraft = {
  base_url: string;
  reported_version: string;
  ca_op: KeepReplaceClear;
  ca_bundle: string;
  credential_op: KeepReplaceClear;
  credential: string;
};

type Draft = {
  revision: number;
  primary: SourceDraft;
  secondary: SourceDraft;
};

function emptySource(): SourceDraft {
  return {
    base_url: "",
    reported_version: "",
    ca_op: "KEEP",
    ca_bundle: "",
    credential_op: "KEEP",
    credential: "",
  };
}

function draftFromSettings(settings: SearchSettingsAdmin): Draft {
  return {
    revision: settings.revision,
    primary: {
      base_url: settings.primary.base_url ?? "",
      reported_version: settings.primary.reported_version ?? "",
      ca_op: "KEEP",
      ca_bundle: "",
      credential_op: "KEEP",
      credential: "",
    },
    secondary: {
      base_url: settings.secondary.base_url ?? "",
      reported_version: settings.secondary.reported_version ?? "",
      ca_op: "KEEP",
      ca_bundle: "",
      credential_op: "KEEP",
      credential: "",
    },
  };
}

function errorCopy(error: unknown, locale: string): string {
  const classified = classifyTransportError(error);
  if (classified.status === 403) return t(locale, "denied");
  if (classified.code === "RECOVERY_BLOCKED")
    return t(locale, "searchRecoveryBlocked");
  if (classified.status === 409 || classified.code === "CONFIG_CONFLICT")
    return t(locale, "conflict");
  return t(locale, "error");
}

function formatDiagTime(
  value: string | null,
  locale: string,
  timeZone: string,
): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
  }).format(date);
}

function buildPart(draft: SourceDraft, role: SearchRole) {
  const part: {
    base_url: string;
    ca_op: KeepReplaceClear;
    ca_bundle?: string | null;
    credential_op: KeepReplaceClear;
    credential?: string | null;
    reported_version: string | null;
  } = {
    base_url: draft.base_url,
    ca_op: draft.ca_op,
    credential_op: role === "PRIMARY" ? "KEEP" : draft.credential_op,
    reported_version: draft.reported_version || null,
  };
  if (draft.ca_op === "REPLACE") part.ca_bundle = draft.ca_bundle;
  if (role === "SECONDARY" && draft.credential_op === "REPLACE") {
    part.credential = draft.credential;
  }
  return part;
}

export function SearchSettings({ host, context }: Props) {
  const { agentId, generation } = context;
  const locale = context.locale;
  const text = (key: MessageKey) => t(locale, key);
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [recovering, setRecovering] = React.useState(false);
  const [error, setError] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [conflict, setConflict] = React.useState(false);
  const [settings, setSettings] = React.useState<SearchSettingsAdmin | null>(
    null,
  );
  const [draft, setDraft] = React.useState<Draft | null>(null);
  const [diagnostics, setDiagnostics] = React.useState<SearchDiagnosticView[]>(
    [],
  );
  const [testRole, setTestRole] = React.useState<SearchRole>("PRIMARY");
  const [publicQuery, setPublicQuery] = React.useState("");
  const [confirmQuery, setConfirmQuery] = React.useState(false);
  const loadGen = React.useRef(0);
  const scopeRef = React.useRef({ agentId, generation });
  scopeRef.current = { agentId, generation };
  const configRevision = React.useRef(0);

  const applySettings = React.useCallback((next: SearchSettingsAdmin) => {
    setSettings(next);
    setDraft(draftFromSettings(next));
    setDiagnostics([...next.diagnostics]);
    configRevision.current = next.revision;
  }, []);

  const refresh = React.useCallback(async () => {
    const gen = ++loadGen.current;
    const key = `${generation}:${agentId ?? ""}`;
    const isScopeCurrent = () => {
      const current = scopeRef.current;
      return `${current.generation}:${current.agentId ?? ""}` === key;
    };
    setLoading(true);
    setError("");
    setConflict(false);
    try {
      const next = await getSearchSettings(host);
      if (gen !== loadGen.current || !isScopeCurrent()) return;
      applySettings(next);
      setNotice(t(locale, "loaded"));
    } catch (err) {
      if (gen !== loadGen.current || !isScopeCurrent()) return;
      setError(errorCopy(err, locale));
      setSettings(null);
      setDraft(null);
    } finally {
      if (gen === loadGen.current) setLoading(false);
    }
  }, [agentId, applySettings, generation, host, locale]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const updateSource = (
    role: "primary" | "secondary",
    patch: Partial<SourceDraft>,
  ) => {
    setDraft((current) =>
      current
        ? { ...current, [role]: { ...current[role], ...patch } }
        : current,
    );
  };

  const onSave = async (event: { preventDefault(): void }) => {
    event.preventDefault();
    if (!draft || saving || recovering) return;
    if (!draft.primary.base_url.trim() || !draft.secondary.base_url.trim()) {
      setError(text("invalidFields"));
      return;
    }
    if (draft.primary.ca_op === "REPLACE" && !draft.primary.ca_bundle.trim()) {
      setError(text("invalidFields"));
      return;
    }
    if (
      draft.secondary.ca_op === "REPLACE" &&
      !draft.secondary.ca_bundle.trim()
    ) {
      setError(text("invalidFields"));
      return;
    }
    if (
      draft.secondary.credential_op === "REPLACE" &&
      !draft.secondary.credential
    ) {
      setError(text("invalidFields"));
      return;
    }
    setSaving(true);
    setError("");
    setNotice(text("saving"));
    try {
      const saved = await saveSearchSettings(host, {
        expected_revision: draft.revision,
        primary: buildPart(draft.primary, "PRIMARY"),
        secondary: buildPart(draft.secondary, "SECONDARY"),
      });
      applySettings(saved);
      setNotice(text("saved"));
      setConflict(false);
    } catch (err) {
      const classified = classifyTransportError(err);
      if (classified.status === 409 || classified.code === "CONFIG_CONFLICT") {
        setConflict(true);
        setError(text("conflict"));
      } else {
        setError(errorCopy(err, locale));
      }
    } finally {
      setSaving(false);
    }
  };

  const onTest = async () => {
    if (!draft || testing || recovering || !confirmQuery || !publicQuery)
      return;
    setTesting(true);
    setError("");
    const expected = draft.revision;
    try {
      const result = await testSearchSource(host, {
        source_role: testRole,
        expected_revision: expected,
        public_query: publicQuery,
      });
      // Late diagnostic must not overwrite a newer saved configuration.
      if (configRevision.current !== expected) {
        setNotice(text("searchStaleDiagnostic"));
        return;
      }
      applySettings(result.settings);
      setDiagnostics((current) => {
        const others = current.filter(
          (item) => item.source_role !== result.diagnostic.source_role,
        );
        return [...others, result.diagnostic];
      });
      setNotice(text("searchTestDone"));
      setConfirmQuery(false);
    } catch (err) {
      setError(errorCopy(err, locale));
    } finally {
      setTesting(false);
    }
  };

  const onRecover = async () => {
    if (!draft || saving || recovering || testing) return;
    setRecovering(true);
    setError("");
    try {
      const recovered = await recoverSearchSettings(host, draft.revision);
      applySettings(recovered);
      setNotice(text("searchRecovered"));
    } catch (err) {
      setError(errorCopy(err, locale));
    } finally {
      setRecovering(false);
    }
  };

  if (loading && !settings) {
    return <p role="status">{text("searchLoading")}</p>;
  }
  if (!settings || !draft) {
    return (
      <div className="ek-section">
        <h3>{text("searchTitle")}</h3>
        {error && <p role="alert">{error}</p>}
        <button type="button" onClick={() => void refresh()}>
          {text("retry")}
        </button>
      </div>
    );
  }

  const blocked = settings.recovery_blocked;
  const formLocked = blocked || saving || recovering;
  const diagFor = (role: SearchRole) =>
    diagnostics.find((item) => item.source_role === role);

  const renderSource = (
    role: "primary" | "secondary",
    label: MessageKey,
    provider: string,
    configured: {
      ca: boolean;
      credential: boolean;
    },
  ) => {
    const source = draft[role];
    const searchRole: SearchRole = role === "primary" ? "PRIMARY" : "SECONDARY";
    return (
      <fieldset disabled={formLocked}>
        <legend>
          {text(label)} ({provider})
        </legend>
        <label>
          {text("searchAddress")}
          <input
            aria-label={`${text(label)} ${text("searchAddress")}`}
            value={source.base_url}
            onChange={(event: { currentTarget: HTMLInputElement }) =>
              updateSource(role, { base_url: event.currentTarget.value })
            }
          />
        </label>
        <label>
          {text("searchReportedVersion")}
          <input
            aria-label={`${text(label)} ${text("searchReportedVersion")}`}
            value={source.reported_version}
            onChange={(event: { currentTarget: HTMLInputElement }) =>
              updateSource(role, {
                reported_version: event.currentTarget.value,
              })
            }
          />
        </label>
        <label>
          {text("searchCaOp")}
          <select
            aria-label={`${text(label)} ${text("searchCaOp")}`}
            value={source.ca_op}
            onChange={(event: { currentTarget: HTMLSelectElement }) =>
              updateSource(role, {
                ca_op: event.currentTarget.value as KeepReplaceClear,
                ca_bundle:
                  event.currentTarget.value === "REPLACE"
                    ? source.ca_bundle
                    : "",
              })
            }
          >
            <option value="KEEP">{text("searchOpKeep")}</option>
            <option value="REPLACE">{text("searchOpReplace")}</option>
            <option value="CLEAR">{text("searchOpClear")}</option>
          </select>
        </label>
        {configured.ca && source.ca_op === "KEEP" && (
          <p className="ek-muted" role="status">
            {text("searchCaConfigured")}
          </p>
        )}
        {source.ca_op === "REPLACE" && (
          <label>
            {text("searchCaBundle")}
            <textarea
              aria-label={`${text(label)} ${text("searchCaBundle")}`}
              value={source.ca_bundle}
              rows={3}
              onChange={(event: { currentTarget: HTMLTextAreaElement }) =>
                updateSource(role, { ca_bundle: event.currentTarget.value })
              }
            />
          </label>
        )}
        {role === "secondary" && (
          <>
            <label>
              {text("searchCredentialOp")}
              <select
                aria-label={`${text(label)} ${text("searchCredentialOp")}`}
                value={source.credential_op}
                onChange={(event: { currentTarget: HTMLSelectElement }) =>
                  updateSource(role, {
                    credential_op: event.currentTarget
                      .value as KeepReplaceClear,
                    credential:
                      event.currentTarget.value === "REPLACE"
                        ? source.credential
                        : "",
                  })
                }
              >
                <option value="KEEP">{text("searchOpKeep")}</option>
                <option value="REPLACE">{text("searchOpReplace")}</option>
                <option value="CLEAR">{text("searchOpClear")}</option>
              </select>
            </label>
            {configured.credential && source.credential_op === "KEEP" && (
              <p className="ek-muted" role="status">
                {text("credentialSet")}
              </p>
            )}
            {source.credential_op === "REPLACE" && (
              <label>
                {text("searchCredential")}
                <input
                  type="password"
                  autoComplete="new-password"
                  aria-label={`${text(label)} ${text("searchCredential")}`}
                  value={source.credential}
                  onChange={(event: { currentTarget: HTMLInputElement }) =>
                    updateSource(role, {
                      credential: event.currentTarget.value,
                    })
                  }
                />
              </label>
            )}
          </>
        )}
        {role === "primary" && (
          <p className="ek-muted" role="status">
            {text("searchNoSearxCredential")}
          </p>
        )}
        {(() => {
          const diag = diagFor(searchRole);
          if (!diag) return null;
          const when = formatDiagTime(diag.tested_at, locale, context.timeZone);
          return (
            <p role="status">
              {text("searchDiagnostic")}: {diag.status}
              {diag.code ? ` (${diag.code})` : ""}
              {diag.latency_ms != null ? ` · ${diag.latency_ms}ms` : ""}
              {diag.hit_count != null
                ? ` · ${text("searchHitCount")}: ${diag.hit_count}`
                : ""}
              {` · ${text("revision")}: ${diag.observed_revision}`}
              {when ? ` · ${text("checkedAt")}: ${when}` : ""}
            </p>
          );
        })()}
      </fieldset>
    );
  };

  return (
    <div className="ek-section">
      <h3>{text("searchTitle")}</h3>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {conflict && (
        <button type="button" onClick={() => void refresh()}>
          {text("reload")}
        </button>
      )}
      <p role="status">
        {text("revision")}: {settings.revision}
        {" · "}
        {text("searchPrimaryReady")}:{" "}
        {settings.primary_ready ? text("searchYes") : text("searchNo")}
        {" · "}
        {text("searchSecondaryReady")}:{" "}
        {settings.secondary_ready ? text("searchYes") : text("searchNo")}
        {" · "}
        {text("searchWebReady")}:{" "}
        {settings.web_search_ready ? text("searchYes") : text("searchNo")}
      </p>
      {!settings.web_search_ready && (
        <p className="ek-muted" role="status">
          {text("searchWebNotReadyHint")}
        </p>
      )}
      {blocked && (
        <div>
          <p role="alert">{text("searchRecoveryBlocked")}</p>
          <button
            type="button"
            disabled={recovering || saving || testing}
            onClick={() => void onRecover()}
          >
            {text("searchRecover")}
          </button>
        </div>
      )}
      <form
        onSubmit={(event: { preventDefault(): void }) => void onSave(event)}
      >
        {renderSource("primary", "searchPrimary", "SEARXNG", {
          ca: settings.primary.ca_bundle_configured,
          credential: false,
        })}
        {renderSource("secondary", "searchSecondary", "FIRECRAWL", {
          ca: settings.secondary.ca_bundle_configured,
          credential: settings.secondary.credential_configured,
        })}
        <button type="submit" disabled={formLocked}>
          {text("searchSave")}
        </button>
      </form>
      <fieldset disabled={blocked || testing || recovering}>
        <legend>{text("searchTest")}</legend>
        <label>
          {text("searchTestSource")}
          <select
            aria-label={text("searchTestSource")}
            value={testRole}
            onChange={(event: { currentTarget: HTMLSelectElement }) =>
              setTestRole(event.currentTarget.value as SearchRole)
            }
          >
            <option value="PRIMARY">{text("searchPrimary")}</option>
            <option value="SECONDARY">{text("searchSecondary")}</option>
          </select>
        </label>
        <label>
          {text("searchPublicQuery")}
          <input
            aria-label={text("searchPublicQuery")}
            value={publicQuery}
            onChange={(event: { currentTarget: HTMLInputElement }) => {
              setPublicQuery(event.currentTarget.value);
              setConfirmQuery(false);
            }}
          />
        </label>
        <label className="ek-choice">
          <input
            type="checkbox"
            checked={confirmQuery}
            onChange={(event: { currentTarget: HTMLInputElement }) =>
              setConfirmQuery(event.currentTarget.checked)
            }
          />
          {text("searchConfirmQuery")}
        </label>
        <button
          type="button"
          disabled={
            blocked || testing || recovering || !confirmQuery || !publicQuery
          }
          onClick={() => void onTest()}
        >
          {text("searchRunTest")}
        </button>
      </fieldset>
    </div>
  );
}
