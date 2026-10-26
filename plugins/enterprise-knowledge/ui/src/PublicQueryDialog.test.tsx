import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

async function setupGlobals() {
  const react = await import("react");
  const jsx = await import("react/jsx-runtime");
  Object.assign(window, { __OCTOP_REACT__: react, __OCTOP_JSX__: jsx });
}

describe("PublicQueryDialog", () => {
  afterEach(() => cleanup());

  it("starts on Cancel, traps Tab, and cancels with Escape", async () => {
    await setupGlobals();
    const onCancel = vi.fn();
    const { PublicQueryDialog } = await import("./PublicQueryDialog");
    render(
      <PublicQueryDialog
        locale="en"
        error=""
        pending={false}
        onCancel={onCancel}
        onConfirm={vi.fn()}
      />,
    );

    const cancel = screen.getByRole("button", { name: "Cancel" });
    const confirm = screen.getByRole("button", {
      name: "Approve and continue",
    });
    expect(document.activeElement).toBe(cancel);

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "public" },
    });
    confirm.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByRole("textbox"));
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(confirm);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("submits exactly what the user entered and blocks while pending", async () => {
    await setupGlobals();
    const onConfirm = vi.fn();
    const { PublicQueryDialog } = await import("./PublicQueryDialog");
    const view = render(
      <PublicQueryDialog
        locale="en"
        error=""
        pending={false}
        onCancel={vi.fn()}
        onConfirm={onConfirm}
      />,
    );

    const query = "  exact PUBLIC words?  ";
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: query },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Approve and continue" }),
    );
    expect(onConfirm).toHaveBeenCalledWith(query);

    view.rerender(
      <PublicQueryDialog
        locale="en"
        error=""
        pending={true}
        onCancel={vi.fn()}
        onConfirm={onConfirm}
      />,
    );
    expect(
      (screen.getByRole("button", { name: "Approving…" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      query,
    );
  });
});
