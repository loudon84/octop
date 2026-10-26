import * as React from "./host";
import type { PluginHost, PluginSurfaceContext } from "./host";
import { action, classifyTransportError, contextKey } from "./api";
import { t, type MessageKey } from "./i18n";
import { SearchSettings } from "./SearchSettings";

type Connection = {
  connection_id: string;
  revision: number;
  name: string;
  base_url: string;
  tenant_id: string | null;
  allowed_kb_ids: string[];
  enabled: boolean;
  credential_configured: boolean;
  probe_status:
    | "CONFIGURED"
    | "DISABLED"
    | "READY"
    | "AUTH_FAILED"
    | "UNREACHABLE"
    | "DEGRADED";
  probe_components: Record<
    string,
    {
      status: string;
      error_code?: string | null;
      observed_revision: number;
      checked_at: string;
    }
  >;
};
type Grant = {
  connection_id: string;
  revision: number;
  user_id: number;
  user_allowed_kb_ids: string[];
};
type Binding = {
  agent_id: string;
  revision: number;
  connection_ref: string | null;
  expert_allowed_kb_ids: string[];
  default_kb_ids: string[];
  web_allowed: boolean;
  enabled: boolean;
};
type SettingsData = {
  capabilities: Record<string, boolean>;
  connections: Connection[];
  grants: Grant[];
};
type CatalogItem = { kb_id: string; name: string };
type SurfaceProps = {
  host: PluginHost;
  context: PluginSurfaceContext;
  location: "connectors" | "expert" | "chat";
};
type Draft = {
  name: string;
  base_url: string;
  tenant_id: string;
  api_key: string;
  allowed_kb_ids: string[];
  enabled: boolean;
};
type ProbeState = { status: string; revision: number };

const emptyDraft = (): Draft => ({
  name: "",
  base_url: "",
  tenant_id: "",
  api_key: "",
  allowed_kb_ids: [],
  enabled: false,
});
const arrayToggle = (values: string[], id: string) =>
  values.includes(id)
    ? values.filter((value) => value !== id)
    : [...values, id];

function errorCopy(error: unknown, locale: string): string {
  const classified = classifyTransportError(error);
  if (classified.status === 403) return t(locale, "denied");
  if (classified.status === 409 || classified.code === "CONFIG_CONFLICT")
    return t(locale, "conflict");
  return t(locale, "error");
}

function statusCopy(connection: Connection, locale: string): string {
  if (!connection.enabled || connection.probe_status === "DISABLED")
    return t(locale, "disabledStatus");
  if (connection.probe_status === "READY") return t(locale, "readyStatus");
  if (connection.probe_status === "CONFIGURED")
    return t(locale, "configuredStatus");
  return connection.probe_status;
}

function formatProbeTime(
  value: string,
  locale: string,
  timeZone: string,
): string | null {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
  }).format(date);
}

function Checks({
  items,
  selected,
  onToggle,
  label,
  locale,
  removable = [],
}: {
  items: CatalogItem[];
  selected: string[];
  onToggle: (id: string) => void;
  label: string;
  locale: string;
  removable?: string[];
}) {
  const visible = new Set(items.map((item) => item.kb_id));
  return (
    <fieldset>
      <legend>{label}</legend>
      {items.map((item) => (
        <label key={item.kb_id} className="ek-choice">
          <input
            type="checkbox"
            checked={selected.includes(item.kb_id)}
            onChange={() => onToggle(item.kb_id)}
          />{" "}
          {item.name} <small>{item.kb_id}</small>
        </label>
      ))}
      {removable
        .filter((id) => !visible.has(id))
        .map((id) => (
          <div key={id} className="ek-choice ek-old-choice">
            <span>
              {t(locale, "oldChoice")}: {id}
            </span>
            <button type="button" onClick={() => onToggle(id)}>
              {t(locale, "remove")}
            </button>
          </div>
        ))}
      {items.length === 0 && removable.length === 0 && (
        <p>{t(locale, "emptyAllowed")}</p>
      )}
    </fieldset>
  );
}

