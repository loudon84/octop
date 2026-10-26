import * as React from "./host";
import type {
  ComposerPreparationHandler,
  ComposerSelectionCapture,
  PluginHost,
  PluginJsonObject,
  PluginSurfaceContext,
} from "./host";
import { classifyTransportError } from "./api";
import { t, type MessageKey } from "./i18n";
import { PublicQueryDialog } from "./PublicQueryDialog";
import {
  approveQuery,
  getSession,
  loadCatalog,
  prepareOperation,
  saveSession,
  type CatalogItem,
  type KnowledgeMode,
  type SessionSnapshot,
} from "./composerApi";

type Props = {
  host: PluginHost;
  context: PluginSurfaceContext;
};

function errorCopy(error: unknown, locale: string): string {
  const classified = classifyTransportError(error);
  if (classified.status === 403) return t(locale, "denied");
  if (classified.status === 409 || classified.code === "CONFIG_CONFLICT")
    return t(locale, "conflict");
  if (classified.code === "MODE_NOT_ALLOWED")
    return t(locale, "composerModeBlocked");
  if (
    classified.code === "EMPTY_SELECTION" ||
    classified.code === "SCOPE_EMPTY"
  )
    return t(locale, "composerEmptyBlocksSend");
  return t(locale, "error");
}

function sourceRevisions(session: SessionSnapshot) {
  return {
    connection_id: session.connection_id,
    config_revision: session.config_revision,
    binding_revision: session.binding_revision,
    grant_revision: session.grant_revision,
    search_revision: session.search_revision,
  };
}

function targetIsCurrent(
  host: PluginHost,
  target: { agentId: string; threadId: string },
  generation: string,
): boolean {
  const current = host.getContext();
  return Boolean(
    current &&
      current.agentId === target.agentId &&
      current.generation === generation &&
      (!current.threadId || current.threadId === target.threadId),
  );
}

const MODES: KnowledgeMode[] = ["KB_ONLY", "WEB_ONLY", "HYBRID"];

function modeLabel(mode: KnowledgeMode): MessageKey {
  if (mode === "KB_ONLY") return "composerModeKb";
  if (mode === "WEB_ONLY") return "composerModeWeb";
  return "composerModeHybrid";
}

function modeUnavailable(
  session: SessionSnapshot,
  mode: KnowledgeMode,
): MessageKey | null {
  if (mode === session.mode && session.invalid) return "composerInvalidSource";
  if (mode === "KB_ONLY") {
    return session.capabilities.knowledge_ready ||
      session.capabilities.can_select_knowledge
      ? null
      : "composerKnowledgeNotReady";
  }
  if (mode === "WEB_ONLY") {
    return session.capabilities.web_ready ? null : "composerWebNotReady";
  }
  return session.capabilities.knowledge_ready && session.capabilities.web_ready
    ? null
    : "composerHybridNotReady";
}

type PublicQueryRequest = {
  operationId: string;
  target: { agentId: string; threadId: string; generation: string };
  signal: AbortSignal;
  trigger: HTMLElement | null;
  resolve: (approved: boolean) => void;
  removeAbortListener: () => void;
};

