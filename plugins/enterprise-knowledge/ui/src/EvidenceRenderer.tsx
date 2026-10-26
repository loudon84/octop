import { useEffect, useRef, useState } from "./host";
import type { PluginToolContext, PluginToolProps } from "./host";
import { classifyTransportError, toolAction, ToolActionError } from "./api";
import { t } from "./i18n";

const schemaId = "octop.enterprise.knowledge.v1";
const uuid4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
interface Evidence {
  evidence_id: string;
  operation_id: string;
  source_kind: "KNOWLEDGE" | "WEB";
  provider: "WEKNORA" | "SEARXNG" | "FIRECRAWL";
  degraded: boolean;
  search_revision: number;
  connection_id: string | null;
  kb_id: string | null;
  knowledge_id: string | null;
  title: string;
  content: string;
  truncated: boolean;
  retrieved_at: string;
  source_page: number | null;
  locator: string | null;
}
interface Operation {
  operation_id: string;
  user_id: number;
  agent_id: string;
  thread_id: string;
  status:
    | "VALIDATING"
    | "RETRIEVING"
    | "GENERATING"
    | "COMPLETED"
    | "EMPTY"
    | "DENIED"
    | "FAILED"
    | "CANCELLED"
    | "INTERRUPTED";
}
type EvidenceList =
  | { kind: "pending" }
  | { kind: "stopped" }
  | { kind: "complete" | "empty"; rows: Evidence[] };
type ErrorKind = "access" | "source" | "cursor" | "other";

