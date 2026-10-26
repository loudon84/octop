import { Alert, Button, Spin } from "antd";
import { useTranslation } from "react-i18next";
import type { PluginReadinessState } from "./types";

export function PluginReadinessNotice(props: {
  readiness: PluginReadinessState;
  onRetry: () => void;
}) {
  const { readiness, onRetry } = props;
  const { t } = useTranslation();
  if (
    readiness.status === "ready" &&
    readiness.unavailablePlugins.length === 0
  ) {
    return null;
  }

  if (readiness.status === "loading") {
    return (
      <div role="status" aria-live="polite">
        <Spin size="small" /> {t("plugins.readiness.loading")}
      </div>
    );
  }

  if (readiness.status === "error") {
    return (
      <Alert
        role="alert"
        type="error"
        showIcon
        message={
          readiness.reason === "denied"
            ? t("plugins.readiness.denied")
            : t("plugins.readiness.error")
        }
        action={
          <Button size="small" onClick={onRetry}>
            {t("common.retry")}
          </Button>
        }
      />
    );
  }

  return (
    <Alert
      role="alert"
      type="warning"
      showIcon
      message={t("plugins.readiness.unavailable", {
        plugins: readiness.unavailablePlugins.join(", "),
      })}
      action={
        <Button size="small" onClick={onRetry}>
          {t("common.retry")}
        </Button>
      }
    />
  );
}