export function Settings({ host, context, location }: SurfaceProps) {
  const locale = context.locale;
  const [loadState, setLoadState] = React.useState("loading");
  const [profile, setProfile] = React.useState<{
    role?: string;
    permissions?: string[];
  } | null>(null);
  const [settings, setSettings] = React.useState<SettingsData | null>(null);
  const [binding, setBinding] = React.useState<Binding | null>(null);
  const [notice, setNotice] = React.useState("");
  const [error, setError] = React.useState("");
  const [conflict, setConflict] = React.useState(false);
  const [connectionId, setConnectionId] = React.useState<string | null>(null);
  const [connectionRefreshVersion, setConnectionRefreshVersion] =
    React.useState(0);
  const [draft, setDraft] = React.useState<Draft>(emptyDraft);
  const [catalog, setCatalog] = React.useState<CatalogItem[]>([]);
  const [catalogLoadedFor, setCatalogLoadedFor] = React.useState("");
  const [probeIds, setProbeIds] = React.useState<string[]>([]);
  const [probeQuery, setProbeQuery] = React.useState("");
  const [probeDocument, setProbeDocument] = React.useState("");
  const [probeState, setProbeState] = React.useState<
    Record<string, ProbeState>
  >({});
  const [busy, setBusy] = React.useState("");
  const revisionGeneration = React.useRef(0);
  const [grantKey, setGrantKey] = React.useState("new");
  const [grantUserId, setGrantUserId] = React.useState("");
  const [grantIds, setGrantIds] = React.useState<string[]>([]);
  const [bindingCatalog, setBindingCatalog] = React.useState<CatalogItem[]>([]);
  const [bindingCatalogFor, setBindingCatalogFor] = React.useState("");
  const [bindingIds, setBindingIds] = React.useState<string[]>([]);
  const [defaultIds, setDefaultIds] = React.useState<string[]>([]);
  const [bindingConnection, setBindingConnection] = React.useState("");
  const [webAllowed, setWebAllowed] = React.useState(false);
  const [bindingEnabled, setBindingEnabled] = React.useState(false);
  const generation = React.useRef(0);
  const key = contextKey(context, location);
  const selectedConnection = settings?.connections.find(
    (item) => item.connection_id === connectionId,
  );

  const loadSettings = async (
    profileValue: { role?: string; permissions?: string[] },
    activeGeneration: number,
  ) => {
    const isAdmin = profileValue.role === "admin";
    const canConfigureExpert =
      (profileValue.permissions ?? []).includes("agent_plugins") &&
      Boolean(context.agentId);
    if (location === "connectors" && !isAdmin) {
      setLoadState("unavailable");
      return;
    }
    if (!isAdmin && (location !== "expert" || !canConfigureExpert)) {
      setLoadState(context.agentId ? "unavailable" : "target");
      return;
    }
    if (location === "expert" && !context.agentId) {
      setLoadState("target");
      return;
    }
    try {
      const data = await action<SettingsData>(host, "settings.get", {});
      if (generation.current !== activeGeneration) return;
      setSettings(data);
      const selected = data.connections[0]?.connection_id ?? null;
      setConnectionId((current) =>
        current &&
        data.connections.some((item) => item.connection_id === current)
          ? current
          : selected,
      );
      if (
        location === "expert" &&
        data.capabilities.can_manage_bindings &&
        context.agentId
      ) {
        const result = await action<{ binding: Binding }>(
          host,
          "binding.get",
          {},
        );
        if (generation.current !== activeGeneration) return;
        setBinding(result.binding);
        setBindingConnection(result.binding.connection_ref ?? "");
        setBindingIds(result.binding.expert_allowed_kb_ids);
        setDefaultIds(result.binding.default_kb_ids);
        setWebAllowed(result.binding.web_allowed);
        setBindingEnabled(result.binding.enabled);
      }
      setLoadState("ready");
      setError("");
    } catch (caught) {
      if (generation.current !== activeGeneration) return;
      const classified = classifyTransportError(caught);
      setLoadState(classified.status === 403 ? "denied" : "error");
      setError(errorCopy(caught, locale));
    }
  };

  React.useEffect(() => {
    const activeGeneration = ++generation.current;
    setLoadState("loading");
    setProfile(null);
    setSettings(null);
    setBinding(null);
    setConnectionId(null);
    setDraft(emptyDraft());
    setCatalog([]);
    setCatalogLoadedFor("");
    setProbeIds([]);
    setProbeState({});
    setGrantKey("new");
    setGrantUserId("");
    setGrantIds([]);
    setBindingCatalog([]);
    setBindingCatalogFor("");
    setBindingIds([]);
    setDefaultIds([]);
    setBindingConnection("");
    setWebAllowed(false);
    setBindingEnabled(false);
    setConflict(false);
    setNotice("");
    setError("");
    void host
      .request<{ role?: string; permissions?: string[] }>("/auth/me")
      .then((user) => {
        if (generation.current !== activeGeneration) return;
        setProfile(user);
        return loadSettings(user, activeGeneration);
      })
      .catch((caught: unknown) => {
        if (generation.current !== activeGeneration) return;
        const classified = classifyTransportError(caught);
        setLoadState(classified.status === 403 ? "denied" : "error");
        setError(errorCopy(caught, locale));
      });
    return () => {
      generation.current += 1;
    };
  }, [host, key]);

  React.useEffect(() => {
    if (!selectedConnection) {
      setDraft(emptyDraft());
      setCatalog([]);
      setCatalogLoadedFor("");
      setProbeIds([]);
      setProbeState({});
      setProbeQuery("");
      setProbeDocument("");
      setGrantKey("new");
      setGrantUserId("");
      setGrantIds([]);
      return;
    }
    setDraft({
      name: selectedConnection.name,
      base_url: selectedConnection.base_url,
      tenant_id: selectedConnection.tenant_id ?? "",
      api_key: "",
      allowed_kb_ids: [...selectedConnection.allowed_kb_ids],
      enabled: selectedConnection.enabled,
    });
    setCatalog([]);
    setCatalogLoadedFor("");
    setProbeIds([]);
    setProbeState({});
    setProbeQuery("");
    setProbeDocument("");
    setGrantKey("new");
    setGrantUserId("");
    setGrantIds([]);
  }, [connectionId, selectedConnection?.revision, connectionRefreshVersion]);

  React.useEffect(() => {
    if (
      location !== "expert" ||
      profile?.role === "admin" ||
      !bindingConnection
    )
      return;
    const candidate = settings?.connections.find(
      (item) => item.connection_id === bindingConnection,
    );
    if (!candidate) return;
    const items = candidate.allowed_kb_ids.map((kb_id) => ({
      kb_id,
      name: kb_id,
    }));
    setBindingCatalog(items);
    setBindingCatalogFor(`${bindingConnection}:${candidate.revision}`);
  }, [bindingConnection, location, profile, settings]);

  React.useEffect(() => {
    setGrantKey("new");
    setGrantUserId("");
    setGrantIds([]);
  }, [connectionId]);

  const selectedConnectionRef = React.useRef<{
    id: string | null;
    revision: number;
  }>({ id: null, revision: 0 });
  selectedConnectionRef.current = {
    id: connectionId,
    revision: selectedConnection?.revision ?? 0,
  };
  const isAdmin = profile?.role === "admin";
  const canManageConnections = Boolean(
    settings?.capabilities.can_manage_connections && isAdmin,
  );
  const canManageGrants = Boolean(
    settings?.capabilities.can_manage_grants && isAdmin,
  );
  const canManageBinding =
    location === "expert" &&
    Boolean(settings?.capabilities.can_manage_bindings && context.agentId);

  const fetchCatalog = async (
    id: string,
    revision: number,
    target: "connection" | "binding",
  ) => {
    if (!id) return;
    const requestKey = `${key}:${target}:${id}:${revision}`;
    if (target === "connection") setBusy("catalog");
    try {
      const result = await action<{
        connection_id: string;
        observed_revision: number;
        knowledge_bases: CatalogItem[];
      }>(host, "connection.catalog", {
        connection_id: id,
        expected_revision: revision,
      });
      if (
        generation.current !== currentGeneration ||
        result.observed_revision !== revision ||
        result.connection_id !== id
      )
        return;
      const items = result.knowledge_bases.map((item) => ({
        kb_id: String(item.kb_id),
        name: String(item.name),
      }));
      if (
        target === "connection" &&
        selectedConnectionRef.current.id === id &&
        selectedConnectionRef.current.revision === revision &&
        requestKey === `${key}:connection:${id}:${revision}`
      ) {
        setCatalog(items);
        setCatalogLoadedFor(`${id}:${revision}`);
      }
      if (
        target === "binding" &&
        bindingConnection === id &&
        settings?.connections.find((item) => item.connection_id === id)
          ?.revision === revision
      ) {
        setBindingCatalog(items);
        setBindingCatalogFor(`${id}:${revision}`);
      }
    } catch (caught) {
      if (generation.current === currentGeneration)
        setError(errorCopy(caught, locale));
    } finally {
      if (target === "connection" && generation.current === currentGeneration)
        setBusy("");
    }
  };
  const currentGeneration = generation.current;

  const refresh = async () => {
    if (!profile) return;
    revisionGeneration.current += 1;
    setConnectionRefreshVersion((current) => current + 1);
    setGrantKey("new");
    setGrantUserId("");
    setGrantIds([]);
    setConflict(false);
    setError("");
    setLoadState("loading");
    await loadSettings(profile, generation.current);
  };

  const saveConnection = async (event: { preventDefault(): void }) => {
    event.preventDefault();
    if (
      !draft.name.trim() ||
      !draft.base_url.trim() ||
      (!selectedConnection && !draft.api_key)
    ) {
      setError(t(locale, "invalidFields"));
      document
        .getElementById(
          !draft.name.trim()
            ? "connection-name"
            : !draft.base_url.trim()
            ? "connection-address"
            : "connection-key",
        )
        ?.focus();
      return;
    }
    if (conflict) return;
    setBusy("connection-save");
    setError("");
    setNotice("");
    revisionGeneration.current += 1;
    const expectedRevision = selectedConnection?.revision ?? 0;
    try {
      const payload: Record<string, unknown> = {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        ...(selectedConnection
          ? { connection_id: selectedConnection.connection_id }
          : {}),
        expected_revision: expectedRevision,
        name: draft.name,
        base_url: draft.base_url,
        tenant_id: draft.tenant_id || null,
        allowed_kb_ids: draft.allowed_kb_ids,
        enabled: draft.enabled,
        ...(draft.api_key ? { api_key: draft.api_key } : {}),
      };
      const result = await action<{ connection: Connection }>(
        host,
        "connection.save",
        payload,
      );
      if (generation.current !== currentGeneration) return;
      setSettings((current) =>
        current
          ? {
              ...current,
              connections: [
                ...current.connections.filter(
                  (item) =>
                    item.connection_id !== result.connection.connection_id,
                ),
                result.connection,
              ],
            }
          : current,
      );
      setConnectionId(result.connection.connection_id);
      setDraft((current) => ({ ...current, api_key: "" }));
      setCatalog([]);
      setCatalogLoadedFor("");
      setProbeState({});
      setNotice(t(locale, "saved"));
    } catch (caught) {
      if (generation.current !== currentGeneration) return;
      const classified = classifyTransportError(caught);
      if (classified.status === 409 || classified.code === "CONFIG_CONFLICT")
        setConflict(true);
      setError(errorCopy(caught, locale));
    } finally {
      if (generation.current === currentGeneration) setBusy("");
    }
  };

  const runProbe = async (component: "LIST" | "SEARCH" | "READ") => {
    if (!selectedConnection || !connectionId) return;
    if (
      component === "SEARCH" &&
      (!probeQuery.trim() || probeIds.length === 0)
    ) {
      setError(t(locale, "blockedSearch"));
      return;
    }
    if (
      component === "READ" &&
      (!probeDocument.trim() || probeIds.length === 0)
    ) {
      setError(t(locale, "blockedRead"));
      return;
    }
    const id = connectionId;
    const revision = selectedConnection.revision;
    const generationKey = currentGeneration;
    const revisionKey = revisionGeneration.current;
    setBusy(`probe-${component}`);
    setError("");
    const payload: Record<string, unknown> = {
      connection_id: id,
      expected_revision: revision,
      component,
    };
    if (component !== "LIST") payload.knowledge_base_ids = probeIds;
    if (component === "SEARCH") payload.query = probeQuery;
    if (component === "READ") payload.knowledge_id = probeDocument;
    try {
      const result = await action<{
        connection_id: string;
        observed_revision: number;
        component: string;
        status: string;
      }>(host, "connection.probe", payload);
      if (
        generation.current !== generationKey ||
        revisionGeneration.current !== revisionKey ||
        selectedConnectionRef.current.id !== id ||
        selectedConnectionRef.current.revision !== revision ||
        result.connection_id !== id ||
        result.observed_revision !== revision ||
        result.component !== component
      )
        return;
      setProbeState((current) => ({
        ...current,
        [component]: { status: result.status, revision },
      }));
      const refreshed = await action<SettingsData>(host, "settings.get", {});
      if (
        generation.current !== generationKey ||
        revisionGeneration.current !== revisionKey ||
        selectedConnectionRef.current.id !== id ||
        selectedConnectionRef.current.revision !== revision
      )
        return;
      setSettings(refreshed);
    } catch (caught) {
      if (generation.current === generationKey)
        setError(errorCopy(caught, locale));
    } finally {
      if (
        generation.current === generationKey &&
        revisionGeneration.current === revisionKey
      )
        setBusy("");
    }
  };

  const selectedGrant = settings?.grants.find(
    (item) =>
      item.connection_id === connectionId &&
      `${item.connection_id}:${item.user_id}` === grantKey,
  );
  const changeGrantUserId = (value: string) => {
    setGrantUserId(value);
    const userId = Number(value);
    const grant = settings?.grants.find(
      (item) => item.connection_id === connectionId && item.user_id === userId,
    );
    if (!grant) {
      setGrantKey("new");
      setGrantIds([]);
      return;
    }
    setGrantKey(`${grant.connection_id}:${grant.user_id}`);
    setGrantIds([...grant.user_allowed_kb_ids]);
  };
  const saveGrant = async (event: { preventDefault(): void }) => {
    event.preventDefault();
    const userId = Number(grantUserId);
    if (!Number.isInteger(userId) || userId <= 0 || !connectionId) {
      setError(t(locale, "invalidId"));
      document.getElementById("grant-user-id")?.focus();
      return;
    }
    if (conflict) return;
    setBusy("grant-save");
    setError("");
    try {
      const result = await action<{ grant: Grant }>(host, "grant.save", {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        connection_id: connectionId,
        expected_revision: selectedGrant?.revision ?? 0,
        user_id: userId,
        user_allowed_kb_ids: grantIds,
      });
      if (generation.current !== currentGeneration) return;
      setSettings((current) =>
        current
          ? {
              ...current,
              grants: [
                ...current.grants.filter(
                  (grant) =>
                    !(
                      grant.connection_id === connectionId &&
                      grant.user_id === userId
                    ),
                ),
                result.grant,
              ],
            }
          : current,
      );
      setGrantKey(`${connectionId}:${userId}`);
      setGrantUserId(String(userId));
      setGrantIds([...result.grant.user_allowed_kb_ids]);
      setNotice(t(locale, "saved"));
    } catch (caught) {
      if (generation.current === currentGeneration) {
        const classified = classifyTransportError(caught);
        if (classified.status === 409) setConflict(true);
        setError(errorCopy(caught, locale));
      }
    } finally {
      if (generation.current === currentGeneration) setBusy("");
    }
  };

  const saveBinding = async (event: { preventDefault(): void }) => {
    event.preventDefault();
    if (!binding || !context.agentId) return;
    if (
      defaultIds.some((id) => !bindingIds.includes(id)) ||
      (bindingEnabled && (!bindingConnection || bindingIds.length === 0))
    ) {
      setError(t(locale, "blockedBinding"));
      return;
    }
    if (conflict) return;
    setBusy("binding-save");
    setError("");
    try {
      const result = await action<{ binding: Binding }>(host, "binding.save", {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        expected_revision: binding.revision,
        connection_ref: bindingConnection || null,
        expert_allowed_kb_ids: bindingIds,
        default_kb_ids: defaultIds,
        web_allowed: webAllowed,
        enabled: bindingEnabled,
      });
      if (generation.current !== currentGeneration) return;
      setBinding(result.binding);
      setBindingIds([...result.binding.expert_allowed_kb_ids]);
      setDefaultIds([...result.binding.default_kb_ids]);
      setNotice(t(locale, "saved"));
    } catch (caught) {
      if (generation.current === currentGeneration) {
        const classified = classifyTransportError(caught);
        if (classified.status === 409) setConflict(true);
        setError(errorCopy(caught, locale));
      }
    } finally {
      if (generation.current === currentGeneration) setBusy("");
    }
  };

  const text = (keyName: MessageKey) => t(locale, keyName);
  if (loadState === "loading")
    return (
      <section aria-busy="true" className="ek-settings">
        <h2>{text("title")}</h2>
        <p role="status">{text("loading")}</p>
      </section>
    );
  if (loadState === "unavailable")
    return (
      <section className="ek-settings">
        <h2>{text("title")}</h2>
        <p role="status">{text("unavailable")}</p>
      </section>
    );
  if (loadState === "target")
    return (
      <section className="ek-settings">
        <h2>{text("title")}</h2>
        <p role="status">{text("target")}</p>
      </section>
    );
  if (loadState === "denied")
    return (
      <section className="ek-settings">
        <h2>{text("title")}</h2>
        <p role="alert">{error || text("denied")}</p>
        <button type="button" onClick={() => void refresh()}>
          {text("retry")}
        </button>
      </section>
    );
  if (loadState === "error")
    return (
      <section className="ek-settings">
        <h2>{text("title")}</h2>
        <p role="alert">{error || text("error")}</p>
        <button type="button" onClick={() => void refresh()}>
          {text("retry")}
        </button>
      </section>
    );
  if (!settings) return null;

  const connectionCatalogRevision = selectedConnection?.revision ?? 0;
  const bindingCatalogRevision =
    settings.connections.find(
      (item) => item.connection_id === bindingConnection,
    )?.revision ?? 0;
  const bindingConnectionInfo = settings.connections.find(
    (item) => item.connection_id === bindingConnection,
  );
  const probeReadyFor =
    catalogLoadedFor === `${connectionId}:${connectionCatalogRevision}`;
  const bindingCatalogReady =
    bindingCatalogFor === `${bindingConnection}:${bindingCatalogRevision}`;
  const grantCatalogLoaded = probeReadyFor;

  return (
    <section className="ek-settings" aria-labelledby="ek-title">
      <style>{`.ek-settings{color:var(--fn-text-primary,inherit);font:inherit}.ek-settings form,.ek-settings .ek-section{display:grid;gap:.8rem;margin:1rem 0;padding:1rem;border:1px solid var(--fn-border-primary,rgba(127,127,127,.35));border-radius:.65rem}.ek-settings label{display:grid;gap:.3rem}.ek-settings input:not([type=checkbox]),.ek-settings select{max-width:36rem;padding:.45rem;border:1px solid var(--fn-border-primary,rgba(127,127,127,.45));border-radius:.35rem;background:var(--fn-bg-primary,transparent);color:inherit}.ek-settings button{width:max-content;padding:.4rem .7rem}.ek-settings fieldset{display:grid;gap:.45rem;border:1px solid var(--fn-border-primary,rgba(127,127,127,.35));border-radius:.4rem}.ek-settings .ek-choice{display:flex;align-items:center;gap:.45rem}.ek-settings .ek-choice input{margin:0}.ek-settings small,.ek-settings .ek-muted{color:var(--fn-text-secondary,#777)}.ek-settings [role=alert]{color:var(--fn-color-error,#b42318)}.ek-settings [role=status]{color:var(--fn-text-secondary,#777)}`}</style>
      <h2 id="ek-title">{text("title")}</h2>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {conflict && (
        <button type="button" onClick={() => void refresh()}>
          {text("reload")}
        </button>
      )}

      {location === "connectors" && canManageConnections && (
        <>
          <SearchSettings host={host} context={context} />
          <div className="ek-section">
            <h3>{text("connection")}</h3>
            <label>
              {text("connection")}
              <select
                aria-label={text("connection")}
                value={connectionId ?? "new"}
                onChange={(event: { currentTarget: HTMLSelectElement }) => {
                  const value = event.currentTarget.value;
                  setConnectionId(value === "new" ? null : value);
                  setGrantKey("new");
                  setGrantUserId("");
                  setGrantIds([]);
                  setError("");
                  setConflict(false);
                }}
              >
                <option value="new">{text("newConnection")}</option>
                {settings.connections.map((item) => (
                  <option key={item.connection_id} value={item.connection_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            {selectedConnection && (
              <p role="status">
                {text("status")}: {statusCopy(selectedConnection, locale)} ·{" "}
                {text("revision")}: {selectedConnection.revision}
                {selectedConnection.credential_configured && (
                  <span> · {text("credentialSet")}</span>
                )}
              </p>
            )}
            {settings.connections.length === 0 && (
              <p>{text("noConnections")}</p>
            )}
            <form onSubmit={saveConnection}>
              <label>
                {text("name")}
                <input
                  id="connection-name"
                  required
                  value={draft.name}
                  onChange={(event: { currentTarget: HTMLInputElement }) =>
                    setDraft({ ...draft, name: event.currentTarget.value })
                  }
                />
              </label>
              <label>
                {text("address")}
                <input
                  id="connection-address"
                  required
                  type="url"
                  value={draft.base_url}
                  onChange={(event: { currentTarget: HTMLInputElement }) =>
                    setDraft({ ...draft, base_url: event.currentTarget.value })
                  }
                />
              </label>
              <label>
                {text("tenant")}
                <input
                  value={draft.tenant_id}
                  onChange={(event: { currentTarget: HTMLInputElement }) =>
                    setDraft({ ...draft, tenant_id: event.currentTarget.value })
                  }
                />
              </label>
              <label>
                {text("key")}
                <input
                  id="connection-key"
                  type="password"
                  autoComplete="new-password"
                  value={draft.api_key}
                  onChange={(event: { currentTarget: HTMLInputElement }) =>
                    setDraft({ ...draft, api_key: event.currentTarget.value })
                  }
                />
              </label>
              <label className="ek-choice">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={(event: { currentTarget: HTMLInputElement }) =>
                    setDraft({ ...draft, enabled: event.currentTarget.checked })
                  }
                />{" "}
                {text("enabled")}
              </label>
              {selectedConnection && (
                <>
                  <button
                    type="button"
                    disabled={busy === "catalog"}
                    onClick={() =>
                      void fetchCatalog(
                        selectedConnection.connection_id,
                        selectedConnection.revision,
                        "connection",
                      )
                    }
                  >
                    {text("catalog")}
                  </button>
                  <Checks
                    items={catalog}
                    selected={draft.allowed_kb_ids}
                    onToggle={(id) =>
                      setDraft((current) => ({
                        ...current,
                        allowed_kb_ids: arrayToggle(current.allowed_kb_ids, id),
                      }))
                    }
                    label={text("allowed")}
                    locale={locale}
                    removable={draft.allowed_kb_ids}
                  />
                  {draft.allowed_kb_ids.length === 0 && (
                    <p role="status">{text("emptyAllowed")}</p>
                  )}
                </>
              )}
              <button
                type="submit"
                disabled={busy === "connection-save" || conflict}
              >
                {busy === "connection-save" ? text("saving") : text("save")}
              </button>
            </form>
            {selectedConnection && (
              <div className="ek-section" aria-label={text("probe")}>
                <h4>
                  {text("probe")} · {text("revision")}{" "}
                  {selectedConnection.revision}
                </h4>
                {selectedConnection.allowed_kb_ids.length > 0 && (
                  <Checks
                    items={catalog.filter((item) =>
                      selectedConnection.allowed_kb_ids.includes(item.kb_id),
                    )}
                    selected={probeIds}
                    onToggle={(id) =>
                      setProbeIds((current) => arrayToggle(current, id))
                    }
                    label={text("probeIds")}
                    locale={locale}
                  />
                )}
                <label>
                  {text("query")}
                  <input
                    value={probeQuery}
                    onChange={(event: { currentTarget: HTMLInputElement }) =>
                      setProbeQuery(event.currentTarget.value)
                    }
                  />
                </label>
                <label>
                  {text("document")}
                  <input
                    value={probeDocument}
                    onChange={(event: { currentTarget: HTMLInputElement }) =>
                      setProbeDocument(event.currentTarget.value)
                    }
                  />
                </label>
                {!probeReadyFor && (
                  <p className="ek-muted" role="status">
                    {text("blocked")}: {text("catalog")} ({text("search")}/
                    {text("read")})
                  </p>
                )}
                {probeReadyFor &&
                  (probeIds.length === 0 || !probeQuery.trim()) && (
                    <p className="ek-muted" role="status">
                      {text("blocked")}: {text("blockedSearch")}
                    </p>
                  )}
                {probeReadyFor &&
                  (probeIds.length === 0 || !probeDocument.trim()) && (
                    <p className="ek-muted" role="status">
                      {text("blocked")}: {text("blockedRead")}
                    </p>
                  )}
                <div>
                  {(["LIST", "SEARCH", "READ"] as const).map((component) => (
                    <button
                      key={component}
                      type="button"
                      disabled={
                        Boolean(busy) ||
                        (component !== "LIST" && !probeReadyFor)
                      }
                      onClick={() => void runProbe(component)}
                    >
                      {text(component.toLowerCase() as MessageKey)}
                      {probeState[component] &&
                        ` · ${probeState[component].status} @${probeState[component].revision}`}
                    </button>
                  ))}
                </div>
                {(["LIST", "SEARCH", "READ"] as const).map((component) => {
                  const summary =
                    selectedConnection.probe_components[component];
                  if (
                    !summary ||
                    summary.observed_revision !== selectedConnection.revision
                  )
                    return null;
                  const checkedAt = summary?.checked_at
                    ? formatProbeTime(
                        summary.checked_at,
                        locale,
                        context.timeZone,
                      )
                    : null;
                  return (
                    <p key={component} className="ek-muted">
                      {text(component.toLowerCase() as MessageKey)}:{" "}
                      {summary.status}
                      {checkedAt && (
                        <>
                          {" "}
                          · {text("checkedAt")}{" "}
                          <time dateTime={summary.checked_at}>{checkedAt}</time>
                        </>
                      )}
                    </p>
                  );
                })}
              </div>
            )}
          </div>
          {canManageGrants && (
            <div className="ek-section">
              <h3>{text("saveGrant")}</h3>
              <label>
                {text("employee")}
                <select
                  aria-label={text("employee")}
                  value={grantKey}
                  onChange={(event: { currentTarget: HTMLSelectElement }) => {
                    const value = event.currentTarget.value;
                    setGrantKey(value);
                    const grant = settings.grants.find(
                      (item) =>
                        `${item.connection_id}:${item.user_id}` === value,
                    );
                    setGrantUserId(grant ? String(grant.user_id) : "");
                    setGrantIds(grant ? [...grant.user_allowed_kb_ids] : []);
                  }}
                >
                  <option value="new">{text("employee")}</option>
                  {settings.grants
                    .filter((item) => item.connection_id === connectionId)
                    .map((item) => (
                      <option
                        key={`${item.connection_id}:${item.user_id}`}
                        value={`${item.connection_id}:${item.user_id}`}
                      >
                        {item.user_id} · r{item.revision}
                      </option>
                    ))}
                </select>
              </label>
              <form onSubmit={saveGrant}>
                <label>
                  {text("employee")}
                  <input
                    id="grant-user-id"
                    type="number"
                    min="1"
                    step="1"
                    required
                    value={grantUserId}
                    onChange={(event: { currentTarget: HTMLInputElement }) =>
                      changeGrantUserId(event.currentTarget.value)
                    }
                  />
                </label>
                {selectedConnection && (
                  <Checks
                    items={catalog.filter((item) =>
                      selectedConnection.allowed_kb_ids.includes(item.kb_id),
                    )}
                    selected={grantIds}
                    onToggle={(id) =>
                      setGrantIds((current) => arrayToggle(current, id))
                    }
                    label={text("grantAllowed")}
                    locale={locale}
                    removable={grantIds}
                  />
                )}
                <button
                  type="submit"
                  disabled={
                    busy === "grant-save" ||
                    conflict ||
                    !connectionId ||
                    !grantCatalogLoaded
                  }
                >
                  {text("saveGrant")}
                </button>
              </form>
              {grantIds.length === 0 && (
                <p role="status">{text("emptyAllowed")}</p>
              )}
            </div>
          )}
        </>
      )}

      {location === "expert" && canManageBinding && binding && (
        <div className="ek-section">
          <h3>
            {text("binding")} · {text("revision")}: {binding.revision}
          </h3>
          <form onSubmit={saveBinding}>
            <label>
              {text("connectionRef")}
              <select
                aria-label={text("connectionRef")}
                value={bindingConnection}
                onChange={(event: { currentTarget: HTMLSelectElement }) => {
                  setBindingConnection(event.currentTarget.value);
                  setBindingIds([]);
                  setDefaultIds([]);
                  setBindingCatalog([]);
                  setBindingCatalogFor("");
                }}
              >
                <option value="">{text("noConnections")}</option>
                {settings.connections.map((item) => (
                  <option key={item.connection_id} value={item.connection_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            {bindingConnection && isAdmin && (
              <button
                type="button"
                onClick={() =>
                  void fetchCatalog(
                    bindingConnection,
                    bindingCatalogRevision,
                    "binding",
                  )
                }
              >
                {text("catalog")}
              </button>
            )}
            {bindingConnection && (
              <>
                <Checks
                  items={bindingCatalog.filter(
                    (item) =>
                      bindingConnectionInfo?.allowed_kb_ids.includes(
                        item.kb_id,
                      ),
                  )}
                  selected={bindingIds}
                  onToggle={(id) => {
                    const next = arrayToggle(bindingIds, id);
                    setBindingIds(next);
                    setDefaultIds((current) =>
                      current.filter((value) => next.includes(value)),
                    );
                  }}
                  label={text("expertAllowed")}
                  locale={locale}
                  removable={bindingIds}
                />
                <Checks
                  items={bindingCatalog.filter(
                    (item) =>
                      bindingConnectionInfo?.allowed_kb_ids.includes(
                        item.kb_id,
                      ) && bindingIds.includes(item.kb_id),
                  )}
                  selected={defaultIds}
                  onToggle={(id) =>
                    setDefaultIds((current) => arrayToggle(current, id))
                  }
                  label={text("defaults")}
                  locale={locale}
                  removable={defaultIds}
                />
              </>
            )}
            <label className="ek-choice">
              <input
                type="checkbox"
                checked={webAllowed}
                onChange={(event: { currentTarget: HTMLInputElement }) =>
                  setWebAllowed(event.currentTarget.checked)
                }
              />{" "}
              {text("web")}
            </label>
            <label className="ek-choice">
              <input
                type="checkbox"
                checked={bindingEnabled}
                disabled={
                  !bindingEnabled &&
                  (!bindingConnection || bindingIds.length === 0)
                }
                onChange={(event: { currentTarget: HTMLInputElement }) =>
                  setBindingEnabled(event.currentTarget.checked)
                }
              />{" "}
              {text("bindingEnabled")}
            </label>
            {bindingEnabled &&
              (!bindingConnection || bindingIds.length === 0) && (
                <p role="status">{text("blockedBinding")}</p>
              )}
            {bindingIds.length === 0 && (
              <p role="status">{text("emptyAllowed")}</p>
            )}
            <button
              type="submit"
              disabled={busy === "binding-save" || conflict}
            >
              {busy === "binding-save" ? text("saving") : text("saveBinding")}
            </button>
          </form>
          {bindingConnection && !bindingCatalogReady && (
            <p className="ek-muted">{text("catalog")}</p>
          )}
        </div>
      )}
    </section>
  );
}