export function Composer({ host, context }: Props) {
  const { agentId, threadId, generation } = context;
  const locale = context.locale;
  const text = (key: MessageKey) => t(locale, key);
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [conflict, setConflict] = React.useState(false);
  const [session, setSession] = React.useState<SessionSnapshot | null>(null);
  const [selected, setSelected] = React.useState<string[]>([]);
  const [mode, setMode] = React.useState<KnowledgeMode>("KB_ONLY");
  const [catalog, setCatalog] = React.useState<CatalogItem[]>([]);
  const [queryRequest, setQueryRequest] =
    React.useState<PublicQueryRequest | null>(null);
  const [queryPending, setQueryPending] = React.useState(false);
  const [queryError, setQueryError] = React.useState("");
  const loadGen = React.useRef(0);
  const scopeRef = React.useRef({ agentId, threadId, generation });
  scopeRef.current = { agentId, threadId, generation };
  const stateGen = React.useRef(0);
  const sessionRef = React.useRef<SessionSnapshot | null>(null);
  const selectedRef = React.useRef<string[]>([]);
  const modeRef = React.useRef<KnowledgeMode>("KB_ONLY");
  const savingRef = React.useRef(false);
  const queryRequestRef = React.useRef<PublicQueryRequest | null>(null);
  const focusTriggerRef = React.useRef<HTMLElement | null>(null);
  const queryPendingRef = React.useRef(false);
  const preparingGen = React.useRef<string | null>(null);

  React.useEffect(() => {
    sessionRef.current = session;
  }, [session]);
  React.useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  React.useEffect(() => {
    modeRef.current = mode;
  }, [mode]);

  const applySession = React.useCallback((next: SessionSnapshot) => {
    stateGen.current += 1;
    setSession(next);
    setSelected([...next.selected_kb_ids]);
    setMode(next.mode);
    sessionRef.current = next;
    selectedRef.current = [...next.selected_kb_ids];
    modeRef.current = next.mode;
  }, []);

  const refresh = React.useCallback(async () => {
    const gen = ++loadGen.current;
    const key = `${generation}:${agentId ?? ""}:${threadId ?? ""}`;
    const isScopeCurrent = () => {
      const current = scopeRef.current;
      return (
        `${current.generation}:${current.agentId ?? ""}:${
          current.threadId ?? ""
        }` === key
      );
    };
    if (!agentId || !threadId) {
      setLoading(false);
      setSession(null);
      setError("");
      setNotice(t(locale, "composerNeedThread"));
      return;
    }
    setLoading(true);
    setSaving(false);
    savingRef.current = false;
    setSession(null);
    sessionRef.current = null;
    setSelected([]);
    selectedRef.current = [];
    setCatalog([]);
    setError("");
    setConflict(false);
    const refreshStateGen = stateGen.current;
    try {
      const target = {
        agentId,
        threadId,
        generation,
      };
      let output = await getSession(host, target);
      if (gen !== loadGen.current || !isScopeCurrent()) return;
      let snap = output.session;
      if (stateGen.current !== refreshStateGen) {
        const activeSession = sessionRef.current;
        if (!activeSession) return;
        snap = activeSession;
      } else {
        if (
          snap.agent_id !== target.agentId ||
          snap.thread_id !== target.threadId
        )
          throw new Error("Composer context changed");
        if (!snap.selection_present) {
          try {
            output = await saveSession(
              host,
              {
                expected_revision: 0,
                selected_kb_ids: [...snap.selected_kb_ids],
                mode: snap.mode,
                source_revisions: sourceRevisions(snap),
              },
              target,
            );
            snap = output.session;
          } catch (err) {
            const classified = classifyTransportError(err);
            if (
              classified.status === 409 ||
              classified.code === "CONFIG_CONFLICT"
            ) {
              output = await getSession(host, target);
              snap = output.session;
            } else {
              throw err;
            }
          }
        }
      }
      if (
        snap.agent_id !== target.agentId ||
        snap.thread_id !== target.threadId
      )
        throw new Error("Composer context changed");
      if (stateGen.current !== refreshStateGen) {
        const activeSession = sessionRef.current;
        if (!activeSession) return;
        snap = activeSession;
      }
      const fallbackKnowledgeIds = [...snap.selected_kb_ids];
      if (gen !== loadGen.current || !isScopeCurrent()) return;
      if (stateGen.current === refreshStateGen) applySession(snap);
      if (snap.connection_id && snap.mode !== "WEB_ONLY") {
        try {
          const catalogResult = await loadCatalog(
            host,
            snap.connection_id,
            snap.config_revision,
            target,
          );
          if (
            gen !== loadGen.current ||
            !isScopeCurrent() ||
            sessionRef.current !== snap
          )
            return;
          setCatalog([...catalogResult.knowledge_bases]);
        } catch {
          if (
            gen === loadGen.current &&
            isScopeCurrent() &&
            sessionRef.current === snap
          )
            setCatalog(
              fallbackKnowledgeIds.map((kb_id) => ({ kb_id, name: kb_id })),
            );
        }
      } else {
        setCatalog([]);
      }
      setNotice(t(locale, "composerLoaded"));
    } catch (err) {
      if (
        gen !== loadGen.current ||
        !isScopeCurrent() ||
        stateGen.current !== refreshStateGen
      )
        return;
      setError(errorCopy(err, locale));
      setSession(null);
    } finally {
      if (gen === loadGen.current) setLoading(false);
    }
  }, [agentId, applySession, generation, host, locale, threadId]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const persistSelection = async (ids: string[], nextMode = mode) => {
    if (!session || saving) return;
    if (!session.capabilities.can_save_selection) {
      setError(text("denied"));
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setError("");
    const target = {
      agentId: context.agentId!,
      threadId: context.threadId!,
      generation: context.generation,
    };
    const isCurrent = () => targetIsCurrent(host, target, target.generation);
    try {
      const saved = await saveSession(
        host,
        {
          expected_revision: session.revision,
          selected_kb_ids: ids,
          mode: nextMode,
          source_revisions: sourceRevisions(session),
        },
        target,
      );
      if (!isCurrent()) return;
      applySession(saved.session);
      if (saved.session.mode === "WEB_ONLY") {
        setCatalog([]);
      } else if (saved.session.connection_id) {
        try {
          const result = await loadCatalog(
            host,
            saved.session.connection_id,
            saved.session.config_revision,
            target,
          );
          if (
            isCurrent() &&
            sessionRef.current?.revision === saved.session.revision
          )
            setCatalog([...result.knowledge_bases]);
        } catch {
          if (
            isCurrent() &&
            sessionRef.current?.revision === saved.session.revision
          )
            setCatalog(
              saved.session.selected_kb_ids.map((kb_id) => ({
                kb_id,
                name: kb_id,
              })),
            );
        }
      }
      setNotice(
        ids.length === 0
          ? text("composerCleared")
          : text("composerSelectionSaved"),
      );
      setConflict(false);
    } catch (err) {
      if (!isCurrent()) return;
      const classified = classifyTransportError(err);
      if (classified.status === 409 || classified.code === "CONFIG_CONFLICT") {
        setConflict(true);
        setError(text("conflict"));
      } else {
        setError(errorCopy(err, locale));
      }
      setMode(session.mode);
      modeRef.current = session.mode;
    } finally {
      if (isCurrent()) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  };

  const toggle = (kbId: string) => {
    const next = selected.includes(kbId)
      ? selected.filter((id) => id !== kbId)
      : [...selected, kbId];
    stateGen.current += 1;
    setSelected(next);
    selectedRef.current = next;
  };

  const finishQueryRequest = (approved: boolean) => {
    const request = queryRequestRef.current;
    if (!request) return;
    queryRequestRef.current = null;
    request.removeAbortListener();
    setQueryRequest(null);
    setQueryPending(false);
    queryPendingRef.current = false;
    setQueryError("");
    request.resolve(approved);
    window.requestAnimationFrame(() => {
      const trigger = request.trigger;
      if (
        trigger?.isConnected &&
        !trigger.matches(":disabled") &&
        targetIsCurrent(host, request.target, request.target.generation)
      )
        trigger.focus();
    });
  };

  const requestPublicQuery = (
    operationId: string,
    target: PublicQueryRequest["target"],
    signal: AbortSignal,
  ): Promise<boolean> =>
    new Promise((resolve) => {
      const trigger = focusTriggerRef.current;
      focusTriggerRef.current = null;
      const onAbort = () => finishQueryRequest(false);
      const request: PublicQueryRequest = {
        operationId,
        target,
        signal,
        trigger,
        resolve,
        removeAbortListener: () => signal.removeEventListener("abort", onAbort),
      };
      if (signal.aborted) {
        resolve(false);
        return;
      }
      queryRequestRef.current = request;
      setQueryRequest(request);
      setQueryPending(false);
      queryPendingRef.current = false;
      setQueryError("");
      signal.addEventListener("abort", onAbort, { once: true });
    });

  React.useEffect(() => {
    if (typeof host.registerComposerPreparation !== "function") return;
    const handler: ComposerPreparationHandler = {
      captureSelection(): ComposerSelectionCapture {
        const activeElement = document.activeElement;
        focusTriggerRef.current =
          activeElement instanceof HTMLElement &&
          activeElement !== document.body &&
          activeElement.isConnected
            ? activeElement
            : null;
        const currentScope = scopeRef.current;
        const current = sessionRef.current;
        if (!current) return { state: "uninitialized" };
        return {
          state: "selected",
          value: {
            selected_kb_ids: [...selectedRef.current],
            session_revision: current.revision,
            mode: modeRef.current,
            saving: savingRef.current,
            agent_id: currentScope.agentId,
            thread_id: currentScope.threadId,
            generation: currentScope.generation,
          },
        };
      },
      async prepare(input) {
        preparingGen.current = input.generation;
        const target = {
          ...input.target,
          generation: input.generation,
        };
        const currentScope = scopeRef.current;
        if (input.signal.aborted) {
          throw new Error("Composer preparation cancelled");
        }
        if (
          target.agentId !== currentScope.agentId ||
          (currentScope.threadId &&
            target.threadId !== currentScope.threadId) ||
          input.generation !== currentScope.generation ||
          !targetIsCurrent(host, target, input.generation)
        ) {
          throw new Error("Composer context changed");
        }
        let captured = input.selection;
        let current: SessionSnapshot;
        if (captured.state === "uninitialized") {
          const initialSession = sessionRef.current;
          const initialIds = [...selectedRef.current];
          const initialMode = modeRef.current;
          const initialSaving = savingRef.current;
          const initialLoadGen = loadGen.current;
          const initializationStillCurrent = () =>
            targetIsCurrent(host, target, input.generation) &&
            loadGen.current === initialLoadGen &&
            sessionRef.current === initialSession &&
            savingRef.current === initialSaving &&
            modeRef.current === initialMode &&
            selectedRef.current.length === initialIds.length &&
            selectedRef.current.every((id, index) => id === initialIds[index]);
          let output = await getSession(host, target, input.signal);
          if (!initializationStillCurrent())
            throw new Error(t(locale, "composerUnsavedBlocksSend"));
          current = output.session;
          if (!current.selection_present) {
            try {
              output = await saveSession(
                host,
                {
                  expected_revision: 0,
                  selected_kb_ids: [...current.selected_kb_ids],
                  mode: current.mode,
                  source_revisions: sourceRevisions(current),
                },
                target,
                input.signal,
              );
              current = output.session;
              if (!initializationStillCurrent())
                throw new Error(t(locale, "composerUnsavedBlocksSend"));
            } catch (err) {
              if (!initializationStillCurrent())
                throw new Error(t(locale, "composerUnsavedBlocksSend"));
              const classified = classifyTransportError(err);
              if (
                classified.status !== 409 &&
                classified.code !== "CONFIG_CONFLICT"
              )
                throw err;
              output = await getSession(host, target, input.signal);
              if (!initializationStillCurrent())
                throw new Error(t(locale, "composerUnsavedBlocksSend"));
              current = output.session;
            }
          }
          if (
            current.agent_id !== target.agentId ||
            current.thread_id !== target.threadId
          )
            throw new Error("Composer context changed");
          if (
            scopeRef.current.threadId === target.threadId &&
            targetIsCurrent(host, target, input.generation)
          ) {
            applySession(current);
          }
          captured = {
            state: "selected",
            value: {
              selected_kb_ids: [...current.selected_kb_ids],
              session_revision: current.revision,
              mode: current.mode,
              saving: false,
              agent_id: target.agentId,
              thread_id: target.threadId,
              generation: input.generation,
            },
          };
        } else {
          current = sessionRef.current as SessionSnapshot;
        }
        if (captured.state !== "selected")
          throw new Error("Composer selection is uninitialized");
        if (
          captured.value.agent_id !== target.agentId ||
          captured.value.thread_id !== target.threadId ||
          captured.value.generation !== input.generation
        )
          throw new Error("Composer context changed");
        const revision = Number(captured.value.session_revision);
        const ids = captured.value.selected_kb_ids;
        const selectedMode = String(captured.value.mode) as KnowledgeMode;
        if (!MODES.includes(selectedMode))
          throw new Error(t(locale, "composerModeBlocked"));
        const selectedIds = Array.isArray(ids) ? ids.map(String) : [];
        const matchesPersistedSelection =
          selectedMode === current.mode &&
          revision === current.revision &&
          selectedIds.length === current.selected_kb_ids.length &&
          selectedIds.every(
            (id, index) => id === current.selected_kb_ids[index],
          );
        const hasCurrentDraft =
          sessionRef.current?.agent_id === target.agentId &&
          sessionRef.current.thread_id === target.threadId;
        const matchesLiveDraft =
          !hasCurrentDraft ||
          (selectedMode === modeRef.current &&
            selectedIds.length === selectedRef.current.length &&
            selectedIds.every(
              (id, index) => id === selectedRef.current[index],
            ));
        if (
          captured.value.saving === true ||
          savingRef.current ||
          !matchesPersistedSelection ||
          !matchesLiveDraft
        ) {
          throw new Error(t(locale, "composerUnsavedBlocksSend"));
        }
        if (
          !current ||
          current.agent_id !== target.agentId ||
          current.thread_id !== target.threadId
        )
          throw new Error("Composer selection is uninitialized");
        const unavailable = modeUnavailable(current, selectedMode);
        if (unavailable) throw new Error(t(locale, unavailable));
        if (selectedMode === "KB_ONLY" && !current.capabilities.knowledge_ready)
          throw new Error(t(locale, "composerKnowledgeNotReady"));
        if (selectedMode !== "WEB_ONLY" && selectedIds.length === 0)
          throw new Error(t(locale, "composerEmptyBlocksSend"));
        if (!Number.isInteger(revision)) {
          throw new Error("Invalid session revision");
        }
        const prepared = await prepareOperation(
          host,
          revision,
          target,
          input.signal,
        );
        if (input.signal.aborted) {
          throw new Error("Composer preparation cancelled");
        }
        // Ignore late prepare generations for UI mutation; host keeps result.
        if (
          preparingGen.current !== input.generation ||
          !targetIsCurrent(host, target, input.generation)
        ) {
          throw new Error("Composer preparation superseded");
        }
        const operationId = prepared.operation.operation_id;
        if (typeof operationId !== "string" || !operationId) {
          throw new Error("Invalid operation");
        }
        if (
          selectedMode !== "KB_ONLY" &&
          !(await requestPublicQuery(operationId, target, input.signal))
        ) {
          throw new Error(t(locale, "publicQueryCancelled"));
        }
        return { operation_id: operationId } satisfies PluginJsonObject;
      },
      async restoreSelection(selection) {
        if (selection.state !== "selected") return;
        const ids = selection.value.selected_kb_ids;
        if (Array.isArray(ids)) {
          const restoredIds = ids.map(String);
          stateGen.current += 1;
          setSelected(restoredIds);
          selectedRef.current = restoredIds;
        }
        const restoredMode = selection.value.mode;
        if (MODES.includes(restoredMode as KnowledgeMode)) {
          stateGen.current += 1;
          setMode(restoredMode as KnowledgeMode);
          modeRef.current = restoredMode as KnowledgeMode;
        }
      },
    };
    const dispose = host.registerComposerPreparation(handler);
    return () => {
      dispose();
      const request = queryRequestRef.current;
      if (!request) return;
      queryRequestRef.current = null;
      request.removeAbortListener();
      request.resolve(false);
    };
  }, [host, locale]);

  if (!context.agentId || !context.threadId) {
    return (
      <section className="ek-composer" aria-labelledby="ek-composer-title">
        <h3 id="ek-composer-title">{text("composerTitle")}</h3>
        <p role="status">{text("composerNeedThread")}</p>
      </section>
    );
  }

  if (loading && !session) {
    return <p role="status">{text("composerLoading")}</p>;
  }

  if (!session) {
    return (
      <section className="ek-composer">
        <h3>{text("composerTitle")}</h3>
        {error && <p role="alert">{error}</p>}
        <button type="button" onClick={() => void refresh()}>
          {text("retry")}
        </button>
      </section>
    );
  }

  const items =
    catalog.length > 0
      ? catalog
      : selected.map((kb_id) => ({ kb_id, name: kb_id }));
  const canEdit = session.capabilities.can_select_knowledge && !saving;
  const selectedModeReason = modeUnavailable(session, mode);
  const emptyBlocks = mode !== "WEB_ONLY" && selected.length === 0;

  return (
    <section className="ek-composer" aria-labelledby="ek-composer-title">
      <style>{`.ek-composer{display:grid;gap:.65rem;margin:.5rem 0;padding:.85rem;border:1px solid var(--fn-border-primary,rgba(127,127,127,.35));border-radius:.65rem}.ek-composer fieldset{display:grid;gap:.4rem;border:1px solid var(--fn-border-primary,rgba(127,127,127,.35));border-radius:.4rem}.ek-composer .ek-choice{display:flex;align-items:center;gap:.45rem}.ek-composer .ek-mode-reason{display:block;margin-left:1.5rem}.ek-composer .ek-muted{color:var(--fn-text-secondary,#777)}.ek-composer [role=alert]{color:var(--fn-color-error,#b42318)}.ek-dialog-backdrop{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;padding:1rem;background:rgba(0,0,0,.55)}.ek-dialog{display:grid;gap:.75rem;width:min(32rem,100%);padding:1rem;border-radius:.65rem;background:var(--fn-bg-primary,#fff);color:var(--fn-text-primary,#111)}.ek-dialog textarea{display:block;width:100%;min-height:7rem;margin-top:.35rem;box-sizing:border-box}.ek-dialog-actions{display:flex;justify-content:flex-end;gap:.5rem}`}</style>
      <h3 id="ek-composer-title">{text("composerTitle")}</h3>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {conflict && (
        <button type="button" onClick={() => void refresh()}>
          {text("reload")}
        </button>
      )}
      <p role="status" className="ek-muted">
        {text("revision")}: {session.revision}
        {session.invalid
          ? ` · ${session.invalid_reason ?? text("blocked")}`
          : ""}
      </p>
      <fieldset>
        <legend>{text("composerMode")}</legend>
        {MODES.map((item) => {
          const reason = modeUnavailable(session, item);
          const reasonId = `ek-mode-${item}-reason`;
          return (
            <div key={item}>
              <label className="ek-choice">
                <input
                  type="radio"
                  name="enterprise-knowledge-mode"
                  checked={mode === item}
                  disabled={saving || reason !== null}
                  aria-describedby={reason ? reasonId : undefined}
                  onChange={() => {
                    stateGen.current += 1;
                    setMode(item);
                    modeRef.current = item;
                    void persistSelection(selected, item);
                  }}
                />
                {text(modeLabel(item))}
              </label>
              {reason && (
                <span id={reasonId} className="ek-muted ek-mode-reason">
                  {text(reason)}
                </span>
              )}
            </div>
          );
        })}
      </fieldset>
      <fieldset disabled={!canEdit}>
        <legend>{text("composerSelectKb")}</legend>
        {items.length === 0 && <p role="status">{text("composerNoCatalog")}</p>}
        {items.map((item) => (
          <label key={item.kb_id} className="ek-choice">
            <input
              type="checkbox"
              checked={selected.includes(item.kb_id)}
              onChange={() => toggle(item.kb_id)}
            />
            {item.name}
          </label>
        ))}
      </fieldset>
      {emptyBlocks && <p role="status">{text("composerEmptyBlocksSend")}</p>}
      {!emptyBlocks && selectedModeReason && (
        <p role="status">{text(selectedModeReason)}</p>
      )}
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <button
          type="button"
          disabled={!canEdit}
          onClick={() => void persistSelection(selected)}
        >
          {text("composerSaveSelection")}
        </button>
        <button
          type="button"
          disabled={!canEdit}
          onClick={() => {
            setSelected([]);
            void persistSelection([], mode);
          }}
        >
          {text("composerClearSelection")}
        </button>
      </div>
      {queryRequest && (
        <PublicQueryDialog
          locale={locale}
          error={queryError}
          pending={queryPending}
          onCancel={() => finishQueryRequest(false)}
          onConfirm={(publicQuery) => {
            const request = queryRequestRef.current;
            if (!request || queryPendingRef.current) return;
            queryPendingRef.current = true;
            setQueryPending(true);
            setQueryError("");
            void approveQuery(
              host,
              request.operationId,
              publicQuery,
              request.target,
              request.signal,
            )
              .then(() => finishQueryRequest(true))
              .catch((err) => {
                if (queryRequestRef.current !== request) return;
                if (
                  !targetIsCurrent(
                    host,
                    request.target,
                    request.target.generation,
                  )
                ) {
                  finishQueryRequest(false);
                  return;
                }
                queryPendingRef.current = false;
                setQueryPending(false);
                setQueryError(errorCopy(err, locale));
              });
          }}
        />
      )}
    </section>
  );
}
