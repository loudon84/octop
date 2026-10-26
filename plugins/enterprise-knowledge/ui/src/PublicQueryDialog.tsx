import * as React from "./host";
import { t } from "./i18n";

type Props = {
  locale: string;
  error: string;
  pending: boolean;
  onCancel: () => void;
  onConfirm: (publicQuery: string) => void;
};

export function PublicQueryDialog({
  locale,
  error,
  pending,
  onCancel,
  onConfirm,
}: Props) {
  const [publicQuery, setPublicQuery] = React.useState("");
  const dialogRef = React.useRef<HTMLDivElement | null>(null);
  const cancelRef = React.useRef<HTMLButtonElement | null>(null);

  React.useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pending) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          "button:not([disabled]), textarea:not([disabled])",
        ) ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel, pending]);

  return (
    <div
      className="ek-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !pending) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        className="ek-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ek-public-query-title"
        aria-describedby="ek-public-query-description"
      >
        <h3 id="ek-public-query-title">{t(locale, "publicQueryTitle")}</h3>
        <p id="ek-public-query-description">
          {t(locale, "publicQueryDescription")}
        </p>
        <label>
          {t(locale, "publicQueryLabel")}
          <textarea
            value={publicQuery}
            disabled={pending}
            onChange={(event) => setPublicQuery(event.target.value)}
          />
        </label>
        <p className="ek-muted">{t(locale, "publicQueryExact")}</p>
        {error && <p role="alert">{error}</p>}
        <div className="ek-dialog-actions">
          <button
            ref={cancelRef}
            type="button"
            disabled={pending}
            onClick={onCancel}
          >
            {t(locale, "cancel")}
          </button>
          <button
            type="button"
            disabled={pending || publicQuery.length === 0}
            onClick={() => onConfirm(publicQuery)}
          >
            {pending
              ? t(locale, "publicQuerySubmitting")
              : t(locale, "publicQueryConfirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
