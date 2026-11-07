import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ContextMeter } from "./ContextMeter";
import { contextState } from "../lib/sessionContext.fixture";

afterEach(cleanup);

it.each([[699, "30%", "neutral"], [700, "30%", "amber"], [899, "10%", "amber"], [900, "10%", "red"], [1200, "0%", "red"]])(
  "shows remaining context with the unrounded occupied threshold %s", (used, percent, tone) => {
    const state = contextState();
    state.sample!.used_tokens = Number(used);
    render(<ContextMeter state={state} />);
    const trigger = screen.getByRole("button", { name: `Context remaining: ${percent}` });
    expect(trigger).toHaveTextContent(String(percent));
    expect(trigger).toHaveAttribute("data-tone", tone);
  },
);

it("opens accessible read-only details and preserves raw over-window values", () => {
  const state = contextState();
  state.sample!.used_tokens = 1200;
  render(<ContextMeter state={state} />);
  const trigger = screen.getByRole("button", { name: "Context remaining: 0%" });
  fireEvent.click(trigger);
  expect(screen.getByRole("dialog", { name: "Context details" })).toBeInTheDocument();
  expect(screen.getByText("1,200 / 1,000 tokens")).toBeInTheDocument();
  expect(screen.getByText(/Latest native sample/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /compact/i })).not.toBeInTheDocument();
});

it.each(["unknown", "stale", "historical"])("does not present %s data as current remaining context", (kind) => {
  const state = contextState();
  if (kind === "stale") state.validity = "stale";
  render(<ContextMeter state={kind === "unknown" ? null : state} historical={kind === "historical"} />);
  expect(screen.getByRole("button")).toHaveTextContent("—");
});

it("describes unknown, stale, compacting and historical state before details are opened", () => {
  const { rerender } = render(<ContextMeter state={null} />);
  expect(screen.getByRole("button", { name: "Context remaining: —" }))
    .toHaveAccessibleDescription("Context remaining is unknown.");

  const state = contextState();
  state.validity = "stale";
  rerender(<ContextMeter state={state} />);
  expect(screen.getByRole("button", { name: "Context remaining: —" }))
    .toHaveAccessibleDescription("Awaiting a new native sample.");

  state.validity = "valid";
  rerender(<ContextMeter state={state} />);
  expect(screen.getByRole("button", { name: "Context remaining: 75%" }))
    .toHaveAccessibleDescription("75% context remaining.");

  state.activity = "compacting";
  rerender(<ContextMeter state={state} />);
  expect(screen.getByRole("button", { name: "Context remaining: 75%" }))
    .toHaveAccessibleDescription("Native compaction is in progress. Any displayed value is the last sample before compaction.");

  rerender(<ContextMeter state={state} historical />);
  expect(screen.getByRole("button", { name: "Context remaining: —" }))
    .toHaveAccessibleDescription("Historical sample from this archived conversation.");
});

it("keeps an unconfirmed committed sample historical until alignment is confirmed", () => {
  render(<ContextMeter state={contextState()} pending />);
  const trigger = screen.getByRole("button", { name: "Context remaining: —" });
  expect(trigger).toHaveTextContent("Awaiting update");
  expect(trigger).toHaveAccessibleDescription("Awaiting a new native sample.");
  fireEvent.click(trigger);
  expect(screen.getByText("Historical sample")).toBeInTheDocument();
  expect(screen.getByText("250 / 1,000 tokens")).toBeInTheDocument();
});
