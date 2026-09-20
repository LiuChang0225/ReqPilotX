import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { Dialog } from "./Dialog";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    Object.defineProperty(this, "open", {
      configurable: true,
      value: true,
      writable: true,
    });
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
  window.requestAnimationFrame = (callback) => {
    callback(0);
    return 1;
  };
});

afterEach(() => cleanup());

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开
      </button>
      <Dialog open={open} title="确认操作" onClose={() => setOpen(false)}>
        <button type="button" onClick={() => setOpen(false)}>
          关闭
        </button>
      </Dialog>
    </>
  );
}

function BusyHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开忙碌弹窗
      </button>
      <Dialog
        open={open}
        title="正在处理"
        busy
        onClose={() => setOpen(false)}
      >
        <p>正在保存</p>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("uses modal dialog semantics and returns focus after closing", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "打开" });

    await user.click(opener);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("open");

    await user.click(screen.getByRole("button", { name: "关闭" }));
    expect(dialog).not.toHaveAttribute("open");
    expect(opener).toHaveFocus();
  });

  it("closes on Escape while idle and returns focus to the opener", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "打开" });

    await user.click(opener);
    const dialog = screen.getByRole("dialog");
    fireEvent(dialog, new Event("cancel", { cancelable: true }));

    await waitFor(() => expect(dialog).not.toHaveAttribute("open"));
    expect(opener).toHaveFocus();
  });

  it("cannot be dismissed by Escape or backdrop while busy", async () => {
    const user = userEvent.setup();
    render(<BusyHarness />);

    await user.click(screen.getByRole("button", { name: "打开忙碌弹窗" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-busy", "true");

    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(dialog).toHaveAttribute("open");

    fireEvent.click(dialog);
    expect(dialog).toHaveAttribute("open");
  });
});
