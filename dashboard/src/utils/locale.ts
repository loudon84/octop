import i18n, { ensureLocaleBundle } from "../i18n";
import {
  normalizeUiLocale,
  resolveInitialLocale,
  storeUiLocale,
  syncDocumentLang,
  type UiLocale,
} from "./localePrefs";

export type { UiLocale };

let applicationSequence = 0;
export {
  detectBrowserLocale,
  normalizeUiLocale,
  readStoredUiLocale,
  resolveInitialLocale,
  storeUiLocale,
  syncDocumentLang,
  UI_LOCALE_STORAGE_KEY,
} from "./localePrefs";

/** Apply server-stored locale to the dashboard i18n instance. */
export async function applyUserLocale(
  raw: string | null | undefined,
  options: { isCurrent?: () => boolean } = {},
): Promise<UiLocale> {
  const lang = normalizeUiLocale(raw);
  const sequence = ++applicationSequence;
  const isCurrent = () =>
    sequence === applicationSequence && (options.isCurrent?.() ?? true);
  await ensureLocaleBundle(lang);
  if (!isCurrent()) return lang;
  storeUiLocale(lang);
  if (i18n.language !== lang) {
    await i18n.changeLanguage(lang);
  }
  if (!isCurrent()) return lang;
  syncDocumentLang(lang);
  return lang;
}

/** Guest surfaces (login / post-logout): stored preference or browser locale. */
export async function applyGuestLocale(): Promise<UiLocale> {
  const lang = resolveInitialLocale();
  const sequence = ++applicationSequence;
  await ensureLocaleBundle(lang);
  if (sequence !== applicationSequence) return lang;
  storeUiLocale(lang);
  if (i18n.language !== lang) {
    await i18n.changeLanguage(lang);
  }
  if (sequence !== applicationSequence) return lang;
  syncDocumentLang(lang);
  return lang;
}
