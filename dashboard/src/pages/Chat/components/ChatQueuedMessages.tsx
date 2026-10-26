import {
  CornerDownLeft,
  ExternalLink,
  Paperclip,
  Pencil,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { QueuedChatItem } from "../hooks/useChatMessageQueue";
import styles from "../index.module.less";

interface ChatQueuedMessagesProps {
  items: QueuedChatItem[];
  displayTarget: { agentId: string; threadId: string | null };
  onRemove: (id: string) => void;
  onReclaim: (id: string) => void;
  /** Commit a local draft as a mid-turn steer. */
  onSteer?: (id: string) => void;
  onOpenTarget?: (target: { agentId: string; threadId: string }) => void;
}

function previewText(item: QueuedChatItem): string {
  return item.text.trim().replace(/\s+/g, " ");
}

export default function ChatQueuedMessages({
  items,
  displayTarget,
  onRemove,
  onReclaim,
  onSteer,
  onOpenTarget,
}: ChatQueuedMessagesProps) {
  const { t } = useTranslation();

  if (items.length === 0) return null;

  return (
    <div
      className={styles.queuedMessages}
      role="list"
      aria-label={t("chat.queue.label")}
    >
      {items.map((item, index) => {
        const text = previewText(item);
        const attachmentCount = item.attachments?.length ?? 0;
        const label =
          text ||
          (attachmentCount > 0
            ? t("chat.queue.attachmentsOnly", {
                count: attachmentCount,
              })
            : t("chat.queue.emptyItem"));
        const isAlias = Boolean(
          item.displayOrigin &&
            (displayTarget.agentId !== item.target.agentId ||
              displayTarget.threadId !== item.target.threadId),
        );
        const canSteer =
          Boolean(onSteer) && item.status === "queued" && !isAlias;
        const canEdit = item.status !== "sending" && !isAlias;

        return (
          <div
            key={item.id}
            className={styles.queuedMessageItem}
            role="listitem"
          >
            {items.length > 1 ? (
              <span className={styles.queuedMessageIndex}>{index + 1}</span>
            ) : null}
            <div className={styles.queuedMessageBody}>
              <span className={styles.queuedMessageText}>{label}</span>
              {isAlias ? (
                <span>
                  {t("chat.queue.openTarget", {
                    threadId: item.target.threadId,
                    defaultValue: `Queued for ${item.target.threadId}`,
                  })}
                </span>
              ) : null}
              {item.status !== "queued" ? (
                <span role="status" aria-live="polite">
                  {t(`chat.queue.${item.status}`)}
                </span>
              ) : null}
              {attachmentCount > 0 && text && (
                <span
                  className={styles.queuedMessageAttach}
                  title={t("chat.queue.attachmentCount", {
                    count: attachmentCount,
                  })}
                >
                  <Paperclip size={12} />
                  {attachmentCount}
                </span>
              )}
            </div>
            <div className={styles.queuedMessageActions}>
              {isAlias && onOpenTarget ? (
                <button
                  type="button"
                  className={styles.queuedMessageIconBtn}
                  onClick={() => onOpenTarget(item.target)}
                  title={t("chat.queue.openTarget", {
                    threadId: item.target.threadId,
                    defaultValue: `Open ${item.target.threadId}`,
                  })}
                  aria-label={t("chat.queue.openTarget", {
                    threadId: item.target.threadId,
                    defaultValue: `Open ${item.target.threadId}`,
                  })}
                >
                  <ExternalLink size={14} aria-hidden />
                </button>
              ) : null}
              {canSteer ? (
                <button
                  type="button"
                  className={styles.queuedMessageIconBtn}
                  onClick={() => onSteer?.(item.id)}
                  title={t("chat.steer.hint")}
                  aria-label={t("chat.steer.action", "Steer")}
                >
                  <CornerDownLeft size={14} aria-hidden />
                </button>
              ) : null}
              <button
                type="button"
                className={styles.queuedMessageIconBtn}
                onClick={() => onReclaim(item.id)}
                disabled={!canEdit}
                aria-label={t("chat.queue.reclaim")}
                title={t("chat.queue.reclaim")}
              >
                <Pencil size={14} />
              </button>
              <button
                type="button"
                className={styles.queuedMessageIconBtn}
                onClick={() => onRemove(item.id)}
                disabled={!canEdit}
                aria-label={t("chat.queue.remove")}
                title={t("chat.queue.remove")}
              >
                <X size={14} />
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
