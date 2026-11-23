/**
 * Renderer-dispatch tests for FileViewerDialog.
 *
 * The dialog hits two endpoints — /meta returns the `kind` (which
 * picks the renderer), then /files returns the bytes. We mock
 * window.fetch so each test can stub a specific kind without
 * needing a real backend. The relationship between extension and
 * kind is enforced server-side and covered by tests/test_file_viewer.py;
 * here we only verify the frontend acts on kind correctly.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";

import { FileViewerDialog } from "./FileViewerDialog";
import { useSessionStore } from "../stores/sessionStore";

interface MetaPayload {
  kind: "markdown" | "code" | "text" | "image" | "pdf";
  mime_type: string;
  size: number;
}

function mockFetch(meta: MetaPayload, body: string) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    // Keep the requested path in the response so CodeBody can infer
    // the language hint from the extension (e.g. main.py → python).
    const reqPath = new URL(url).searchParams.get("path") ?? "test";
    if (url.includes("/files/meta")) {
      return new Response(JSON.stringify({
        path: reqPath, canonical_path_id: "file-path", canonical_directory: { path: ".", path_id: "root-path" }, ...meta,
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(body, {
      status: 200,
      headers: { "content-type": meta.mime_type },
    });
  });
}

beforeEach(() => {
  useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: null });
});

afterEach(() => {
  cleanup();
  useSessionStore.setState({ viewer: null });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "pdfViewerEnabled");
});

describe("FileViewerDialog", () => {
  it("renders nothing while viewer state is null", () => {
    render(<FileViewerDialog />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders markdown body for kind=markdown", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "markdown", mime_type: "text/markdown; charset=utf-8", size: 22 },
        "# Hello\n\nWorld."
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.md"));

    // The H1 the markdown renderer produces
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Hello" })).toBeInTheDocument()
    );
  });

  it("loads Markdown and relative images from the canonical document in the source session", async () => {
    const fileFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/sessions/original/files/meta") {
        return Response.json({
          path: "docs/original.md", canonical_path_id: "original-path",
          canonical_directory: { path: "docs", path_id: "docs-path" }, kind: "markdown", size: 80,
        });
      }
      if (url.pathname === "/api/sessions/original/files" &&
        url.searchParams.get("path") === "docs/original.md" && url.searchParams.get("canonical_path_id") === "original-path") {
        return new Response([
          "# Original",
          "![Diagram](../assets/图%2520%23.png?token=author)",
          "[External](https://example.test/help?q=1#ref)",
          "[Protocol relative](//example.test/help)",
          "[Site root](/help)",
          "![Remote](//example.test/figure.png)",
          "![Site asset](/figure.png)",
        ].join("\n\n"));
      }
      return new Response("# Wrong file");
    });
    vi.stubGlobal("fetch", fileFetch);
    useSessionStore.setState({ activeSessionId: "different" });
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("original", "alias.md"));

    await screen.findByRole("heading", { name: "Original" });
    const diagram = new URL((await screen.findByRole("img", { name: "Diagram" })).getAttribute("src")!);
    expect(diagram.pathname).toBe("/api/sessions/original/files");
    expect(diagram.searchParams.get("path")).toBe("../assets/图%20#.png");
    expect(diagram.searchParams.get("source_directory")).toBe("docs");
    expect(diagram.searchParams.get("source_directory_id")).toBe("docs-path");
    expect(diagram.searchParams.getAll("token")).toEqual(["tok"]);
    expect(fileFetch).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ cache: "no-store" }));
    expect(screen.getByRole("link", { name: "External" })).toHaveAttribute("href", "https://example.test/help?q=1#ref");
    expect(screen.getByRole("link", { name: "Protocol relative" })).toHaveAttribute("href", "//example.test/help");
    expect(screen.getByRole("link", { name: "Site root" })).toHaveAttribute("href", "/help");
    expect(screen.getByRole("img", { name: "Remote" })).toHaveAttribute("src", "//example.test/figure.png");
    expect(screen.getByRole("img", { name: "Site asset" })).toHaveAttribute("src", "/figure.png");
  });

  it.each(["reopen", "close", "change account"] as const)(
    "retires a pending fragment when the reader chooses to %s",
    async (transition) => {
      let finishOld!: (response: Response) => void;
      const pending = new Promise<Response>((resolve) => { finishOld = resolve; });
      let targetReads = 0;
      vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input));
        const filename = url.searchParams.get("path")!.replace(/^\.\//, "");
        if (url.pathname.endsWith("/meta")) {
          return Response.json({
            path: filename, canonical_path_id: filename, canonical_directory: { path: ".", path_id: "root-path" },
            kind: "markdown", size: 60,
          });
        }
        if (filename === "start.md") return new Response("[Follow](next.md#details)");
        if (++targetReads === 1) return pending;
        return new Response("# New reading\n\n## Details");
      }));

      const scroll = vi.fn();
      const originalScroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView");
      Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: scroll });
      const { openViewer, closeViewer, setToken } = useSessionStore.getState();
      const savedToken = localStorage.getItem("octopus_token");
      try {
        render(<FileViewerDialog />);
        act(() => openViewer("s1", "start.md"));
        const follow = await screen.findByRole("link", { name: "Follow" });
        act(() => follow.click());
        await waitFor(() => expect(targetReads).toBe(1));

        act(() => {
          if (transition === "change account") setToken("other-token");
          if (transition === "close") closeViewer();
          else openViewer("s1", "next.md");
        });
        if (transition === "close") expect(screen.queryByRole("dialog")).toBeNull();
        else await screen.findByRole("heading", { name: "New reading" });

        await act(async () => finishOld(new Response("# Retired reading\n\n## Details")));
        expect(screen.queryByRole("heading", { name: "Retired reading" })).toBeNull();
        expect(scroll).not.toHaveBeenCalled();
      } finally {
        if (originalScroll) Object.defineProperty(Element.prototype, "scrollIntoView", originalScroll);
        else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
        if (savedToken === null) localStorage.removeItem("octopus_token");
        else localStorage.setItem("octopus_token", savedToken);
      }
    },
  );

  it("renders <img> for kind=image", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "image", mime_type: "image/png", size: 1024 },
        "ignored-binary"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "shot.png"));

    await waitFor(() => {
      const img = document.querySelector("img");
      expect(img).not.toBeNull();
      expect(img!.getAttribute("src")).toContain("/files?path=shot.png");
      expect(img!.getAttribute("src")).toContain("token=tok");
    });
  });

  it("requests explicit PDF preview and preserves the download URL", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "pdf", mime_type: "application/pdf", size: 8000 },
        "ignored"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.pdf"));

    await waitFor(() => {
      const iframe = document.querySelector("iframe");
      expect(iframe).not.toBeNull();
      expect(iframe!.getAttribute("src")).toContain("/files?path=doc.pdf");
      expect(new URL(iframe!.getAttribute("src")!).searchParams.get("preview")).toBe("true");
    });
    expect(screen.getByRole("link", { name: "Open in new tab" })).toHaveAttribute("href", document.querySelector("iframe")!.getAttribute("src"));
    const download = screen.getByRole("link", { name: "Download file" });
    expect(new URL(download.getAttribute("href")!).searchParams.has("preview")).toBe(false);
    expect(download).toHaveAttribute("download", "doc.pdf");
  });

  it("keeps explicit open and download when PDF rendering is unavailable", async () => {
    Object.defineProperty(navigator, "pdfViewerEnabled", { configurable: true, value: false });
    vi.stubGlobal("fetch", mockFetch({ kind: "pdf", mime_type: "application/pdf", size: 8000 }, "ignored"));
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.pdf"));
    await screen.findByText(/PDF preview is unavailable/);
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.getByRole("link", { name: "Open in new tab" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Download file" })).toBeVisible();
  });

  it("renders <pre><code> for kind=code", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "code", mime_type: "text/plain; charset=utf-8", size: 14 },
        "def main():\n    pass\n"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "main.py"));

    await waitFor(() => {
      const code = document.querySelector("pre code");
      expect(code).not.toBeNull();
      expect(code!.textContent).toContain("def main");
      expect(code!.className).toContain("language-python");
    });
  });

  it("renders plain <pre> for kind=text", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "text", mime_type: "text/plain; charset=utf-8", size: 5 },
        "logged"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "out.log"));

    await waitFor(() => {
      // text body is a bare <pre> with text directly inside (no <code>)
      const pres = document.querySelectorAll("pre");
      const match = Array.from(pres).find(
        (p) => p.querySelector("code") === null && p.textContent?.includes("logged")
      );
      expect(match).toBeDefined();
    });
  });

  it("shows an error when the meta endpoint returns 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "File not found: nope.md" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        })
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "nope.md"));

    await waitFor(() =>
      expect(screen.getByText(/Couldn't open file/i)).toBeInTheDocument()
    );
    expect(screen.getByText(/File not found/i)).toBeInTheDocument();
  });

  it("closes when the close button is clicked", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "text", mime_type: "text/plain; charset=utf-8", size: 1 },
        "x"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "x.txt"));

    await waitFor(() => expect(document.querySelector("pre")).not.toBeNull());
    const closeBtn = screen.getByRole("button", { name: /close/i });
    act(() => closeBtn.click());
    expect(useSessionStore.getState().viewer).toBeNull();
  });
});
