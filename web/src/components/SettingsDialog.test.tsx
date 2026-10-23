/** Current account settings keep the bearer and explicit sign out. */

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SettingsDialog } from "./SettingsDialog";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => {
  useSessionStore.setState({ token: "current-account-bearer" });
});

afterEach(() => {
  cleanup();
});

it("keeps the current bearer and sign out without installation rotation", () => {
  const signOut = vi.fn();
  render(<SettingsDialog open onOpenChange={() => {}} onSignOut={signOut} />);
  const tab = screen.getByRole("tab", { name: /account/i });
  fireEvent.pointerDown(tab);
  fireEvent.mouseDown(tab);
  fireEvent.click(tab);
  expect(screen.queryByPlaceholderText(/New token/i)).toBeNull();
  expect(screen.getByText("current-account-bearer")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(signOut).toHaveBeenCalledOnce();
});
