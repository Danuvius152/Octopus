import { act, cleanup, render, screen } from "@testing-library/react";
import katex from "katex";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { useSessionStore } from "../stores/sessionStore";
import { FileViewerDialog } from "./FileViewerDialog";
import { MessageBubble } from "./MessageBubble";

const originalTrust = Object.getOwnPropertyDescriptor(Object.prototype, "trust");

beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "math-test" }, true));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  if (originalTrust) Object.defineProperty(Object.prototype, "trust", originalTrust);
  else Reflect.deleteProperty(Object.prototype, "trust");
});

async function markdown(target: "chat" | "file", content: string) {
  if (target === "chat") {
    return render(<MessageBubble sessionId="math-session" message={{
      role: "assistant", type: "text", content,
    }} />).container;
  }
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
    String(input).includes("/files/meta")
      ? Response.json({ path: "math.md", kind: "markdown", mime_type: "text/markdown", size: content.length })
      : new Response(content, { headers: { "content-type": "text/markdown" } })));
  render(<FileViewerDialog />);
  act(() => useSessionStore.getState().openViewer("math-session", "math.md"));
  return screen.findByRole("article");
}

it.each(["chat", "file"] as const)("renders inline and display equations in %s", async (target) => {
  const root = await markdown(target, "Before $x^2$ after.\n\n$$\n\\frac{1}{2}\n$$");
  expect(root.textContent).toContain("Before");
  expect(root.querySelectorAll("math")).toHaveLength(2);
  expect(root.querySelector("math[display='block']")).not.toBeNull();
  expect(Array.from(root.querySelectorAll("annotation"), (node) => node.textContent)).toEqual([
    "x^2", String.raw`\frac{1}{2}`,
  ]);
});

it.each(["chat", "file"] as const)("rejects inherited trust in the actual %s Markdown pipeline", async (target) => {
  // Exercise rehype-katex's resolved dependency, not just the directly
  // imported package. A transitive 0.16.x copy would still emit a remote image.
  Object.defineProperty(Object.prototype, "trust", { value: true, configurable: true, writable: true });
  const root = await markdown(target, String.raw`$\includegraphics{https://untrusted.invalid/math-probe.svg}$`);
  expect(root.textContent).toContain("includegraphics");
  expect(root.querySelectorAll("img, mglyph, a")).toHaveLength(0);
});

it("ignores trust inherited from a renderer options object", () => {
  const html = katex.renderToString(String.raw`\href{https://untrusted.invalid/}{probe}`, Object.create({ trust: true }));
  const result = document.createElement("div");
  result.innerHTML = html;
  expect(result.querySelector("a")).toBeNull();
});
