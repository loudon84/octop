import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PluginReadinessNotice } from "./PluginReadinessNotice";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { plugins?: string }) =>
      options?.plugins ? `${key}: ${options.plugins}` : key,
  }),
}));

describe("PluginReadinessNotice", () => {
  it("keeps unavailable requirements visible without plugin UI registrations", () => {
    render(
      <PluginReadinessNotice
        readiness={{
          status: "ready",
          requiredPlugins: { enterprise: "1" },
          unavailablePlugins: ["enterprise"],
        }}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "plugins.readiness.unavailable: enterprise",
    );
  });

  it("shows loading and retryable errors distinctly", () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <PluginReadinessNotice
        readiness={{ status: "loading" }}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "plugins.readiness.loading",
    );
    rerender(
      <PluginReadinessNotice
        readiness={{ status: "error", reason: "unavailable" }}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "plugins.readiness.error",
    );
  });
});
