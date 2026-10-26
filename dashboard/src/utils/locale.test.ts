import { afterEach, expect, it, vi } from "vitest";

const {
  ensureLocaleBundle,
  changeLanguage,
  localeState,
  storeUiLocale,
  syncDocumentLang,
} = vi.hoisted(() => ({
  ensureLocaleBundle: vi.fn(),
  changeLanguage: vi.fn(),
  localeState: { language: "en" },
  storeUiLocale: vi.fn(),
  syncDocumentLang: vi.fn(),
}));

vi.mock("../i18n", () => ({
  default: {
    get language() {
      return localeState.language;
    },
    changeLanguage,
  },
  ensureLocaleBundle,
}));

vi.mock("./localePrefs", () => ({
  normalizeUiLocale: (raw: string | null | undefined) =>
    raw?.toLowerCase().startsWith("zh") ? "zh" : "en",
  resolveInitialLocale: () => "en",
  storeUiLocale,
  syncDocumentLang,
  detectBrowserLocale: () => "en",
  readStoredUiLocale: () => null,
  UI_LOCALE_STORAGE_KEY: "octop:ui-locale",
}));

import { applyUserLocale } from "./locale";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => {
  ensureLocaleBundle.mockReset();
  changeLanguage.mockReset();
  storeUiLocale.mockReset();
  syncDocumentLang.mockReset();
  localeState.language = "en";
});

it("does not commit an older locale bundle after the caller lease changes", async () => {
  const oldBundle = deferred();
  const currentBundle = deferred();
  ensureLocaleBundle.mockImplementation((locale: string) =>
    locale === "en" ? oldBundle.promise : currentBundle.promise,
  );
  changeLanguage.mockImplementation(async (locale: string) => {
    localeState.language = locale;
  });
  let oldLeaseCurrent = true;

  const oldApply = applyUserLocale("en", {
    isCurrent: () => oldLeaseCurrent,
  });
  oldLeaseCurrent = false;
  const currentApply = applyUserLocale("zh", { isCurrent: () => true });

  currentBundle.resolve();
  await currentApply;
  oldBundle.resolve();
  await oldApply;

  expect(storeUiLocale.mock.calls).toEqual([["zh"]]);
  expect(changeLanguage.mock.calls).toEqual([["zh"]]);
  expect(syncDocumentLang.mock.calls).toEqual([["zh"]]);
});