function object(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
function validOperation(
  value: unknown,
  operationId: string,
  target: PluginToolContext,
): value is Operation {
  return (
    object(value) &&
    value.schema_id === schemaId &&
    value.version === 1 &&
    value.operation_id === operationId &&
    uuid4.test(String(value.operation_id)) &&
    Number.isInteger(value.revision) &&
    Number(value.revision) >= 1 &&
    Number.isInteger(value.user_id) &&
    Number(value.user_id) > 0 &&
    value.agent_id === target.agentId &&
    value.thread_id === target.threadId &&
    Number.isInteger(value.session_revision) &&
    Number(value.session_revision) >= 0 &&
    Number.isInteger(value.config_revision) &&
    Number(value.config_revision) >= 0 &&
    Number.isInteger(value.binding_revision) &&
    Number(value.binding_revision) >= 0 &&
    Number.isInteger(value.grant_revision) &&
    Number(value.grant_revision) >= 0 &&
    (value.connection_id === null ||
      (typeof value.connection_id === "string" &&
        uuid4.test(value.connection_id))) &&
    (value.status === "VALIDATING" ||
      value.status === "RETRIEVING" ||
      value.status === "GENERATING" ||
      value.status === "COMPLETED" ||
      value.status === "EMPTY" ||
      value.status === "DENIED" ||
      value.status === "FAILED" ||
      value.status === "CANCELLED" ||
      value.status === "INTERRUPTED")
  );
}
function validUtc(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /(?:Z|\+00:00)$/i.test(value) &&
    Number.isFinite(new Date(value).getTime())
  );
}
function validWebLocator(value: unknown): value is string {
  if (typeof value !== "string" || value !== value.trim() || /\s/.test(value))
    return false;
  try {
    const parsed = new URL(value);
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      Boolean(parsed.hostname) &&
      !parsed.username &&
      !parsed.password
    );
  } catch {
    return false;
  }
}
function parseEvidence(
  value: unknown,
  operationId: string,
  target: PluginToolContext,
  userId: number,
): Evidence | null {
  if (
    !object(value) ||
    value.schema_id !== schemaId ||
    value.version !== 1 ||
    !Object.hasOwn(value, "connection_id") ||
    !Object.hasOwn(value, "kb_id") ||
    !Object.hasOwn(value, "knowledge_id") ||
    !Object.hasOwn(value, "source_page") ||
    !Object.hasOwn(value, "source_version") ||
    !Object.hasOwn(value, "locator") ||
    value.operation_id !== operationId ||
    !uuid4.test(String(value.evidence_id)) ||
    value.agent_id !== target.agentId ||
    value.thread_id !== target.threadId ||
    value.user_id !== userId ||
    typeof value.title !== "string" ||
    typeof value.content !== "string" ||
    typeof value.truncated !== "boolean" ||
    typeof value.degraded !== "boolean" ||
    !Number.isInteger(value.search_revision) ||
    Number(value.search_revision) < 0 ||
    !validUtc(value.retrieved_at) ||
    !(
      value.source_page === null ||
      (Number.isInteger(value.source_page) && Number(value.source_page) >= 0)
    ) ||
    !(
      value.source_version === null || typeof value.source_version === "string"
    ) ||
    !(value.locator === null || typeof value.locator === "string")
  )
    return null;
  if (value.source_kind === "KNOWLEDGE") {
    if (
      value.provider !== "WEKNORA" ||
      value.degraded ||
      value.search_revision !== 0 ||
      typeof value.connection_id !== "string" ||
      !uuid4.test(value.connection_id) ||
      typeof value.kb_id !== "string" ||
      !value.kb_id ||
      typeof value.knowledge_id !== "string" ||
      !value.knowledge_id ||
      value.locator !== null
    )
      return null;
  } else if (value.source_kind === "WEB") {
    if (
      (value.provider !== "SEARXNG" && value.provider !== "FIRECRAWL") ||
      Number(value.search_revision) < 1 ||
      value.connection_id !== null ||
      value.kb_id !== null ||
      value.knowledge_id !== null ||
      !validWebLocator(value.locator)
    )
      return null;
  } else return null;
  return {
    evidence_id: value.evidence_id as string,
    operation_id: operationId,
    source_kind: value.source_kind,
    provider: value.provider,
    degraded: value.degraded,
    search_revision: value.search_revision as number,
    connection_id: value.connection_id as string | null,
    kb_id: value.kb_id as string | null,
    knowledge_id: value.knowledge_id as string | null,
    title: value.title,
    content: value.content,
    truncated: value.truncated,
    retrieved_at: value.retrieved_at,
    source_page: value.source_page as number | null,
    locator: value.locator as string | null,
  };
}
function parseEvidenceList(
  value: unknown,
  operationId: string,
  target: PluginToolContext,
): EvidenceList | null {
  if (
    !object(value) ||
    value.schema_id !== schemaId ||
    value.version !== 1 ||
    !Array.isArray(value.evidence)
  )
    return null;
  const operation = value.operation;
  if (!validOperation(operation, operationId, target)) return null;
  if (
    operation.status === "VALIDATING" ||
    operation.status === "RETRIEVING" ||
    operation.status === "GENERATING"
  )
    return { kind: "pending" };
  if (
    operation.status === "DENIED" ||
    operation.status === "FAILED" ||
    operation.status === "CANCELLED" ||
    operation.status === "INTERRUPTED"
  )
    return { kind: "stopped" };
  const rows = value.evidence.map((item) =>
    parseEvidence(item, operationId, target, operation.user_id),
  );
  if (rows.some((item) => item === null)) return null;
  const parsed = rows as Evidence[];
  if (new Set(parsed.map((row) => row.evidence_id)).size !== parsed.length)
    return null;
  return {
    kind: operation.status === "EMPTY" ? "empty" : "complete",
    rows: parsed,
  };
}
function operationReference(value: unknown): string | null {
  return object(value) &&
    value.schema_id === schemaId &&
    value.version === 1 &&
    uuid4.test(String(value.operation_id))
    ? String(value.operation_id)
    : null;
}
function stateEvidenceId(state: unknown, operationId: string): string | null {
  return object(state) &&
    state.operation_id === operationId &&
    typeof state.evidence_id === "string" &&
    uuid4.test(state.evidence_id)
    ? state.evidence_id
    : null;
}
function errorKind(error: unknown): ErrorKind {
  const { status, code } =
    error instanceof ToolActionError ? error : classifyTransportError(error);
  if (
    status === 401 ||
    status === 403 ||
    code === "ACCESS_DENIED" ||
    code === "SCOPE_INVALID"
  )
    return "access";
  if (code === "READ_SOURCE_CHANGED") return "source";
  if (code === "READ_CURSOR_INVALID") return "cursor";
  return "other";
}
function validTimezone(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    new Intl.DateTimeFormat("en", { timeZone: value });
    return value;
  } catch {
    return null;
  }
}
function currentTarget(
  host: PluginToolProps["host"],
): PluginToolContext | null {
  try {
    const target = host.getToolContext();
    return target?.agentId && target.threadId ? target : null;
  } catch {
    return null;
  }
}

