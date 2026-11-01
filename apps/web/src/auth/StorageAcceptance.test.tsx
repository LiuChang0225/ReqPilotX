import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setSaasAccountIdentity, saasStorageKey } from "../../../../packages/workbench-ui/src/shared/api/saasSession";
import { entryCommandStore } from "../../../../packages/workbench-ui/src/shared/api/entryCommands";
import { useScopedUiState } from "../../../../packages/workbench-ui/src/shared/hooks/useScopedUiState";
import { sourceFeedbackDraftKey, useSourceFeedbackDraft } from "../../../../packages/workbench-ui/src/features/extraction/sourceItemDrafts";

afterEach(() => { cleanup(); localStorage.clear(); setSaasAccountIdentity(undefined); });

describe("independent private browser-storage account acceptance", () => {
  it("preserves established local keys while isolating guest and each account", () => {
    setSaasAccountIdentity(undefined); expect(saasStorageKey("existing-key")).toBe("existing-key");
    setSaasAccountIdentity("account-a"); const a = saasStorageKey("existing-key");
    setSaasAccountIdentity("account-b"); const b = saasStorageKey("existing-key");
    setSaasAccountIdentity(null); const guest = saasStorageKey("existing-key");
    expect(new Set(["existing-key", a, b, guest]).size).toBe(4);
  });

  it("does not show another account's retained project command or let a late receipt delete a newer account's command", () => {
    const store = entryCommandStore({ namespace: "synthetic-test", storage: localStorage });
    const payload = { name: "Private A project name", customer_name: "Synthetic" };
    setSaasAccountIdentity("account-a"); const a = store.acquire("project.create", "", payload).intent;
    setSaasAccountIdentity("account-b"); expect(store.pending()).toEqual([]);
    const b = store.acquire("project.create", "", payload).intent;
    expect(b.commandId).not.toBe(a.commandId);
    store.complete(a);
    expect(store.pending().map((intent) => intent.commandId)).toEqual([b.commandId]);
    setSaasAccountIdentity(null); expect(store.pending()).toEqual([]);
  });

  it("does not mount another account's in-memory editor draft at the same project deep link", () => {
    const key = "same-business-deep-link-" + crypto.randomUUID();
    function Editor() {
      const [text, setText] = useScopedUiState(key, "");
      return <input aria-label="draft" value={text} onChange={(event) => setText(event.target.value)} />;
    }
    setSaasAccountIdentity("account-a"); const a = render(<Editor />);
    fireEvent.change(screen.getByLabelText("draft"), { target: { value: "Unsent private A opinion" } });
    a.unmount();
    setSaasAccountIdentity("account-b"); const b = render(<Editor />);
    expect(screen.getByLabelText("draft")).toHaveValue(""); b.unmount();
    setSaasAccountIdentity("account-a"); render(<Editor />);
    expect(screen.getByLabelText("draft")).toHaveValue("Unsent private A opinion");
  });

  it("separates durable feedback and historical draft discovery across accounts", () => {
    const scope = crypto.randomUUID();
    function Editor() {
      const key = sourceFeedbackDraftKey(scope, "same-document", "same-source", "same-baseline");
      const draft = useSourceFeedbackDraft(key, 1);
      return <><input aria-label="feedback" value={draft.text} onChange={(event) => draft.update(event.target.value)} />
        <span data-testid="history">{draft.otherDrafts.map((entry) => entry.text).join("|")}</span></>;
    }
    setSaasAccountIdentity("account-a"); const a = render(<Editor />);
    fireEvent.change(screen.getByLabelText("feedback"), { target: { value: "Sensitive A-only draft" } }); a.unmount();
    setSaasAccountIdentity("account-b"); const b = render(<Editor />);
    expect(screen.getByLabelText("feedback")).toHaveValue("");
    expect(screen.getByTestId("history")).not.toHaveTextContent("Sensitive A-only draft"); b.unmount();
    setSaasAccountIdentity(null); render(<Editor />);
    expect(screen.getByLabelText("feedback")).toHaveValue("");
    expect(screen.getByTestId("history")).not.toHaveTextContent("Sensitive A-only draft");
  });
});
