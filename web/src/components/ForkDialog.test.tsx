/**
 * Renderer tests for the fork-confirm view (session-rewind.md §5.6.2).
 * Drives the presentational `ForkConfirmView` directly so we don't have to
 * mock the two-step fetch flow: it must render the three-class side-effect
 * summary and gate the single revert checkbox on the preflight result.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ForkConfirmView, ForkDialog, type ForkPreview } from "./ForkDialog";
import { sessions } from "../lib/sessionList";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.unstubAllGlobals(); });

function makePreview(overrides: Partial<ForkPreview> = {}): ForkPreview {
  return {
    rewind_to_msg_seq: 2,
    prefilled_prompt: "redo this",
    side_effect_summary: {
      file_edits: [
        { path: "server/auth.py", turns: 3 },
        { path: "server/db.py", turns: 1 },
      ],
      bg_tasks: [
        { task_id: "t1", command: "bun run test:e2e", description: null, status: "running" },
      ],
      other_tools: [
        { label: "Bash commands", count: 12 },
        { label: "github calls", count: 2 },
      ],
      counts: { total: 17, file_edits: 2, bg_tasks: 1 },
    },
    revert: { available: true, refused_reason: null },
    can_fork: true,
    ...overrides,
  };
}

function renderView(preview: ForkPreview, revertChecked = true) {
  return render(
    <ForkConfirmView
      parentName="Refactor auth"
      preview={preview}
      revertChecked={revertChecked}
      onRevertChange={() => {}}
      label=""
      onLabelChange={() => {}}
    />
  );
}

describe("ForkConfirmView", () => {
  it("renders the three-class side-effect summary", () => {
    renderView(makePreview());
    expect(screen.getByText("Files modified (2)")).toBeTruthy();
    expect(screen.getByText("server/auth.py")).toBeTruthy();
    expect(screen.getByText("Background tasks (1)")).toBeTruthy();
    expect(screen.getByText("bun run test:e2e")).toBeTruthy();
    expect(screen.getByText(/Other tool activity/)).toBeTruthy();
    expect(screen.getByText("12 Bash commands")).toBeTruthy();
  });

  it("enables the revert checkbox when the preflight allows it", () => {
    renderView(makePreview());
    const cb = screen.getByTestId("fork-revert-checkbox") as HTMLInputElement;
    expect(cb.disabled).toBe(false);
    expect(cb.checked).toBe(true);
  });

  it("disables the revert checkbox with the refusal reason when unavailable", () => {
    renderView(
      makePreview({
        revert: {
          available: false,
          refused_reason: "HEAD has moved since the fork-point",
        },
      }),
      false
    );
    const cb = screen.getByTestId("fork-revert-checkbox") as HTMLInputElement;
    expect(cb.disabled).toBe(true);
    // The reason is surfaced as a tooltip on the label and inline.
    const label = screen.getByTestId("fork-revert-label");
    expect(label.getAttribute("title")).toContain("HEAD has moved");
    expect(
      screen.getByTestId("fork-revert-reason").textContent
    ).toContain("HEAD has moved");
  });

  it("handles an empty file-edit list", () => {
    renderView(
      makePreview({
        side_effect_summary: {
          file_edits: [],
          bg_tasks: [],
          other_tools: [],
          counts: { total: 0, file_edits: 0, bg_tasks: 0 },
        },
      })
    );
    expect(screen.getByText("Files modified (0)")).toBeTruthy();
    expect(screen.getByText("None")).toBeTruthy();
  });
});

it.each(["current", "reconnect", "token", "account"])("handles a successful rewind in the %s scope", async (scope) => {
  useSessionStore.getState().setToken("fork-account");
  useSessionStore.setState({ dispatchGeneration: "service", activeSessionId: "parent", mainView: "chat" });
  const parent = { id: "parent", name: "Parent", archived: false, status: "idle" } as SessionInfo;
  const fork = { ...parent, id: "fork", forked_from_session_id: parent.id };
  const accountGeneration = useSessionStore.getState().accountGeneration;
  const first = { socket: {}, accountGeneration }, second = { socket: {}, accountGeneration };
  let finish!: (response: Response) => void;
  let saved = false;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>((resolve) => { finish = resolve; });
    if (url.includes("fork-preview")) return Response.json(makePreview());
    if (new URL(url).pathname.endsWith("/api/sessions")) return Response.json(saved ?
      url.includes("include_archived=true") ? [fork, { ...parent, archived: true }] : [fork] : [parent]);
    return Response.json({ ...parent, messages: [] });
  });
  vi.stubGlobal("fetch", fetcher);
  await act(async () => { sessions.connect(first); });
  const onClose = vi.fn(), onForked = vi.fn();
  render(<ForkDialog sessionId={parent.id} parentName={parent.name} initialSeq={2} onClose={onClose} onForked={onForked} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Rewind" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  const foreign = { ...parent, id: "other-account", name: "Other account" };
  await act(async () => {
    if (scope === "reconnect") { sessions.disconnect(first); sessions.connect(second); }
    if (scope === "token") useSessionStore.getState().setToken("another-token");
    if (scope === "account") useSessionStore.setState({ accountGeneration: accountGeneration + 1 });
    if (scope === "token" || scope === "account") useSessionStore.setState({ sessions: [foreign] });
  });
  const closesBeforeReceipt = onClose.mock.calls.length;
  await act(async () => { saved = true; finish(Response.json(fork)); });
  if (scope === "current" || scope === "reconnect") {
    await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([fork]));
    expect(useSessionStore.getState().archivedSessions).toEqual([{ ...parent, archived: true }]);
    expect(onClose).toHaveBeenCalledOnce();
  } else {
    expect(useSessionStore.getState().sessions).toEqual([foreign]);
    expect(onClose).toHaveBeenCalledTimes(closesBeforeReceipt);
  }
  expect(onForked).toHaveBeenCalledTimes(scope === "current" ? 1 : 0);
  expect(fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/api/sessions"))).toHaveLength(scope === "reconnect" ? 3 : 1);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  sessions.disconnect(second);
});