export function EvidenceRenderer(props: PluginToolProps) {
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [autoRefreshStopped, setAutoRefreshStopped] = useState(false);
  const [listError, setListError] = useState<ErrorKind | null>(null);
  const [timezone, setTimezone] = useState<string | null>(null);
  const [timezoneReady, setTimezoneReady] = useState(false);
  const [selected, setSelected] = useState<Evidence | null>(null);
  const sequence = useRef(0);
  const denied = useRef(false);
  const mountController = useRef<AbortController | null>(null);
  const listController = useRef<AbortController | null>(null);
  const manualRefresh = useRef<(() => void) | null>(null);
  const operationId = operationReference(props.data);
  const target = currentTarget(props.host);
  const active =
    props.pluginId === "enterprise-knowledge" &&
    props.toolName === "enterprise_knowledge_search" &&
    props.status === "done" &&
    Boolean(props.callId) &&
    Boolean(operationId) &&
    Boolean(target) &&
    Boolean(props.presentation?.active);
  const locale = target?.locale || "en";
  const sidePanel = props.presentation?.location === "side-panel";
  const selectedFromState = operationId
    ? stateEvidenceId(props.presentation?.state, operationId)
    : null;

  async function fetchList(signal?: AbortSignal): Promise<EvidenceList> {
    if (!target || !operationId)
      throw new Error("Evidence context unavailable");
    const result = await toolAction<unknown>(
      props.host,
      target,
      "evidence.list",
      { schema_id: schemaId, version: 1, operation_id: operationId },
      signal,
    );
    const list = parseEvidenceList(result, operationId, target);
    if (!list) throw new Error("Invalid evidence list");
    return list;
  }
  async function reload(signal?: AbortSignal): Promise<Evidence[]> {
    const list = await fetchList(signal);
    if (list.kind !== "complete" && list.kind !== "empty")
      throw new Error("Evidence operation is not complete");
    return list.rows;
  }
  useEffect(() => {
    if (denied.current) return;
    const id = ++sequence.current;
    let zoneController: AbortController | null = new AbortController();
    mountController.current = zoneController;
    let requestController: AbortController | null = null;
    const listControllers: AbortController[] = [];
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let deadlineTimer: ReturnType<typeof setTimeout> | null = null;
    let deadline: number | null = null;
    let delay = 250;
    let pendingOperation = false;
    let automaticStopped = false;
    let started = false;
    let hidden = document.visibilityState === "hidden";
    const isCurrent = () =>
      sequence.current === id &&
      active &&
      currentTarget(props.host)?.agentId === target?.agentId &&
      currentTarget(props.host)?.threadId === target?.threadId;
    const clearRetry = () => {
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
    };
    const stopAutomatic = () => {
      automaticStopped = true;
      clearRetry();
      if (deadlineTimer !== null) clearTimeout(deadlineTimer);
      deadlineTimer = null;
      requestController?.abort();
      if (pendingOperation && isCurrent()) setAutoRefreshStopped(true);
    };
    const startDeadlineTimer = () => {
      if (deadlineTimer === null && !automaticStopped && deadline !== null)
        deadlineTimer = setTimeout(
          stopAutomatic,
          Math.max(0, deadline - Date.now()),
        );
    };
    const applyList = (list: EvidenceList, manual: boolean) => {
      if (list.kind === "pending") {
        if (deadline === null) deadline = Date.now() + 30_000;
        pendingOperation = true;
        setPending(true);
        setEvidence([]);
        setSelected(null);
        setListError(null);
        setLoading(false);
        if (!manual && !automaticStopped) startDeadlineTimer();
        if (manual) {
          automaticStopped = true;
          setAutoRefreshStopped(true);
        } else if (!automaticStopped && !hidden) {
          if (Date.now() >= deadline) stopAutomatic();
          else {
            retryTimer = setTimeout(() => void requestList(false), delay);
            delay = Math.min(2_000, delay * 2);
          }
        }
        return;
      }
      pendingOperation = false;
      automaticStopped = true;
      clearRetry();
      if (deadlineTimer !== null) clearTimeout(deadlineTimer);
      deadlineTimer = null;
      setPending(false);
      setAutoRefreshStopped(false);
      setLoading(false);
      if (list.kind === "stopped") {
        setEvidence([]);
        setSelected(null);
        setListError("other");
        return;
      }
      setEvidence(list.rows);
      setListError(null);
      if (selectedFromState) {
        const found = list.rows.find(
          (row) => row.evidence_id === selectedFromState,
        );
        if (found) setSelected(found);
        else setListError("other");
      }
    };
    const requestList = async (manual: boolean) => {
      if (
        !isCurrent() ||
        hidden ||
        requestController ||
        (!manual && automaticStopped)
      )
        return;
      if (!manual && deadline !== null && Date.now() >= deadline) {
        stopAutomatic();
        return;
      }
      const request = new AbortController();
      requestController = request;
      listController.current = request;
      listControllers.push(request);
      if (!started) setLoading(true);
      try {
        const list = await fetchList(request.signal);
        if (!isCurrent() || request.signal.aborted || hidden) return;
        started = true;
        applyList(list, manual);
      } catch (error) {
        if (!isCurrent() || request.signal.aborted || hidden) return;
        const kind = errorKind(error);
        if (kind === "access") {
          denyMount();
          return;
        }
        pendingOperation = false;
        automaticStopped = true;
        clearRetry();
        if (deadlineTimer !== null) clearTimeout(deadlineTimer);
        deadlineTimer = null;
        setPending(false);
        setAutoRefreshStopped(false);
        setEvidence([]);
        setSelected(null);
        setListError(kind);
        setLoading(false);
      } finally {
        if (requestController === request) {
          requestController = null;
          listController.current = null;
        }
      }
    };
    const loadTimezone = () => {
      if (!isCurrent() || hidden) return;
      if (!zoneController || zoneController.signal.aborted) {
        zoneController = new AbortController();
        mountController.current = zoneController;
      }
      const request = zoneController;
      void props.host
        .request<{ timezone?: unknown }>("/settings/timezone", {
          signal: request.signal,
        })
        .then((value) => {
          if (isCurrent() && !hidden)
            setTimezone(validTimezone(value?.timezone));
        })
        .catch((error: unknown) => {
          if (!isCurrent() || hidden || request.signal.aborted) return;
          if (errorKind(error) === "access") denyMount();
        })
        .finally(() => {
          if (isCurrent() && !hidden && !request.signal.aborted)
            setTimezoneReady(true);
        });
    };
    const onVisibilityChange = () => {
      hidden = document.visibilityState === "hidden";
      if (hidden) {
        clearRetry();
        if (deadlineTimer !== null) clearTimeout(deadlineTimer);
        deadlineTimer = null;
        requestController?.abort();
        zoneController?.abort();
        return;
      }
      startDeadlineTimer();
      loadTimezone();
      if (!started) void requestList(false);
      else if (pendingOperation && !automaticStopped) {
        if (deadline !== null && Date.now() >= deadline) stopAutomatic();
        else void requestList(false);
      }
    };
    setEvidence([]);
    setSelected(null);
    setListError(null);
    setPending(false);
    setAutoRefreshStopped(false);
    setTimezone(null);
    setTimezoneReady(false);
    if (!active || !target || !operationId) {
      setLoading(false);
      return () => zoneController?.abort();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    manualRefresh.current = () => void requestList(true);
    loadTimezone();
    if (!hidden) {
      started = false;
      void requestList(false);
    }
    return () => {
      ++sequence.current;
      clearRetry();
      if (deadlineTimer !== null) clearTimeout(deadlineTimer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      zoneController?.abort();
      requestController?.abort();
      mountController.current?.abort();
      listControllers.forEach((request) => request.abort());
      mountController.current = null;
      listController.current = null;
      manualRefresh.current = null;
    };
  }, [
    props.host,
    props.callId,
    props.status,
    props.pluginId,
    props.toolName,
    props.data,
    props.presentation?.location,
    props.presentation?.active,
    props.presentation?.state,
    operationId,
    target?.agentId,
    target?.threadId,
  ]);

  function openReader(item: Evidence) {
    if (denied.current || !props.callId || !operationId) return;
    props.host.openSidePanel({
      callId: props.callId,
      title: t(locale, "readerTitle"),
      toolName: props.toolName,
      state: { operation_id: operationId, evidence_id: item.evidence_id },
    });
  }
  if (!active) return props.textFallback ? <p>{props.textFallback}</p> : null;
  const errorLabel = listError === "access" ? "accessChanged" : "evidenceError";
  const denyMount = () => {
    if (denied.current) return;
    denied.current = true;
    ++sequence.current;
    mountController.current?.abort();
    listController.current?.abort();
    setEvidence([]);
    setSelected(null);
    setListError("access");
    setLoading(false);
  };
  const reloadList = async () => {
    if (denied.current) return;
    manualRefresh.current?.();
  };
  return (
    <section aria-label={t(locale, "evidenceReadLabel")}>
      {loading && (
        <p role="status" aria-live="polite">
          {t(locale, "evidenceLoading")}
        </p>
      )}
      {pending && !listError && (
        <div role="status" aria-live="polite">
          <p>{t(locale, "evidencePending")}</p>
          {autoRefreshStopped && (
            <button type="button" onClick={() => void reloadList()}>
              {t(locale, "evidenceRefresh")}
            </button>
          )}
        </div>
      )}
      {listError && (
        <div role="status" aria-live="polite">
          <p>{t(locale, errorLabel)}</p>
          {listError !== "access" && (
            <button type="button" onClick={() => void reloadList()}>
              {t(locale, "evidenceRetry")}
            </button>
          )}
        </div>
      )}
      {!loading && !pending && !listError && evidence.length === 0 && (
        <p role="status">{t(locale, "evidenceEmpty")}</p>
      )}
      {!listError && (
        <ul>
          {evidence.map((item) => {
            let timestamp: string | null = null;
            if (timezone) {
              try {
                timestamp = new Intl.DateTimeFormat(locale, {
                  dateStyle: "medium",
                  timeStyle: "short",
                  timeZone: timezone,
                }).format(new Date(item.retrieved_at));
              } catch {
                timestamp = null;
              }
            }
            const sourceLabel = t(
              locale,
              item.source_kind === "KNOWLEDGE"
                ? "evidenceSourceKnowledge"
                : "evidenceSourceWeb",
            );
            return (
              <li key={item.evidence_id}>
                <button
                  type="button"
                  onClick={() => openReader(item)}
                  aria-label={item.title + " — " + sourceLabel}
                >
                  <strong>{item.title || sourceLabel}</strong>
                  <span> {sourceLabel}</span>
                </button>
                {item.source_kind === "WEB" && (
                  <>
                    <p>
                      {item.provider === "FIRECRAWL" ? "Firecrawl" : "SearXNG"}
                    </p>
                    {item.degraded && (
                      <p>{t(locale, "evidenceFallbackUsed")}</p>
                    )}
                    <p>{item.content}</p>
                    <p>{t(locale, "evidenceWebUntrusted")}</p>
                  </>
                )}
                <p>
                  {timezoneReady
                    ? timestamp || t(locale, "timezoneUnavailable")
                    : t(locale, "timezoneUnavailable")}
                </p>
                <p>
                  {item.source_page === null
                    ? t(locale, "evidenceNoPage")
                    : t(locale, "evidencePage") + " " + item.source_page}
                </p>
                {item.truncated && <p>{t(locale, "evidenceTruncated")}</p>}
              </li>
            );
          })}
        </ul>
      )}
      {sidePanel &&
        selected &&
        (selected.source_kind === "WEB" ? (
          <WebEvidenceReader
            key={selected.evidence_id}
            host={props.host}
            target={target!}
            item={selected}
            locale={locale}
            onAccessDenied={denyMount}
          />
        ) : (
          <EvidenceReader
            key={selected.evidence_id}
            host={props.host}
            target={target!}
            item={selected}
            locale={locale}
            onAccessDenied={denyMount}
            reloadEvidence={reload}
          />
        ))}
    </section>
  );
}

function WebEvidenceReader({
  host,
  target,
  item,
  locale,
  onAccessDenied,
}: {
  host: PluginToolProps["host"];
  target: PluginToolContext;
  item: Evidence;
  locale: string;
  onAccessDenied: () => void;
}) {
  const [state, setState] = useState<
    "idle" | "loading" | "ready" | "source" | "error"
  >("idle");
  const [locator, setLocator] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const heading = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    ++sequence.current;
    controller.current?.abort();
    setState("idle");
    setLocator(null);
    queueMicrotask(() => heading.current?.focus());
    return () => {
      ++sequence.current;
      controller.current?.abort();
    };
  }, [
    host,
    target.agentId,
    target.threadId,
    item.operation_id,
    item.evidence_id,
  ]);

  async function authorizeLocator() {
    const id = ++sequence.current;
    const request = new AbortController();
    controller.current?.abort();
    controller.current = request;
    setState("loading");
    setLocator(null);
    try {
      const value = await toolAction<unknown>(
        host,
        target,
        "evidence.original",
        {
          operation_id: item.operation_id,
          evidence_id: item.evidence_id,
        },
        request.signal,
      );
      if (
        sequence.current !== id ||
        request.signal.aborted ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      if (
        !object(value) ||
        value.schema_id !== schemaId ||
        value.version !== 1 ||
        value.operation_id !== item.operation_id ||
        value.evidence_id !== item.evidence_id ||
        value.available !== true ||
        value.reason !== null ||
        value.resource_token !== null ||
        value.locator !== item.locator ||
        !validWebLocator(value.locator)
      )
        throw new Error("Invalid web original response");
      setLocator(value.locator);
      setState("ready");
    } catch (error) {
      if (
        sequence.current !== id ||
        request.signal.aborted ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      const kind = errorKind(error);
      if (kind === "access") onAccessDenied();
      else setState(kind === "source" ? "source" : "error");
    } finally {
      if (controller.current === request) controller.current = null;
    }
  }

  return (
    <section aria-label={t(locale, "webSourceTitle")}>
      <h2 ref={heading} tabIndex={-1}>
        {t(locale, "webSourceTitle")}
      </h2>
      <p>{item.content}</p>
      <p>{t(locale, "evidenceWebUntrusted")}</p>
      <button
        type="button"
        disabled={state === "loading"}
        onClick={() => void authorizeLocator()}
      >
        {t(locale, "webSourceAuthorize")}
      </button>
      {state === "loading" && (
        <p role="status" aria-live="polite">
          {t(locale, "originalLoading")}
        </p>
      )}
      {state === "source" && (
        <p role="status" aria-live="polite">
          {t(locale, "sourceChanged")}
        </p>
      )}
      {state === "error" && (
        <p role="status" aria-live="polite">
          {t(locale, "originalError")}
        </p>
      )}
      {state === "ready" && locator && (
        <a href={locator} target="_blank" rel="noreferrer noopener">
          {t(locale, "webSourceOpen")}
        </a>
      )}
    </section>
  );
}

function EvidenceReader({
  host,
  target,
  item,
  locale,
  onAccessDenied,
  reloadEvidence,
}: {
  host: PluginToolProps["host"];
  target: PluginToolContext;
  item: Evidence;
  locale: string;
  onAccessDenied: () => void;
  reloadEvidence: (signal?: AbortSignal) => Promise<Evidence[]>;
}) {
  const [authorizedItem, setAuthorizedItem] = useState(item);
  const [text, setText] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<
    | "loading"
    | "ready"
    | "empty"
    | "end"
    | "error"
    | "source"
    | "cursor"
    | "refresh"
    | "refreshError"
  >("loading");
  const [original, setOriginal] = useState<
    "idle" | "loading" | "unavailable" | "error"
  >("idle");
  const [busy, setBusy] = useState(false);
  const [appending, setAppending] = useState(false);
  const selectionSequence = useRef(0);
  const readSequence = useRef(0);
  const slot = useRef<string | null>(null);
  const terminated = useRef(false);
  const readController = useRef<AbortController | null>(null);
  const originalController = useRef<AbortController | null>(null);
  const restartController = useRef<AbortController | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  async function read(
    next: string | null,
    append: boolean,
    evidence: Evidence = authorizedItem,
  ) {
    if (slot.current || terminated.current) return;
    const id = String(++readSequence.current);
    const selectionId = selectionSequence.current;
    const controller = new AbortController();
    readController.current?.abort();
    readController.current = controller;
    slot.current = id;
    setBusy(true);
    setAppending(append);
    setStatus("loading");
    try {
      const response = await toolAction<unknown>(
        host,
        target,
        "evidence.read",
        {
          operation_id: evidence.operation_id,
          evidence_id: evidence.evidence_id,
          ...(next ? { continuation: next } : {}),
        },
        controller.signal,
      );
      if (
        slot.current !== id ||
        readSequence.current !== Number(id) ||
        selectionSequence.current !== selectionId ||
        terminated.current ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      if (
        !object(response) ||
        response.schema_id !== schemaId ||
        response.version !== 1 ||
        response.operation_id !== evidence.operation_id ||
        response.evidence_id !== evidence.evidence_id ||
        response.knowledge_id !== evidence.knowledge_id ||
        typeof response.knowledge_id !== "string" ||
        !response.knowledge_id ||
        (response.status !== "SUCCESS" && response.status !== "EMPTY") ||
        typeof response.content !== "string" ||
        [...response.content].length > 8000 ||
        typeof response.truncated !== "boolean" ||
        !(
          response.continuation === null ||
          (typeof response.continuation === "string" &&
            uuid4.test(response.continuation))
        ) ||
        response.truncated !== (response.continuation !== null) ||
        response.source_version !== null ||
        (response.status === "EMPTY" &&
          (response.content !== "" ||
            response.truncated ||
            response.continuation !== null))
      )
        throw new Error("Invalid read response");
      const pageText = response.content as string;
      setText((previous) => (append ? previous + pageText : pageText));
      setCursor(response.continuation as string | null);
      setStatus(
        response.status === "EMPTY" ? (append ? "end" : "empty") : "ready",
      );
    } catch (error) {
      if (
        slot.current !== id ||
        readSequence.current !== Number(id) ||
        selectionSequence.current !== selectionId ||
        terminated.current ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      const kind = errorKind(error);
      if (kind === "access") {
        terminate();
        onAccessDenied();
        return;
      }
      if (kind === "source") {
        sourceChanged();
        return;
      }
      if (kind === "cursor") {
        ++readSequence.current;
        setCursor(null);
        setStatus("cursor");
        return;
      }
      setStatus("error");
    } finally {
      if (slot.current === id) {
        slot.current = null;
        readController.current = null;
        setBusy(false);
        setAppending(false);
      }
    }
  }
  useEffect(() => {
    terminated.current = false;
    setAuthorizedItem(item);
    setText("");
    setCursor(null);
    setOriginal("idle");
    ++selectionSequence.current;
    slot.current = null;
    void read(null, false, item);
    queueMicrotask(() => heading.current?.focus());
    return () => {
      ++selectionSequence.current;
      ++readSequence.current;
      slot.current = null;
      readController.current?.abort();
      originalController.current?.abort();
      restartController.current?.abort();
    };
  }, [
    host,
    target.agentId,
    target.threadId,
    item.operation_id,
    item.evidence_id,
  ]);
  async function restart() {
    if (terminated.current) return;
    ++selectionSequence.current;
    ++readSequence.current;
    slot.current = null;
    readController.current?.abort();
    originalController.current?.abort();
    restartController.current?.abort();
    const controller = new AbortController();
    restartController.current = controller;
    const restartSequence = selectionSequence.current;
    setText("");
    setCursor(null);
    setStatus("refresh");
    setOriginal("idle");
    try {
      const rows = await reloadEvidence(controller.signal);
      if (
        selectionSequence.current !== restartSequence ||
        terminated.current ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      const freshItem = rows.find(
        (row) =>
          row.evidence_id === item.evidence_id &&
          row.source_kind === "KNOWLEDGE",
      );
      if (!freshItem) {
        onAccessDenied();
        return;
      }
      setAuthorizedItem(freshItem);
      void read(null, false, freshItem);
    } catch (error) {
      if (
        selectionSequence.current !== restartSequence ||
        terminated.current ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      const kind = errorKind(error);
      if (kind === "access") {
        terminate();
        onAccessDenied();
      } else setStatus("refreshError");
    }
  }
  async function checkOriginal() {
    if (terminated.current) return;
    const requestSequence = selectionSequence.current;
    const controller = new AbortController();
    originalController.current?.abort();
    originalController.current = controller;
    setOriginal("loading");
    try {
      const value = await toolAction<unknown>(
        host,
        target,
        "evidence.original",
        {
          operation_id: authorizedItem.operation_id,
          evidence_id: authorizedItem.evidence_id,
        },
        controller.signal,
      );
      if (
        selectionSequence.current !== requestSequence ||
        terminated.current ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      if (
        !object(value) ||
        value.schema_id !== schemaId ||
        value.version !== 1 ||
        value.operation_id !== authorizedItem.operation_id ||
        value.evidence_id !== authorizedItem.evidence_id ||
        value.available !== false ||
        value.reason !== "CAPABILITY_UNVERIFIED" ||
        value.resource_token !== null
      )
        throw new Error("Invalid original response");
      setOriginal("unavailable");
    } catch (error) {
      if (
        selectionSequence.current !== requestSequence ||
        terminated.current ||
        controller.signal.aborted ||
        currentTarget(host)?.agentId !== target.agentId ||
        currentTarget(host)?.threadId !== target.threadId
      )
        return;
      const kind = errorKind(error);
      if (kind === "access") {
        terminate();
        onAccessDenied();
      } else if (kind === "source" || kind === "cursor") {
        sourceChanged();
      } else setOriginal("error");
    } finally {
      if (originalController.current === controller)
        originalController.current = null;
    }
  }
  function terminate() {
    if (terminated.current) return;
    terminated.current = true;
    ++selectionSequence.current;
    ++readSequence.current;
    slot.current = null;
    readController.current?.abort();
    originalController.current?.abort();
    restartController.current?.abort();
    setText("");
    setCursor(null);
    setBusy(false);
    setAppending(false);
  }
  function sourceChanged() {
    ++selectionSequence.current;
    ++readSequence.current;
    slot.current = null;
    readController.current?.abort();
    originalController.current?.abort();
    restartController.current?.abort();
    setCursor(null);
    setStatus("source");
    setOriginal("idle");
    setBusy(false);
    setAppending(false);
  }
  const statusText =
    status === "loading"
      ? t(locale, appending ? "readerMoreLoading" : "readerLoading")
      : status === "refresh"
      ? t(locale, "readerLoading")
      : status === "empty"
      ? t(locale, "readerEmpty")
      : status === "end"
      ? t(locale, "readerEnd")
      : status === "source"
      ? t(locale, "sourceChanged")
      : status === "cursor"
      ? t(locale, "cursorInvalid")
      : status === "error"
      ? t(locale, "readerError")
      : status === "refreshError"
      ? t(locale, "readerError")
      : "";
  return (
    <section aria-label={t(locale, "readerTitle")}>
      <h2 ref={heading} tabIndex={-1}>
        {t(locale, "readerTitle")}
      </h2>
      <p>{t(locale, "evidenceReadLabel")}</p>
      {statusText && (
        <p role="status" aria-live="polite">
          {statusText}
        </p>
      )}
      {text && <pre>{text}</pre>}
      {status === "error" && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void read(cursor, Boolean(cursor))}
        >
          {t(locale, "readerRetry")}
        </button>
      )}
      {status === "refreshError" && (
        <button type="button" disabled={busy} onClick={() => void restart()}>
          {t(locale, "readerRetry")}
        </button>
      )}
      {(status === "source" || status === "cursor") && (
        <button type="button" disabled={busy} onClick={() => void restart()}>
          {t(locale, "readerRestart")}
        </button>
      )}
      {status === "ready" && cursor && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void read(cursor, true)}
        >
          {busy ? t(locale, "readerMoreLoading") : t(locale, "readerMore")}
        </button>
      )}
      <button
        type="button"
        disabled={
          terminated.current ||
          busy ||
          original === "loading" ||
          status === "source" ||
          status === "cursor"
        }
        onClick={() => void checkOriginal()}
      >
        {t(locale, "original")}
      </button>
      {original === "loading" && (
        <p role="status" aria-live="polite">
          {t(locale, "originalLoading")}
        </p>
      )}
      {original === "unavailable" && (
        <p role="status" aria-live="polite">
          {t(locale, "originalUnavailable")}
        </p>
      )}
      {original === "error" && (
        <p role="status" aria-live="polite">
          {t(locale, "originalError")}
        </p>
      )}
    </section>
  );
}
