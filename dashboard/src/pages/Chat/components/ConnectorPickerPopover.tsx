import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { Switch } from "antd";
import { Settings2 } from "lucide-react";
import SearchablePickerPanel, {
  pickerStyles,
} from "../../../components/ChatPicker/SearchablePickerPanel";
import { ConnectorLogo } from "../../Agent/Connectors/connectorDefs";
import styles from "../index.module.less";

export interface ChatConnectorOption {
  mcp_server_name: string;
  label: string;
  kind: string;
  default_open?: boolean;
}

interface ConnectorPickerPopoverProps {
  connectors: ChatConnectorOption[];
  selectedConnectors: string[];
  onConnectorsChange: (names: string[]) => void;
  onNavigateAway?: () => void;
}

export default function ConnectorPickerPopover({
  connectors,
  selectedConnectors,
  onConnectorsChange,
  onNavigateAway,
}: ConnectorPickerPopoverProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const filterFn = useCallback(
    (connector: ChatConnectorOption, query: string) =>
      connector.label.toLowerCase().includes(query) ||
      connector.mcp_server_name.toLowerCase().includes(query) ||
      connector.kind.toLowerCase().includes(query),
    [],
  );

  const hasComposio = connectors.some((c) => c.kind === "composio-connect");

  return (
    <SearchablePickerPanel
      items={connectors}
      filterFn={filterFn}
      searchPlaceholder={t("connectors.chatPickerSearch")}
      emptyMessage={t("connectors.chatEmptyHint")}
      width="narrow"
      footerIcon={<Settings2 size={15} aria-hidden />}
      footerLabel={t("connectors.manageConnectors")}
      onFooterClick={() => {
        onNavigateAway?.();
        navigate("/connectors");
      }}
      beforeFooter={
        hasComposio ? (
          <div
            className={pickerStyles.itemDesc}
            style={{ padding: "8px 12px" }}
          >
            {t("connectors.chatComposioScopeHint")}
          </div>
        ) : null
      }
      renderItem={(connector) => {
        const active = selectedConnectors.includes(connector.mcp_server_name);
        return (
          <div
            key={connector.mcp_server_name}
            className={`${styles.connectorPickerItem} ${
              active ? styles.connectorPickerItemActive : ""
            }`}
          >
            <span className={styles.connectorPickerAvatar}>
              <ConnectorLogo kind={connector.kind} size={18} />
            </span>
            <span className={pickerStyles.itemText}>
              <span className={pickerStyles.itemName}>{connector.label}</span>
              {connector.kind === "composio-connect" ? (
                <span className={pickerStyles.itemDesc}>
                  {t("connectors.composio.source", "来源：Composio")}
                </span>
              ) : null}
              {connector.default_open ? (
                <span className={pickerStyles.itemDesc}>
                  {t("connectors.defaultOpenLockedBadge", "默认打开")}
                </span>
              ) : null}
            </span>
            <Switch
              size="small"
              className={styles.connectorPickerSwitch}
              checked={active}
              onChange={(checked) => {
                const next = checked
                  ? [...selectedConnectors, connector.mcp_server_name]
                  : selectedConnectors.filter(
                      (n) => n !== connector.mcp_server_name,
                    );
                onConnectorsChange(next);
              }}
            />
          </div>
        );
      }}
    />
  );
}
