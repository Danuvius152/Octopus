import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useSessionStore } from "../stores/sessionStore";
import { TranscriptIntegrityNotice } from "./TranscriptIntegrityNotice";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("exports original bytes with session-scoped authentication", async () => {
  useSessionStore.setState({ token: "owner-token" });
  const original = new Blob(['{"id":1,"seq":4}\n{"id":2,"seq":4}\n']);
  const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => original });
  const create = vi.fn().mockReturnValue("blob:history");
  const revoke = vi.fn();
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = create;
    static revokeObjectURL = revoke;
  });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  render(<TranscriptIntegrityNotice sessionId="damaged" />);
  expect(screen.getByRole("alert").textContent).toContain("original rows are preserved");
  fireEvent.click(screen.getByRole("button", { name: "Export all original rows" }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(fetch).toHaveBeenCalledWith("/api/sessions/damaged/transcript-export", {
    headers: { Authorization: "Bearer owner-token" },
  });
  expect(create).toHaveBeenCalledWith(original);
  expect(revoke).toHaveBeenCalledWith("blob:history");
});

it("retains the notice and offers a retry when export fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
  render(<TranscriptIntegrityNotice sessionId="damaged" />);
  fireEvent.click(screen.getByRole("button"));
  expect(await screen.findByText("Could not export this transcript. Try again.")).toBeTruthy();
  expect(screen.getByRole("button").hasAttribute("disabled")).toBe(false);
});
