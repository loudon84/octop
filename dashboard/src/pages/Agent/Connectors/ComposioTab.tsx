import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Input, Modal, Spin, Tag } from "antd";
import { Cloud, Link2, RefreshCw, Search, Unplug } from "lucide-react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import {
  connectorsApi,
  type ComposioAppSummary,
  type ComposioConnection,
} from "../../../api/modules/connectors";
import { apiErrorMessage } from "../../../utils/apiError";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { message } from "../../../utils/antdMessage";
import {
  isAuthPopupBlocked,
  navigateCurrentTab,
  navigateAuthWindow,
  tryOpenAuthPopup,
} from "./openAuthWindow";
import { oauthCallbackSupported } from "./oauthCallback";
import { notifyConnectorsChanged } from "./customMcpUtils";
import styles from "./index.module.less";

const COMPOSIO_KIND = "composio-connect";
const OAUTH_TIMEOUT_MS = 120_000;
const POPUP_POLL_MS = 500;

function safeAuthorizationUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (
      (url.protocol !== "https:" && url.protocol !== "http:") ||
      !url.hostname ||
      url.username ||
      url.password
    ) {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

function statusLabel(t: TFunction, value: string) {
  const known: Record<string, string> = {
    active: "connectors.composio.statusActive",
    disabled: "connectors.composio.statusDisabled",
    authenticated: "connectors.composio.statusAuthenticated",
    reauth_required: "connectors.composio.statusReauthRequired",
    reachable: "connectors.composio.statusReachable",
    unavailable: "connectors.composio.statusUnavailable",
    unknown: "connectors.composio.statusUnknown",
    enabled: "connectors.composio.statusEnabled",
  };
  return t(known[value] || "connectors.composio.statusUnknown", {
    defaultValue: value || "unknown",
  });
}

type OAuthFlow = {
  stateId: string;
  popup: Window | null;
  cleanup: () => void;
};

export function ComposioTab() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<ComposioConnection | null>(null);
  const [query, setQuery] = useState("");
  const [apps, setApps] = useState<ComposioAppSummary[]>([]);
  const [searching, setSearching] = useState(false);
  const [detail, setDetail] = useState<ComposioAppSummary | null>(null);
  const appRequestGeneration = useRef(0);
  const connectionRef = useRef<ComposioConnection | null>(null);
  const oauthFlow = useRef<OAuthFlow | null>(null);
  const oauthStartRequest = useRef<{
    popup: Window | null;
    cancelled: boolean;
  } | null>(null);

  const clearAppState = useCallback(() => {
    appRequestGeneration.current += 1;
    setApps([]);
    setDetail(null);
    setQuery("");
  }, []);

  const applyConnection = useCallback(
    (next: ComposioConnection | null) => {
      const previous = connectionRef.current;
      if (
        previous &&
        next &&
        (previous.instance_id !== next.instance_id ||
          previous.credential_gen !== next.credential_gen)
      ) {
        clearAppState();
      }
      if (previous && (!next || next.usage !== "enabled")) clearAppState();
      connectionRef.current = next;
      setConnection(next);
    },
    [clearAppState],
  );

  const cancelOAuthFlow = useCallback((flow: OAuthFlow, closePopup = true) => {
    flow.cleanup();
    if (oauthFlow.current === flow) oauthFlow.current = null;
    if (closePopup) {
      try {
        flow.popup?.close();
      } catch {
        // The popup may already have navigated or closed.
      }
    }
    void connectorsApi.oauthCancel(flow.stateId).catch(() => undefined);
  }, []);

  useEffect(() => {
    return () => {
      if (oauthStartRequest.current) {
        oauthStartRequest.current.cancelled = true;
        try {
          oauthStartRequest.current.popup?.close();
        } catch {
          // The popup may already have closed.
        }
        oauthStartRequest.current = null;
      }
      if (oauthFlow.current) cancelOAuthFlow(oauthFlow.current);
    };
  }, [cancelOAuthFlow]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await connectorsApi.composioGet();
      applyConnection(res.connected ? res.connection : null);
    } catch (err) {
      message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
    } finally {
      setLoading(false);
    }
  }, [applyConnection, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const startConnect = useCallback(async () => {
    if (!oauthCallbackSupported()) {
      message.error(t("connectors.composio.httpsRequired"));
      return;
    }
    if (oauthFlow.current) cancelOAuthFlow(oauthFlow.current);
    // Open synchronously in the user gesture so popup blockers do not lose the window.
    const popup = tryOpenAuthPopup(
      "",
      "octop-composio-oauth",
      "width=520,height=720",
    );
    const startRequest = { popup, cancelled: false };
    oauthStartRequest.current = startRequest;
    setBusy(true);
    let flow: OAuthFlow | null = null;
    try {
      const { authorize_url, state_id } = await connectorsApi.oauthStart(
        { type: "catalog", kind: COMPOSIO_KIND },
        "/connectors?tab=composio",
      );
      if (
        startRequest.cancelled ||
        oauthStartRequest.current !== startRequest
      ) {
        void connectorsApi.oauthCancel(state_id).catch(() => undefined);
        return;
      }
      oauthStartRequest.current = null;
      const safeUrl = safeAuthorizationUrl(authorize_url);
      if (!safeUrl) {
        void connectorsApi.oauthCancel(state_id).catch(() => undefined);
        throw new Error("Unsafe Composio authorization URL");
      }
      if (isAuthPopupBlocked(popup)) {
        navigateCurrentTab(safeUrl);
        return;
      }

      let settled = false;
      let timeout = 0;
      let poll = 0;
      let popupClosedAt: number | null = null;
      let claiming = false;
      const cleanup = () => {
        window.removeEventListener("message", onMessage);
        window.clearTimeout(timeout);
        window.clearInterval(poll);
      };
      const claimPending = async () => {
        if (settled || claiming) return;
        claiming = true;
        try {
          const pending = await connectorsApi.oauthPending(state_id);
          if (pending.applied && pending.kind === COMPOSIO_KIND) {
            settled = true;
            cleanup();
            if (oauthFlow.current === flow) oauthFlow.current = null;
            applyConnection(
              pending.connection ??
                (await connectorsApi.composioGet()).connection,
            );
            message.success(t("connectors.composio.connectSuccess"));
            clearAppState();
            notifyConnectorsChanged();
            setBusy(false);
            try {
              popup?.close();
            } catch {
              // Ignore a popup already closed by the callback.
            }
          }
        } catch {
          // The callback has not completed yet; bounded polling handles COOP popups.
        } finally {
          claiming = false;
        }
      };
      const onMessage = (event: MessageEvent) => {
        if (event.origin !== window.location.origin || event.source !== popup)
          return;
        const data = event.data as { type?: string; state_id?: string } | null;
        if (
          data?.type !== "octop:connector-oauth" ||
          data.state_id !== state_id
        )
          return;
        void claimPending();
      };
      flow = {
        stateId: state_id,
        popup,
        cleanup: () => {
          settled = true;
          cleanup();
        },
      };
      oauthFlow.current = flow;
      window.addEventListener("message", onMessage);
      timeout = window.setTimeout(() => {
        if (flow) cancelOAuthFlow(flow);
        setBusy(false);
        message.error(t("connectors.oauthTimedOut"));
      }, OAUTH_TIMEOUT_MS);
      poll = window.setInterval(() => {
        void claimPending();
        if (!popup || !popup.closed) return;
        popupClosedAt ??= Date.now();
        if (Date.now() - popupClosedAt >= 3_000 && flow) {
          cancelOAuthFlow(flow, false);
          setBusy(false);
          message.info(t("connectors.composio.oauthCancelled"));
        }
      }, POPUP_POLL_MS);
      navigateAuthWindow(popup, safeUrl);
    } catch (err) {
      if (
        startRequest.cancelled ||
        oauthStartRequest.current !== startRequest
      ) {
        return;
      }
      oauthStartRequest.current = null;
      if (flow) cancelOAuthFlow(flow);
      else popup?.close();
      setBusy(false);
      message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
    }
  }, [applyConnection, cancelOAuthFlow, clearAppState, t]);

  const runCheck = useCallback(async () => {
    setBusy(true);
    try {
      const res = await connectorsApi.composioCheck();
      applyConnection(res.connection);
    } catch (err) {
      message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
    } finally {
      setBusy(false);
    }
  }, [applyConnection, t]);

  const runDisable = useCallback(() => {
    Modal.confirm({
      title: t("connectors.composio.disableConfirmTitle"),
      content: t("connectors.composio.disableConfirmBody"),
      okText: t("connectors.composio.disable"),
      cancelText: t("common.cancel"),
      onOk: async () => {
        setBusy(true);
        try {
          const res = await connectorsApi.composioDisable();
          applyConnection(res.connection);
          clearAppState();
          notifyConnectorsChanged();
        } catch (err) {
          message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
          throw err;
        } finally {
          setBusy(false);
        }
      },
    });
  }, [applyConnection, clearAppState, t]);

  const runEnable = useCallback(async () => {
    setBusy(true);
    try {
      const res = await connectorsApi.composioEnable();
      applyConnection(res.connection);
      clearAppState();
      notifyConnectorsChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
    } finally {
      setBusy(false);
    }
  }, [applyConnection, clearAppState, t]);

  const runDisconnect = useCallback(() => {
    Modal.confirm({
      title: t("connectors.composio.disconnectConfirmTitle"),
      content: t("connectors.composio.disconnectConfirmBody"),
      okText: t("connectors.composio.disconnect"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: async () => {
        setBusy(true);
        try {
          await connectorsApi.composioDisconnect();
          applyConnection(null);
          clearAppState();
          notifyConnectorsChanged();
          message.success(t("connectors.composio.disconnected"));
        } catch (err) {
          message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
          throw err;
        } finally {
          setBusy(false);
        }
      },
    });
  }, [applyConnection, clearAppState, t]);

  const runSearch = useCallback(async () => {
    if (!connection || connection.usage !== "enabled") return;
    const generation = appRequestGeneration.current;
    setSearching(true);
    try {
      const res = await connectorsApi.composioSearchApps(query.trim());
      if (generation !== appRequestGeneration.current) return;
      setApps(res.apps);
      if (res.apps.length === 0)
        message.info(t("connectors.composio.searchEmpty"));
    } catch (err) {
      if (generation === appRequestGeneration.current) {
        message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
      }
    } finally {
      setSearching(false);
    }
  }, [connection, query, t]);

  const openDetail = useCallback((app: ComposioAppSummary) => {
    setDetail(app);
  }, []);

  const authorizeApp = useCallback(async () => {
    if (!detail) return;
    // The user explicitly clicked; reserve a popup before waiting on the upstream call.
    const popup = tryOpenAuthPopup(
      "",
      "octop-composio-app-auth",
      "width=520,height=720",
    );
    setBusy(true);
    try {
      const response = await connectorsApi.composioAuthorizeApp(detail.toolkit);
      const result = response.result;
      const safeUrl = safeAuthorizationUrl(
        result && typeof result === "object" && "redirect_url" in result
          ? result.redirect_url
          : null,
      );
      if (!safeUrl) {
        popup?.close();
        message.info(t("connectors.composio.appAuthorizationUnknown"));
        return;
      }
      navigateAuthWindow(popup, safeUrl);
      message.info(t("connectors.composio.authorizeStarted"));
    } catch (err) {
      popup?.close();
      message.error(apiErrorMessage(err, t("connectors.loadFailed"), t));
    } finally {
      setBusy(false);
    }
  }, [detail, t]);

  if (loading) {
    return (
      <div className={styles.loadingState}>
        <Spin />
      </div>
    );
  }

  const capabilities = connection?.capabilities ?? {};
  const canSearch = capabilities.search_apps === true;

  return (
    <div className={styles.composioTab}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("connectors.composio.cloudNotice")}
      />

      {!connection ? (
        <div className={styles.composioCard}>
          <Cloud size={28} />
          <h3>{t("connectors.composio.disconnectedTitle")}</h3>
          <p>{t("connectors.composio.disconnectedDesc")}</p>
          <Button
            type="primary"
            loading={busy}
            onClick={() => void startConnect()}
          >
            {t("connectors.composio.connectCta")}
          </Button>
        </div>
      ) : (
        <div className={styles.composioCard}>
          <div className={styles.composioCardHeader}>
            <div>
              <h3>
                {connection.display_name || t("connectors.composio.myComposio")}
              </h3>
              <div className={styles.composioMeta}>
                <Tag>{t("connectors.composio.source")}</Tag>
                <Tag>{statusLabel(t, connection.status)}</Tag>
                <Tag
                  color={connection.usage === "enabled" ? "green" : "default"}
                >
                  {t("connectors.composio.usageLabel")}:{" "}
                  {statusLabel(t, connection.usage)}
                </Tag>
                <Tag>
                  {t("connectors.composio.authLabel")}:{" "}
                  {statusLabel(t, connection.auth_status)}
                </Tag>
                <Tag>
                  {t("connectors.composio.healthLabel")}:{" "}
                  {statusLabel(t, connection.health)}
                </Tag>
              </div>
              {connection.account_email ? (
                <p>
                  {t("connectors.composio.accountEmail")}:{" "}
                  {connection.account_email}
                </p>
              ) : (
                <p>{t("connectors.composio.accountUnknown")}</p>
              )}
              {connection.last_checked_at ? (
                <p>
                  {t("connectors.composio.lastChecked")}:{" "}
                  {formatServerDateTime(connection.last_checked_at, timeZone)}
                </p>
              ) : null}
            </div>
            <div className={styles.composioActions}>
              <Button
                icon={<RefreshCw size={14} />}
                loading={busy}
                onClick={() => void runCheck()}
              >
                {t("connectors.composio.check")}
              </Button>
              {connection.usage === "enabled" ? (
                <Button loading={busy} onClick={runDisable}>
                  {t("connectors.composio.disable")}
                </Button>
              ) : (
                <Button loading={busy} onClick={() => void runEnable()}>
                  {t("connectors.composio.enable")}
                </Button>
              )}
              <Button
                danger
                icon={<Unplug size={14} />}
                loading={busy}
                onClick={runDisconnect}
              >
                {t("connectors.composio.disconnect")}
              </Button>
              {(connection.auth_status === "reauth_required" ||
                connection.health === "unavailable") && (
                <Button
                  type="primary"
                  loading={busy}
                  onClick={() => void startConnect()}
                >
                  {t("connectors.composio.reauth")}
                </Button>
              )}
            </div>
          </div>

          <div className={styles.composioSearch}>
            <h4>{t("connectors.composio.discoverApps")}</h4>
            <p>
              {t(
                canSearch
                  ? "connectors.composio.discoverHint"
                  : "connectors.composio.searchUnsupported",
              )}
            </p>
            {canSearch ? (
              <>
                <div className={styles.composioSearchRow}>
                  <Input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder={t("connectors.composio.searchPlaceholder")}
                    onPressEnter={() => void runSearch()}
                    disabled={connection.usage !== "enabled"}
                  />
                  <Button
                    type="primary"
                    icon={<Search size={14} />}
                    loading={searching}
                    disabled={connection.usage !== "enabled"}
                    onClick={() => void runSearch()}
                  >
                    {t("connectors.composio.searchButton")}
                  </Button>
                </div>
                <div className={styles.composioAppList}>
                  {apps.map((app) => (
                    <button
                      key={app.toolkit}
                      type="button"
                      className={styles.composioAppItem}
                      onClick={() => openDetail(app)}
                    >
                      <Link2 size={16} />
                      <span>
                        <strong>{app.name}</strong>
                        <small>{app.toolkit}</small>
                        <em>{app.description}</em>
                      </span>
                    </button>
                  ))}
                </div>
              </>
            ) : null}
          </div>
        </div>
      )}

      <Drawer
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail?.name}
        width={420}
      >
        {detail ? (
          <div className={styles.composioDetail}>
            <p>{detail.description}</p>
            <p>
              {t("connectors.composio.source")} · <code>{detail.toolkit}</code>
            </p>
            <p>
              {t("connectors.composio.appStatus")}:{" "}
              {t("connectors.composio.statusUnavailable")}
            </p>
            <p>{t("connectors.composio.appStatusUnavailable")}</p>
            <Button
              type="primary"
              loading={busy}
              onClick={() => void authorizeApp()}
            >
              {t("connectors.composio.connectApp")}
            </Button>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
