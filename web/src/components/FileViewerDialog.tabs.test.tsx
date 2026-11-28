import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
import { FileViewerDialog } from "./FileViewerDialog";

const root = "/workspace";

const pdfRenderer = vi.hoisted(() => ({
  openPdf: vi.fn(() => ({ promise: Promise.resolve({ numPages: 1 }), destroy: async () => {} })),
  paintPdfPage: vi.fn(() => ({ promise: Promise.resolve(), cancel: () => {} })),
  viewerStyles: "",
}));
vi.mock("./pdfRenderer", () => pdfRenderer);

function metadata(path: string, source = root, absolutePath = `${source}/${path}`, id = absolutePath,
  directory = { path: ".", path_id: source }, kind = "markdown") {
  return Response.json({
    path, absolute_path: absolutePath, source, canonical_path_id: id,
    canonical_directory: directory, kind, size: 120,
  });
}

function documentRead(documents: Record<string, string>) {
  const bodies = new Map<string, Array<Promise<Response>>>();
  const aliases = new Map<string, Promise<Response> | ((url: URL) => Response)>();
  const read = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input));
    const path = url.searchParams.get("path") ?? "";
    const source = url.searchParams.get("source") ?? root;
    if (url.pathname.endsWith("/sources")) return Response.json({ cwd: root, roots: [root, `${root}/shared`] });
    if (url.pathname.endsWith("/browse")) return Response.json({
      entries: ["a.md", "picked.md"].map((name) => ({ path: name, name, kind: "file" })), skipped: false, truncated: false,
    });
    if (url.pathname.endsWith("/meta")) {
      const result = aliases.get(path);
      return typeof result === "function" ? result(url) : result ?? metadata(path, source);
    }
    return bodies.get(path)?.shift() ?? new Response(documents[path] ?? `# ${path} body`);
  });
  vi.stubGlobal("fetch", read);
  const fileRequests = (path: string) => read.mock.calls.filter(([input]) =>
    new URL(String(input)).searchParams.get("path") === path);
  return {
    fileRequests,
    meta(path: string, result: Promise<Response> | ((url: URL) => Response)) { aliases.set(path, result); },
    body(path: string) {
      const response = deferred<Response>();
      bodies.set(path, [...(bodies.get(path) ?? []), response.promise]);
      return response;
    },
  };
}

const openLink = async (name: string) => {
  const label = `Open file: ${name}`;
  fireEvent.click(await screen.findByRole("button", { name: (value) => value === label || value.startsWith(`${label} at line `) }));
};
const tab = (name: string) => screen.getByRole("tab", { name });
const selected = () => screen.getByRole("tab", { selected: true });
const closeTab = (name: string) => fireEvent.click(screen.getByRole("button", { name: `Close ${name}` }));
const cancel = (name: string) => fireEvent.click(screen.getByRole("button", { name: `Cancel opening ${name}` }));

function openReader(path = "a.md", position?: { kind: "heading"; slug: string }) {
  useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: "foreground", mainView: "chat" });
  render(<FileViewerDialog />);
  act(() => useSessionStore.getState().openViewer("original", path, { position }));
}

function imageRead(documents: Record<string, string>) {
  const files = documentRead(documents);
  files.meta("diagram.png", () => metadata("diagram.png", root, undefined, undefined, undefined, "image"));
  let nextUrl = 0;
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => `blob:diagram-${++nextUrl}`);
    static revokeObjectURL = revoke;
  });
  return {
    ...files,
    revoke,
    imageBody() {
      const bytes = files.body("diagram.png");
      return () => act(async () => bytes.resolve(new Response(new Blob(["image"], { type: "image/png" }))));
    },
  };
}

async function startImageDecode(image: HTMLElement) {
  const decoded = deferred<void>();
  const decode = vi.fn(() => decoded.promise);
  Object.defineProperty(image, "decode", { value: decode });
  await act(async () => fireEvent.load(image));
  expect(decode).toHaveBeenCalledOnce();
  return decoded;
}

afterEach(() => {
  cleanup();
  useSessionStore.setState({ viewer: null });
  vi.unstubAllGlobals();
  pdfRenderer.openPdf.mockClear();
  pdfRenderer.paintPdfPage.mockClear();
});

describe("text file tabs", () => {
  it("keeps initial recovery when the picker reopens a file whose first read fails", async () => {
    const files = documentRead({ "a.md": "# Recovered document" });
    const initial = files.body("a.md");
    openReader();
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(await screen.findByRole("button", { name: "a.md" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel confirmation" })).not.toBeInTheDocument());
    await act(async () => initial.resolve(new Response(null, { status: 403 })));
    expect(selected()).toBe(tab("a.md"));
    expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
    expect(screen.getByRole("button", { name: "Close" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(await screen.findByRole("heading", { name: "Recovered document" })).toBeVisible();
  });

  it.each(["Retry", "Edit path", "Edit path with a prior draft"] as const)(
    "uses the latest picker selection on %s after the first alias is repointed", async (recovery) => {
      const files = documentRead({ "picked.md": "# Last selected document", "other.md": "# Repointed alias" });
      files.meta("alias.md", Promise.resolve(metadata("picked.md")));
      const initial = files.body("picked.md");
      openReader("alias.md");
      await waitFor(() => expect(files.fileRequests("picked.md")).toHaveLength(1));
      if (recovery === "Edit path with a prior draft") {
        fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
        expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("/workspace/picked.md");
      }
      fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
      fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
      await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel confirmation" })).not.toBeInTheDocument());
      files.meta("alias.md", Promise.resolve(metadata("other.md")));
      await act(async () => {
        if (recovery === "Retry") initial.reject(new TypeError("Network disconnected"));
        else initial.resolve(new Response(null, { status: 403 }));
      });
      fireEvent.click(screen.getByRole("button", { name: recovery === "Retry" ? "Retry" : "Edit path" }));
      if (recovery !== "Retry") {
        expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("picked.md");
        fireEvent.click(screen.getByRole("button", { name: "Open file" }));
      }
      expect(await screen.findByRole("heading", { name: "Last selected document" })).toBeVisible();
      expect(files.fileRequests("alias.md")).toHaveLength(1);
      const retried = files.fileRequests("picked.md").find(([input]) => new URL(String(input)).pathname.endsWith("/meta"));
      expect(retried).toBeDefined();
      expect(new URL(String(retried![0])).searchParams.get("source")).toBe(root);
    },
  );

  it.each([true, false])("clears the older alias error when the newer alias merges into its origin (older fails first: %s)", async (olderFirst) => {
    const files = documentRead({ "a.md": "# Original document\n\n[Read alias](alias.md)" });
    const older = deferred<Response>();
    const newer = deferred<Response>();
    files.meta("alias.md", older.promise);
    openReader();
    await openLink("Read alias");
    await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(1));
    files.meta("alias.md", newer.promise);
    await openLink("Read alias");
    await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(2));
    const failOlder = () => older.resolve(new Response(null, { status: 404 }));
    if (olderFirst) await act(async () => failOlder());
    await act(async () => newer.resolve(metadata("a.md")));
    if (!olderFirst) {
      expect(screen.getByRole("button", { name: "Cancel opening alias.md" })).toBeVisible();
      await act(async () => failOlder());
    }
    expect(selected()).toBe(tab("a.md"));
    expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cancel opening alias.md" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([true, false])("settles the older link failure after a source-bound retry (older fails first: %s)", async (olderFirst) => {
    const source = "/shared/docs";
    const alias = `${source}/alias.md`;
    const files = documentRead({ "a.md": `# Original document\n\n[Read alias](${alias})`, "plan.md": "# Current plan" });
    const older = deferred<Response>();
    const stalled = files.body("plan.md");
    files.meta(alias, older.promise);
    openReader();
    await openLink("Read alias");
    await waitFor(() => expect(files.fileRequests(alias)).toHaveLength(1));
    expect(new URL(String(files.fileRequests(alias)[0][0])).searchParams.get("source")).toBeNull();
    files.meta(alias, (url) => {
      const requestedSource = url.searchParams.get("source");
      return requestedSource && requestedSource !== source ? new Response(null, { status: 403 }) : metadata("plan.md", source);
    });

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Read alias" })));
      await act(async () => vi.advanceTimersByTime(5000));
      const failOlder = () => older.resolve(new Response(null, { status: 404 }));
      if (olderFirst) {
        await act(async () => failOlder());
        expect(screen.getByRole("alert")).toHaveTextContent("File not found.");
      }
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening plan.md" })));
      const retried = files.fileRequests(alias)[2];
      expect(new URL(String(retried[0])).searchParams.get("source")).toBe(source);
      expect(selected()).toBe(tab("plan.md"));
      if (!olderFirst) await act(async () => failOlder());
      await act(async () => stalled.resolve(new Response("# Retired body")));
      expect(screen.getByRole("heading", { name: "Current plan" })).toBeVisible();
      fireEvent.click(tab("a.md"));
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the failed read's source when editing only the position", async () => {
    const source = "/shared/docs";
    const alias = `${source}/alias.md`;
    const files = documentRead({ "a.md": `# Original document\n\n[Read alias](${alias})` });
    const stalled = files.body("plan.md");
    files.meta(alias, () => metadata("plan.md", source));
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read alias" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(link));
      expect(files.fileRequests("plan.md")).toHaveLength(1);
      files.meta(alias, (url) => url.searchParams.get("source") === source
        ? new Response(null, { status: 403 }) : metadata("docs/plan.md", "/shared"));
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening plan.md" })));
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      fireEvent.click(screen.getByRole("button", { name: `Edit path for ${alias}` }));
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(alias);
      fireEvent.change(screen.getByRole("textbox", { name: "Position (optional)" }), { target: { value: "#L3" } });
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
      expect(new URL(String(files.fileRequests(alias)[2][0])).searchParams.get("source")).toBe(source);
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      await act(async () => stalled.resolve(new Response("# Retired body")));
    } finally {
      vi.useRealTimers();
    }
  });

  it("honors a cleared position when a failed link is edited and reuses an open tab", async () => {
    const files = documentRead({
      "a.md": "# Guide\n\n[Direct link](b.md)\n\n[Alias at line three](alias.md#L3)\n\n[Another link](another.md)",
      "b.md": "# Report\n\nThe third line\n\nThe fifth line\n\n[Jump to five](#L5)",
    });
    files.meta("b.md", Promise.resolve(new Response(null, { status: 404 })));
    files.meta("alias.md", () => metadata("b.md"));
    openReader();
    await openLink("Direct link");
    await screen.findByRole("alert");
    await openLink("Alias at line three");
    expect(await screen.findByText("The third line")).toHaveAttribute("data-located", "true");
    fireEvent.click(tab("a.md"));
    files.meta("b.md", () => metadata("b.md"));
    fireEvent.click(screen.getByRole("button", { name: "Edit path for b.md" }));
    expect(screen.getByRole("textbox", { name: "Position (optional)" })).toHaveValue("");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: "Jump to five" }));
    expect(screen.getByText("The fifth line")).toHaveAttribute("data-located", "true");
    fireEvent.click(tab("a.md"));
    files.meta("another.md", Promise.resolve(new Response(null, { status: 404 })));
    await openLink("Another link");
    await screen.findByRole("alert");
    files.meta("another.md", () => metadata("b.md"));
    fireEvent.click(screen.getByRole("button", { name: "Edit path for another.md" }));
    expect(screen.getByRole("textbox", { name: "Position (optional)" })).toHaveValue("");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    expect(files.fileRequests("b.md").filter(([input]) => new URL(String(input)).pathname.endsWith("/files"))).toHaveLength(1);
  });

  it("cannot retry a settled alias after the other origin of a shared read closes", async () => {
    const files = documentRead({
      "a.md": "# A\n\n[Open P](p.md)\n\n[Open C](c.md)",
      "c.md": "# C\n\n[Open Q](q.md)",
      "u.md": "# Current P",
    });
    const stalled = files.body("t.md");
    files.meta("p.md", () => metadata("t.md"));
    files.meta("q.md", () => metadata("t.md"));
    openReader();
    await openLink("Open C");
    await screen.findByRole("heading", { name: "C" });
    fireEvent.click(tab("a.md"));

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open P" })));
      fireEvent.click(tab("c.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open Q" })));
      expect(files.fileRequests("t.md")).toHaveLength(1);
      fireEvent.click(tab("a.md"));
      files.meta("p.md", () => metadata("u.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open P" })));
      expect(screen.getByRole("heading", { name: "Current P" })).toBeVisible();
      closeTab("c.md");
      await act(async () => vi.advanceTimersByTime(5000));
      expect(screen.queryByRole("button", { name: "Retry opening t.md" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Cancel opening t.md" })).toBeVisible();
      await act(async () => stalled.resolve(new Response(null, { status: 404 })));
      fireEvent.click(tab("a.md"));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the confirmed source in the initial recovery pane after a shared read is denied", async () => {
    const source = "/shared/docs";
    const initial = `${source}/plan.md`;
    const alias = `${source}/alias.md`;
    const files = documentRead({ "picked.md": `# Guide\n\n[Read alias](${alias})` });
    const stalled = files.body("plan.md");
    files.meta(initial, () => metadata("plan.md", source));
    files.meta(alias, () => metadata("plan.md", source));
    openReader(initial);
    await waitFor(() => expect(files.fileRequests("plan.md")).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
    await screen.findByRole("heading", { name: "Guide" });
    await openLink("Read alias");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel opening alias.md" })).not.toBeInTheDocument());
    files.meta(alias, (url) => url.searchParams.get("source") === source
      ? new Response(null, { status: 403 }) : metadata("docs/plan.md", "/shared"));
    await act(async () => stalled.resolve(new Response(null, { status: 403 })));
    fireEvent.click(tab("plan.md"));
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(alias);
    fireEvent.change(screen.getByRole("textbox", { name: "Position (optional)" }), { target: { value: "#L3" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
    expect(new URL(String(files.fileRequests(alias)[1][0])).searchParams.get("source")).toBe(source);
    expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
  });

  it.each(["origin remains open", "origin closes"])("routes a failed retry of the shared initial read when its alias %s", async (origin) => {
    const files = documentRead({ "picked.md": "# Guide\n\n[Read alias](alias.md)" });
    const initial = files.body("a.md");
    files.meta("alias.md", () => metadata("a.md"));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => openReader());
      expect(files.fileRequests("a.md")).toHaveLength(2);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file picker" })));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "picked.md" })));
      expect(selected()).toBe(tab("picked.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Read alias" })));
      expect(files.fileRequests("a.md")).toHaveLength(2);
      files.meta("alias.md", Promise.resolve(new Response(null, { status: 404 })));
      files.meta("a.md", Promise.resolve(new Response(null, { status: 404 })));
      if (origin === "origin closes") closeTab("picked.md");
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening a.md" })));
      if (origin === "origin remains open") {
        expect(selected()).toBe(tab("picked.md"));
        expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open alias.md. File not found.");
        expect(screen.getByRole("button", { name: "Retry opening alias.md" })).toBeVisible();
      } else {
        expect(files.fileRequests("a.md")).toHaveLength(3);
        expect(selected()).toBe(tab("a.md"));
        expect(screen.getByRole("alert")).toHaveTextContent("File not found.");
        expect(screen.getByRole("button", { name: "Reload file" })).toBeVisible();
        expect(screen.getByRole("button", { name: "Edit path" })).toBeVisible();
      }
      await act(async () => initial.resolve(new Response("# Retired initial read")));
      expect(screen.queryByText("Retired initial read")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["join before readiness", "join after readiness", "join before failure"] as const)(
    "settles both aliases when they share one read: %s", async (ordering) => {
      const files = documentRead({ "a.md": "# Guide\n\n[Open P](p.md)\n\n[Open Q](q.md)" });
      const firstP = deferred<Response>();
      const secondP = deferred<Response>();
      const b = files.body("b.md");
      files.meta("p.md", firstP.promise);
      files.meta("q.md", Promise.resolve(metadata("b.md")));
      openReader();
      await openLink("Open P");
      await waitFor(() => expect(files.fileRequests("p.md")).toHaveLength(1));
      files.meta("p.md", secondP.promise);
      await openLink("Open P");
      await waitFor(() => expect(files.fileRequests("p.md")).toHaveLength(2));
      await openLink("Open Q");
      await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(1));

      const join = () => secondP.resolve(metadata("b.md"));
      if (ordering !== "join after readiness") await act(async () => join());
      const failed = ordering === "join before failure";
      await act(async () => b.resolve(failed ? new Response(null, { status: 403 }) : new Response("# Both aliases")));
      if (failed) expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't open q.md.");
      else expect(await screen.findByRole("heading", { name: "Both aliases" })).toBeVisible();
      if (ordering === "join after readiness") await act(async () => join());
      await act(async () => firstP.resolve(new Response(null, { status: 404 })));
      fireEvent.click(tab("a.md"));
      if (failed) expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open q.md.");
      else expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(files.fileRequests("b.md")).toHaveLength(1);
    },
  );

  it("reports a shared read failure on both originating tabs", async () => {
    const files = documentRead({
      "a.md": "# Guide A\n\n[Open guide B](b.md)\n\n[Open from A](p.md)",
      "b.md": "# Guide B\n\n[Open from B](q.md)",
    });
    const shared = files.body("shared.md");
    files.meta("p.md", () => metadata("shared.md"));
    files.meta("q.md", () => metadata("shared.md"));
    openReader();
    await openLink("Open guide B");
    await screen.findByRole("heading", { name: "Guide B" });
    fireEvent.click(tab("a.md"));
    await openLink("Open from A");
    await waitFor(() => expect(files.fileRequests("shared.md")).toHaveLength(1));
    fireEvent.click(tab("b.md"));
    await openLink("Open from B");
    await waitFor(() => expect(files.fileRequests("q.md")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel opening q.md" })).not.toBeInTheDocument());

    await act(async () => shared.resolve(new Response(null, { status: 403 })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Guide B" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open q.md.");
    expect(screen.getByRole("button", { name: "Retry opening q.md" })).toBeVisible();
    fireEvent.click(tab("a.md"));
    expect(screen.getByRole("heading", { name: "Guide A" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open p.md.");
    expect(screen.getByRole("button", { name: "Retry opening p.md" })).toBeVisible();
    expect(files.fileRequests("shared.md")).toHaveLength(1);
  });

  it.each([
    { slow: false, lateTarget: undefined, closed: false },
    { slow: true, lateTarget: undefined, closed: false },
    { slow: false, lateTarget: "initial.md", closed: false },
    { slow: false, lateTarget: "initial.md", closed: true },
    { slow: false, lateTarget: "initial.md", closed: true, picker: true },
    { slow: false, lateTarget: "redirected.md", closed: false },
  ])("settles a shared recovery after reopening its joined retry (slow: $slow, late: $lateTarget, closed: $closed, picker: $picker)", async ({ slow, lateTarget, closed, picker }) => {
    const left = lateTarget ? `${root}/initial.md` : "left.md";
    const files = documentRead({
      "a.md": `# Left guide\n\n[Open left](${left})\n\n[Start replacement](known.md)`,
      "picked.md": "# Right guide\n\n[Open right](right.md)",
      "initial.md": "# Recovered document",
    });
    const initial = files.body("initial.md");
    const replacement = files.body("initial.md");
    for (const path of [left, "right.md", "known.md"]) files.meta(path, () => metadata("initial.md"));
    openReader("initial.md");
    await waitFor(() => expect(files.fileRequests("initial.md")).toHaveLength(2));
    for (const [path, link] of [["a.md", "Open left"], ["picked.md", "Open right"]]) {
      fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
      fireEvent.click(await screen.findByRole("button", { name: path }));
      await openLink(link);
    }
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel opening right.md" })).not.toBeInTheDocument());
    await act(async () => initial.resolve(new Response(null, { status: 403 })));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open right.md.");
    fireEvent.click(tab("a.md"));
    if (slow) vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Start replacement" })));
      expect(files.fileRequests("initial.md")).toHaveLength(3);
      const late = deferred<Response>();
      if (lateTarget) files.meta(left, late.promise);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry opening ${left}` })));
      files.meta(left, () => metadata("initial.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open left" })));
      expect(files.fileRequests(left)).toHaveLength(3);
      if (slow) {
        await act(async () => vi.advanceTimersByTime(5000));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening initial.md" })));
        expect(files.fileRequests("initial.md")[2][1]?.signal?.aborted).toBe(true);
      }
      await act(async () => replacement.resolve(new Response(slow ? "# Retired read" : "# Recovered document")));
      expect(screen.getByRole("heading", { name: "Recovered document" })).toBeVisible();
      if (closed) {
        const slot = selected().closest<HTMLElement>("[data-file-slot]")!;
        fireEvent.click(within(slot).getByRole("button", { name: "Close initial.md" }));
      }
      const active = selected();
      if (picker) {
        fireEvent.click(tab("initial.md"));
        fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
        await screen.findByRole("button", { name: "a.md" });
      }
      if (lateTarget) await act(async () => late.resolve(metadata(lateTarget)));
      if (picker) {
        expect(screen.getByRole("heading", { name: "Choose a file" })).toHaveFocus();
        fireEvent.click(screen.getByRole("button", { name: "a.md" }));
        expect(await screen.findByRole("heading", { name: "Left guide" })).toBeVisible();
        expect(selected()).toBe(tab("a.md"));
      } else expect(selected()).toBe(active);
      const redirected = lateTarget === "redirected.md";
      expect(screen.queryAllByRole("tab", { name: "initial.md" })).toHaveLength(redirected ? 2 : closed ? 0 : 1);
      if (redirected) expect(tab("redirected.md")).toBeVisible();
      fireEvent.click(tab("picked.md"));
      if (redirected) expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open right.md.");
      else expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(files.fileRequests("initial.md")).toHaveLength(slow ? 4 : 3);
    } finally {
      if (slow) vi.useRealTimers();
    }
  });

  it.each([
    { olderConfirmsFirst: false, closed: false },
    { olderConfirmsFirst: false, closed: true },
    { olderConfirmsFirst: true, closed: false },
    { olderConfirmsFirst: true, closed: true },
  ])("keeps an independent file retryable (older confirms first: $olderConfirmsFirst, closed: $closed)", async ({ olderConfirmsFirst, closed }) => {
    const files = documentRead({ "a.md": "# Guide\n\n[Read alias](alias.md)" });
    const older = deferred<Response>();
    const body = files.body("c.md");
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read alias" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      files.meta("alias.md", older.promise);
      await act(async () => fireEvent.click(link));
      const confirmOlder = () => act(async () => older.resolve(metadata("c.md")));
      if (olderConfirmsFirst) await confirmOlder();
      files.meta("alias.md", () => metadata("b.md"));
      await act(async () => fireEvent.click(link));
      expect(selected()).toBe(tab("b.md"));
      if (closed) closeTab("b.md");
      if (!olderConfirmsFirst) await confirmOlder();
      expect(files.fileRequests("c.md")).toHaveLength(1);
      expect(selected()).toBe(tab(closed ? "a.md" : "b.md"));
      await act(async () => vi.advanceTimersByTime(5000));
      const slowRetry = within(screen.getByRole("tablist")).queryByRole("button", { name: "Retry opening c.md" });
      await act(async () => body.resolve(new Response(null, { status: 403 })));
      fireEvent.click(tab("a.md"));
      expect(slowRetry).not.toBeNull();
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      files.meta("alias.md", () => metadata("c.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening alias.md" })));
      expect(selected()).toBe(tab("c.md"));
      expect(screen.getByRole("heading", { name: "c.md body" })).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["retry", "denial"] as const)("keeps a repointed retry independent of an older recovery member (%s)", async (outcome) => {
    const files = documentRead({
      "a.md": "# Guide\n\n[Open earlier](alias.md#L3)\n\n[Open later](alias.md#L7)",
      "d.md": "# Current report\n\nEarlier line\n\nMiddle\n\nLatest line",
    });
    const firstBody = files.body("b.md");
    const currentBody = files.body("d.md");
    openReader();
    const earlier = await screen.findByRole("button", { name: "Open file: Open earlier at line 3" });
    const later = screen.getByRole("button", { name: "Open file: Open later at line 7" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      files.meta("alias.md", () => metadata("b.md"));
      await act(async () => fireEvent.click(earlier));
      await act(async () => firstBody.reject(new TypeError("Initial body disconnected")));
      files.body("c.md");
      files.meta("alias.md", () => metadata("c.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening alias.md" })));
      await act(async () => fireEvent.click(later));
      expect(files.fileRequests("c.md")).toHaveLength(1);
      const slowRetry = (path: string) => within(screen.getByRole("tablist")).getByRole("button", { name: `Retry opening ${path}` });
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta("alias.md", () => metadata("d.md"));
      await act(async () => fireEvent.click(slowRetry("c.md")));
      expect(files.fileRequests("c.md")[0][1]?.signal?.aborted).toBe(true);
      expect(files.fileRequests("d.md")).toHaveLength(1);

      files.meta("alias.md", () => metadata("e.md"));
      await act(async () => fireEvent.click(later));
      expect(selected()).toBe(tab("e.md"));
      if (outcome === "denial") {
        await act(async () => currentBody.resolve(new Response(null, { status: 403 })));
        expect(selected()).toBe(tab("e.md"));
        fireEvent.click(tab("a.md"));
        expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
        expect(screen.getByRole("button", { name: "Retry opening alias.md" })).toBeVisible();
      } else {
        await act(async () => vi.advanceTimersByTime(5000));
        files.meta("alias.md", () => metadata("d.md"));
        await act(async () => fireEvent.click(slowRetry("d.md")));
        expect(files.fileRequests("d.md")[0][1]?.signal?.aborted).toBe(true);
        expect(selected()).toBe(tab("d.md"));
        expect(screen.getByText("Latest line")).toHaveAttribute("data-located", "true");
        expect(screen.getByText("Earlier line")).not.toHaveAttribute("data-located");
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries a slow unconfirmed alias after a newer read of one target is closed", async () => {
    const files = documentRead({ "a.md": "# Guide\n\n[Read alias](alias.md)" });
    const oldest = deferred<Response>();
    const middle = deferred<Response>();
    const denied = files.body("b.md");
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read alias" });
    const open = () => act(async () => fireEvent.click(link));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      files.meta("alias.md", oldest.promise);
      await open();
      files.meta("alias.md", middle.promise);
      await open();
      files.meta("alias.md", () => metadata("b.md"));
      await open();
      await act(async () => denied.resolve(new Response(null, { status: 403 })));
      await act(async () => middle.resolve(metadata("b.md")));
      closeTab("b.md");
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta("alias.md", () => metadata("c.md"));
      const tablist = within(screen.getByRole("tablist", { name: "Open files" }));
      await act(async () => fireEvent.click(tablist.getByRole("button", { name: "Retry opening alias.md" })));
      expect(files.fileRequests("alias.md")).toHaveLength(4);
      expect(files.fileRequests("alias.md")[0][1]?.signal?.aborted).toBe(true);
      await act(async () => oldest.resolve(metadata("b.md")));
      expect(selected()).toBe(tab("c.md"));
      expect(screen.queryByRole("tab", { name: "b.md" })).not.toBeInTheDocument();
      expect(files.fileRequests("b.md")).toHaveLength(2);
      fireEvent.click(tab("a.md"));
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
    } finally {
      vi.useRealTimers();
    }
  });

  it("retires the redirected recovery when its last live origin closes", async () => {
    const files = documentRead({
      "a.md": "# Left guide\n\n[Open left](left.md)", "picked.md": "# Right guide\n\n[Open right](right.md)",
    });
    const initial = files.body("initial.md");
    const successful = files.body("initial.md");
    const redirected = files.body("redirected.md");
    files.meta("left.md", () => metadata("initial.md"));
    files.meta("right.md", () => metadata("initial.md"));
    openReader("initial.md");
    await waitFor(() => expect(files.fileRequests("initial.md")).toHaveLength(2));

    for (const [path, link] of [["a.md", "Open left"], ["picked.md", "Open right"]]) {
      fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
      fireEvent.click(await screen.findByRole("button", { name: path }));
      await openLink(link);
    }
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel opening right.md" })).not.toBeInTheDocument());
    await act(async () => initial.reject(new TypeError("Interrupted body")));
    fireEvent.click(tab("a.md"));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening left.md" })));
    await waitFor(() => expect(files.fileRequests("initial.md")).toHaveLength(3));
    fireEvent.click(tab("initial.md"));
    files.meta("right.md", () => metadata("redirected.md"));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
    expect(selected()).toBe(tab("redirected.md"));
    await act(async () => redirected.reject(new TypeError("Interrupted redirected body")));
    await act(async () => successful.resolve(new Response("# Recovered original")));
    expect(tab("initial.md")).toBeVisible();
    expect(selected()).toBe(tab("redirected.md"));

    screen.getByRole("button", { name: /^Retry$/ }).focus();
    closeTab("picked.md");
    expect(screen.queryByRole("tab", { name: "redirected.md" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Retry$/ })).not.toBeInTheDocument();
    expect(selected()).toBe(tab("a.md"));
    expect(selected()).toHaveFocus();
    expect(files.fileRequests("redirected.md")).toHaveLength(1);
  });

  it.each([
    { order: "older first", olderFile: "b.md", newerFile: "c.md" },
    { order: "newer first", olderFile: "b.md", newerFile: "c.md" },
    { order: "newer first", olderFile: "c.md", newerFile: "c.md" },
    { order: "newer first", olderFile: "b.md", newerFile: undefined },
  ])("settles shared alias failures with $order for $olderFile and $newerFile", async ({ order, olderFile, newerFile }) => {
    const files = documentRead({ "a.md": "# Guide\n\n[Open alias](alias.md)" });
    const olderMeta = deferred<Response>();
    const newerResponse = newerFile ? files.body(newerFile) : deferred<Response>();
    const olderBody = files.body(olderFile);
    files.meta("alias.md", olderMeta.promise);
    openReader();
    await openLink("Open alias");
    await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(1));
    files.meta("alias.md", newerFile ? () => metadata(newerFile) : newerResponse.promise);
    await openLink("Open alias");
    await waitFor(() => expect(files.fileRequests(newerFile ?? "alias.md")).toHaveLength(newerFile ? 1 : 2));
    const failOlder = async () => {
      await act(async () => olderMeta.resolve(metadata(olderFile)));
      expect(files.fileRequests(olderFile)).toHaveLength(olderFile === newerFile ? 2 : 1);
      await act(async () => olderBody.resolve(new Response(null, { status: 404 })));
    };
    const failNewer = () => act(async () => newerResponse.resolve(new Response(null, { status: 403 })));
    const [first, second] = order === "older first" ? [failOlder, failNewer] : [failNewer, failOlder];
    await first();
    await second();
    if (olderFile === newerFile) {
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      expect(screen.getByRole("button", { name: "Retry opening alias.md" })).toBeVisible();
    } else {
      expect(screen.getByRole("button", { name: `Retry opening ${root}/b.md` })).toBeVisible();
      const newerLabel = newerFile ? `${root}/${newerFile}` : "alias.md (target unconfirmed)";
      expect(screen.getByRole("button", { name: `Retry opening ${newerLabel}` })).toBeVisible();
    }
    expect(selected()).toBe(tab("a.md"));
    expect(screen.getByRole("heading", { name: "Guide" })).toBeVisible();
  });

  it.each([
    "redirected", "redirected slow", "redirected denied", "redirected after denial", "same file", "closed original", "denied",
    "two failed files retried", "two failed files and a cancelled open", "two failed files and a successful open",
    "two failed files at the same path", "merged retry after unconfirmed denial", "merged retry after redirected denial",
  ] as const)("settles a delayed shared retry confirmation: %s", async (outcome) => {
    const originalPath = `${root}/initial.md`;
    const files = documentRead({
      "picked.md": `# Guide\n\n[Open original](${originalPath})\n\n[Open again](known.md)`,
      "a.md": "# Other origin\n\n[Open again](known.md)",
      "redirected.md": "# Independently redirected",
    });
    const initial = files.body("initial.md");
    const current = files.body("initial.md");
    const delayed = deferred<Response>();
    const hasNewerDenial = outcome === "redirected after denial" || outcome.startsWith("two failed files");
    const mergedRetry = outcome === "merged retry after unconfirmed denial" || outcome === "merged retry after redirected denial";
    const replacedDirectory = outcome === "two failed files at the same path";
    const redirectedPath = replacedDirectory ? "initial.md" : "redirected.md";
    const redirected = outcome === "redirected slow" || outcome === "redirected denied" ||
      outcome === "merged retry after redirected denial" || hasNewerDenial
      ? files.body(redirectedPath) : undefined;
    files.meta(originalPath, () => metadata("initial.md"));
    files.meta("known.md", () => metadata("initial.md"));
    openReader("initial.md");
    await waitFor(() => expect(files.fileRequests("initial.md")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
    await openLink("Open original");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel confirmation" })).not.toBeInTheDocument());
    await act(async () => initial.reject(new TypeError("Interrupted body")));
    if (mergedRetry) {
      fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
      fireEvent.click(await screen.findByRole("button", { name: "a.md" }));
    }
    await openLink("Open again");
    await waitFor(() => expect(files.fileRequests("initial.md")).toHaveLength(3));
    if (mergedRetry) fireEvent.click(tab("picked.md"));

    files.meta(originalPath, delayed.promise);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry opening ${originalPath}` })));
    expect(screen.getByRole("button", { name: "Cancel confirmation" })).toBeVisible();
    if (mergedRetry) {
      await act(async () => delayed.resolve(metadata("initial.md")));
      const redirects = outcome === "merged retry after redirected denial";
      files.meta(originalPath, () => redirects ? metadata("redirected.md") : new Response(null, { status: 403 }));
      if (redirects) {
        fireEvent.click(tab("initial.md"));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
        await act(async () => redirected!.resolve(new Response(null, { status: 403 })));
        fireEvent.click(tab("picked.md"));
      } else {
        await act(async () => openLink("Open original"));
      }
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      await act(async () => current.resolve(new Response(null, { status: 404 })));
      const alerts = screen.getAllByRole("alert");
      expect(alerts).toHaveLength(2);
      expect(alerts.some((alert) => alert.textContent?.includes("don't have access"))).toBe(true);
      expect(alerts.some((alert) => alert.textContent?.includes("File not found."))).toBe(true);
      const labels = screen.getAllByRole("button", { name: /^Retry opening/ }).map((button) => button.textContent);
      expect(new Set(labels).size).toBe(2);
      if (!redirects) expect(screen.getByRole("button", { name: `Retry opening ${originalPath} (target unconfirmed)` })).toBeVisible();
      return;
    }
    files.meta(originalPath, () => hasNewerDenial ? new Response(null, { status: 403 }) : metadata("initial.md"));
    fireEvent.click(tab("initial.md"));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
    if (hasNewerDenial) {
      fireEvent.click(tab("picked.md"));
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      await act(async () => delayed.resolve(replacedDirectory
        ? metadata("initial.md", root, originalPath, "replacement-directory/initial.md") : metadata(redirectedPath)));
      expect(files.fileRequests(redirectedPath)).toHaveLength(replacedDirectory ? 4 : 1);
      await act(async () => {
        if (outcome === "two failed files and a successful open") redirected!.reject(new TypeError("Redirected body disconnected"));
        else redirected!.resolve(new Response(null, { status: 403 }));
      });
      expect(screen.getAllByRole("alert")).toHaveLength(2);
      if (replacedDirectory) {
        const labels = (prefix: RegExp) => new Set(screen.getAllByRole("button", { name: prefix }).map((button) => button.textContent));
        expect(labels(/^Retry opening/).size).toBe(2);
        expect(labels(/^Edit path for/).size).toBe(2);
        return;
      }
      expect(screen.getByRole("button", { name: `Retry opening ${originalPath}` })).toBeVisible();
      if (outcome === "two failed files retried") {
        const retryRedirected = deferred<Response>();
        const retryOriginal = deferred<Response>();
        files.meta(originalPath, retryRedirected.promise);
        await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry opening ${root}/redirected.md` })));
        expect(screen.getByRole("button", { name: `Cancel confirmation for ${root}/redirected.md` })).toBeVisible();
        expect(screen.getByRole("button", { name: `Retry opening ${originalPath}` })).toBeVisible();
        files.meta(originalPath, retryOriginal.promise);
        await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry opening ${originalPath}` })));
        expect(screen.getAllByRole("button", { name: /^Cancel confirmation/ })).toHaveLength(2);
        expect(screen.getByRole("button", { name: `Cancel confirmation for ${originalPath}` })).toBeVisible();
        expect(screen.getByRole("button", { name: `Cancel confirmation for ${root}/redirected.md` })).toBeVisible();
        await act(async () => retryOriginal.resolve(metadata("initial.md")));
        await act(async () => current.resolve(new Response("# Recovered original")));
        expect(screen.getByRole("heading", { name: "Recovered original" })).toBeVisible();
        await act(async () => retryRedirected.resolve(metadata("redirected.md")));
        expect(selected()).toBe(tab("initial.md"));
        fireEvent.click(tab("redirected.md"));
        expect(screen.getByRole("heading", { name: "Independently redirected" })).toBeVisible();
        return;
      }
      if (outcome === "two failed files and a cancelled open" || outcome === "two failed files and a successful open") {
        const repeat = deferred<Response>();
        files.meta(originalPath, repeat.promise);
        await openLink("Open original");
        expect(screen.getAllByRole("alert")).toHaveLength(2);
        if (outcome === "two failed files and a cancelled open") {
          fireEvent.click(screen.getByRole("button", { name: `Cancel confirmation for ${originalPath} (target unconfirmed)` }));
          await act(async () => repeat.resolve(metadata("initial.md")));
          expect(screen.getAllByRole("alert")).toHaveLength(2);
          expect(selected()).toBe(tab("picked.md"));
        } else {
          await act(async () => repeat.resolve(metadata("initial.md")));
          await act(async () => current.resolve(new Response("# Recovered original")));
          fireEvent.click(tab("picked.md"));
          expect(screen.getByRole("alert")).toHaveTextContent("Check your connection and retry.");
        }
        return;
      }
      files.meta(originalPath, () => metadata("redirected.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry opening ${root}/redirected.md` })));
      expect(screen.getByRole("heading", { name: "Independently redirected" })).toBeVisible();
      fireEvent.click(tab("picked.md"));
      expect(screen.getByRole("alert")).toHaveTextContent(`Couldn't open ${originalPath}.`);
      expect(screen.getByRole("button", { name: `Retry opening ${originalPath}` })).toBeVisible();
      return;
    }
    await act(async () => current.resolve(new Response("# Recovered original")));
    expect(screen.getByRole("heading", { name: "Recovered original" })).toBeVisible();

    const closesOriginal = outcome === "closed original";
    if (closesOriginal) closeTab("initial.md");
    const result = outcome === "denied" ? new Response(null, { status: 403 })
      : metadata(outcome.startsWith("redirected") ? "redirected.md" : "initial.md");
    if (outcome === "redirected slow") vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => delayed.resolve(result));
      expect(selected()).toBe(tab(closesOriginal ? "picked.md" : "initial.md"));
      expect(screen.queryByRole("button", { name: "Cancel confirmation" })).not.toBeInTheDocument();
      if (outcome === "redirected") {
        fireEvent.click(tab("redirected.md"));
        expect(screen.getByRole("heading", { name: "Independently redirected" })).toBeVisible();
      } else if (outcome === "redirected slow") {
        expect(files.fileRequests("redirected.md")).toHaveLength(1);
        await act(async () => vi.advanceTimersByTime(5000));
        files.meta(originalPath, () => metadata("redirected.md"));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening redirected.md" })));
        expect(selected()).toBe(tab("redirected.md"));
        expect(screen.getByRole("heading", { name: "Independently redirected" })).toBeVisible();
        expect(files.fileRequests("redirected.md")).toHaveLength(2);
      } else if (outcome === "redirected denied") {
        await act(async () => redirected!.resolve(new Response(null, { status: 403 })));
        fireEvent.click(tab("picked.md"));
        expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
        expect(screen.getByRole("button", { name: `Retry opening ${originalPath}` })).toBeVisible();
      } else {
        expect(screen.getAllByRole("tab")).toHaveLength(closesOriginal ? 1 : 2);
        expect(files.fileRequests("initial.md")).toHaveLength(3);
        fireEvent.click(tab("picked.md"));
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      }
    } finally {
      if (outcome === "redirected slow") vi.useRealTimers();
    }
  });

  it("closes a superseded failure's editor and returns focus to its tab", async () => {
    const files = documentRead({ "a.md": "# Guide\n\n[Open alias](alias.md)" });
    const older = deferred<Response>();
    const newer = deferred<Response>();
    files.meta("alias.md", older.promise);
    openReader();
    await openLink("Open alias");
    await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(1));
    files.meta("alias.md", newer.promise);
    await openLink("Open alias");
    await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(2));
    await act(async () => older.resolve(new Response(null, { status: 404 })));
    fireEvent.click(screen.getByRole("button", { name: "Edit path for alias.md" }));
    const field = screen.getByRole("textbox", { name: "File path" });
    fireEvent.change(field, { target: { value: "correction.md" } });
    act(() => field.focus());
    expect(field).toHaveFocus();
    await act(async () => newer.resolve(metadata("a.md")));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(field).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open file" })).not.toBeInTheDocument();
    expect(tab("a.md")).toHaveFocus();
    expect(files.fileRequests("correction.md")).toHaveLength(0);
  });

  it.each(["failure after cancel", "failure before cancel", "success after cancel"] as const)(
    "preserves the older candidate's outcome without activating it: %s", async (outcome) => {
      const files = documentRead({ "a.md": "# Original document\n\n[Read alias](alias.md)" });
      const older = deferred<Response>();
      const newer = deferred<Response>();
      files.meta("alias.md", older.promise);
      openReader();
      await openLink("Read alias");
      await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(1));
      files.meta("alias.md", newer.promise);
      await openLink("Read alias");
      await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(2));
      const cancelOpening = screen.getAllByRole("button", { name: "Cancel opening alias.md" })[1];
      const fails = () => older.resolve(new Response(null, { status: 404 }));
      if (outcome === "failure before cancel") await act(async () => fails());
      fireEvent.click(cancelOpening);
      if (outcome === "failure after cancel") await act(async () => fails());
      if (outcome === "success after cancel") await act(async () => older.resolve(metadata("b.md")));

      expect(files.fileRequests("alias.md")[1][1]?.signal?.aborted).toBe(true);
      expect(selected()).toBe(tab("a.md"));
      if (outcome === "success after cancel") {
        fireEvent.click(await screen.findByRole("tab", { name: "b.md" }));
        expect(screen.getByRole("heading", { name: "b.md body" })).toBeVisible();
      } else {
        expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open alias.md. File not found.");
        expect(screen.getByRole("button", { name: "Retry opening alias.md" })).toBeVisible();
      }
    },
  );

  it("re-resolves an alias after it is repointed, across source boundaries", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Follow alias](/shared/alias.md)" });
    files.meta("/shared/alias.md", Promise.resolve(metadata("b.md", "/shared")));
    openReader();
    await openLink("Follow alias");
    expect(await screen.findByRole("heading", { name: "b.md body" })).toBeVisible();
    fireEvent.click(tab("a.md"));
    files.meta("/shared/alias.md", Promise.resolve(metadata("c.md", "/shared/new")));
    await openLink("Follow alias");
    expect(await screen.findByRole("heading", { name: "c.md body" })).toBeVisible();
    expect(files.fileRequests("/shared/alias.md")).toHaveLength(2);
    expect(files.fileRequests("b.md")).toHaveLength(1);
  });

  it.each([
    [0, "Check your connection"], [403, "don't have access"], [404, "File not found"], [409, "path changed"],
    [413, "exceeds the preview limit"], [415, "format can't be previewed"],
  ])("reports failure %i on the originating tab and retries the target", async (status, message) => {
    const files = documentRead({ "a.md": "# Original\n\n[Read B](b.md)", "b.md": "# Retried document" });
    const failed = files.body("b.md");
    openReader("a.md", { kind: "heading", slug: "original" });
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    const scroller = screen.getByRole("heading", { name: "Original" }).closest(".file-viewer-body")!;
    scroller.scrollTop = 135;
    await act(async () => {
      if (status) failed.resolve(new Response(null, { status }));
      else failed.reject(new TypeError("Network disconnected"));
    });
    expect(screen.getByRole("alert")).toHaveTextContent(`Couldn't open b.md.`);
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(selected()).toBe(tab("a.md"));
    expect(scroller.scrollTop).toBe(135);
    fireEvent.click(screen.getByRole("button", { name: "Retry opening b.md" }));
    expect(await screen.findByRole("heading", { name: "Retried document" })).toBeVisible();
    expect(files.fileRequests("b.md")).toHaveLength(4);
  });

  it("cancels the newer request without restoring the earlier request's activation", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    const c = files.body("c.md");
    openReader();
    await openLink("Read B");
    await openLink("Read C");
    await waitFor(() => expect(files.fileRequests("c.md")).toHaveLength(2));
    cancel("c.md");
    expect(files.fileRequests("c.md")[1][1]?.signal?.aborted).toBe(true);
    await act(async () => {
      c.resolve(new Response("# C leaked"));
      b.resolve(new Response("# B ready"));
    });
    expect(selected()).toBe(tab("a.md"));
    expect(screen.queryByRole("tab", { name: "c.md" })).not.toBeInTheDocument();
    fireEvent.click(tab("b.md"));
    expect(screen.getByRole("heading", { name: "B ready" })).toBeVisible();
  });

  it.each(["resolve", "reject"] as const)("ignores a closed entry's late %s after reopening the same file", async (outcome) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)" });
    const former = files.body("b.md");
    const current = files.body("b.md");
    openReader();
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    cancel("b.md");
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(4));
    await act(async () => current.resolve(new Response("# Current attempt")));
    await act(async () => {
      if (outcome === "resolve") former.resolve(new Response("# Retired text"));
      else former.reject(new Error("Retired error"));
    });
    expect(screen.getByRole("heading", { name: "Current attempt" })).toBeVisible();
    expect(screen.queryByText("Retired text")).not.toBeInTheDocument();
    fireEvent.click(tab("a.md"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps a reloading tab selectable and updates only that tab after switching", async () => {
    const files = documentRead({ "a.md": "# Original A\n\n[Read B](b.md)", "b.md": "# Reading B" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Reading B" });
    const bScroll = screen.getByRole("tabpanel").querySelector<HTMLElement>(".file-viewer-body")!;
    bScroll.scrollTop = 95;
    fireEvent.click(tab("a.md"));
    const original = screen.getByRole("heading", { name: "Original A" });
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    expect(original).toBeVisible();
    expect(screen.queryByRole("button", { name: "Edit path" })).not.toBeInTheDocument();
    fireEvent.click(tab("b.md"));
    expect(tab("a.md")).toBeVisible();
    await act(async () => reload.resolve(new Response("# Updated A")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Reading B" })).toBeVisible();
    expect(bScroll.scrollTop).toBe(95);
    fireEvent.click(tab("a.md"));
    expect(await screen.findByRole("heading", { name: "Updated A" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    expect(screen.getByRole("textbox", { name: "File path" })).toBeVisible();
    expect(screen.queryByText("Original A")).not.toBeInTheDocument();
    expect(files.fileRequests("b.md")).toHaveLength(2);
  });

  it.each(["another tab", "the retained snapshot"] as const)("preserves a newer location from %s when reloading", async (origin) => {
    const files = documentRead({
      "a.md": "# Original A\n\n[Read B](b.md)\n\n[Go to line](#L7)\n\nOld target line",
      "b.md": "# B\n\n[Find line in A](a.md#L7)",
    });
    openReader();
    if (origin === "another tab") {
      await openLink("Read B");
      await screen.findByRole("heading", { name: "B" });
      fireEvent.click(tab("a.md"));
    } else {
      await screen.findByRole("heading", { name: "Original A" });
    }
    const firstReload = files.body("a.md");
    const retry = files.body("a.md");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reload file" })));
      expect(files.fileRequests("a.md")).toHaveLength(4);
      if (origin === "another tab") {
        fireEvent.click(tab("b.md"));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Find line in A at line 7" })));
      } else {
        fireEvent.click(screen.getByRole("link", { name: "Go to line" }));
      }
      expect(screen.getByText("Old target line", { selector: "[data-line]" })).toHaveAttribute("data-located", "true");
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry reloading a.md" })));
      await act(async () => retry.resolve(new Response("# Reloaded A\n\nLine three\n\nLine five\n\nNew target line")));
      await act(async () => firstReload.resolve(new Response("# Stale reload")));
      expect(screen.getByText("New target line", { selector: "[data-line]" })).toHaveAttribute("data-located", "true");
      expect(screen.queryByText("Stale reload")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([0, 404, 403])("withdraws the original snapshot and can retry a terminal reload failure (%s)", async (status) => {
    const files = documentRead({ "a.md": "# Original snapshot" });
    openReader();
    await screen.findByRole("heading", { name: "Original snapshot" });
    const original = tab("a.md").getAttribute("aria-controls");
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    await act(async () => {
      if (status) reload.resolve(new Response(null, { status }));
      else reload.reject(new TypeError("Network unavailable"));
    });
    expect(screen.queryByText("Original snapshot")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(status === 403 ? "don't have access" :
      status === 404 ? "File not found" : "Check your connection");
    expect(screen.queryByRole("link", { name: "Download file" })).not.toBeInTheDocument();
    const retry = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(6));
    await act(async () => retry.resolve(new Response("# Recovered snapshot")));
    expect(await screen.findByRole("heading", { name: "Recovered snapshot" })).toBeVisible();
    expect(tab("a.md")).toHaveAttribute("aria-controls", original);
  });

  it("keeps a newer reload and discards a reload response after the tab closes", async () => {
    const files = documentRead({ "a.md": "# Original A\n\n[Read B](b.md)" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "b.md body" });
    fireEvent.click(tab("a.md"));
    const former = files.body("a.md");
    const current = files.body("a.md");
    const reload = screen.getByRole("button", { name: "Reload file" });
    fireEvent.click(reload);
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    fireEvent.click(reload);
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(6));
    await act(async () => current.resolve(new Response("# Current A")));
    await act(async () => former.resolve(new Response(null, { status: 403 })));
    expect(await screen.findByRole("heading", { name: "Current A" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")[3][1]?.signal?.aborted).toBe(true);
    const retiring = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(8));
    closeTab("a.md");
    await act(async () => retiring.resolve(new Response("# Retired A")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.queryByText("Retired A")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")[7][1]?.signal?.aborted).toBe(true);
  });

  it.each(["duplicate is still open", "duplicate was closed", "reload bytes denied"] as const)(
    "settles an alias reload in its own slot when the %s", async (outcome) => {
      const files = documentRead({ "a.md": "# Original A\n\n[Read B](b.md)" });
      openReader();
      await openLink("Read B");
      await screen.findByRole("heading", { name: "b.md body" });
      const foreground = screen.getByRole("tabpanel").querySelector<HTMLElement>(".file-viewer-body")!;
      foreground.scrollTop = 95;
      fireEvent.click(tab("a.md"));
      files.meta("a.md", Promise.resolve(metadata("b.md")));
      const pending = files.body("b.md");
      fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
      await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(3));
      fireEvent.click(tab("b.md"));
      if (outcome === "duplicate was closed") closeTab("b.md");
      await act(async () => pending.resolve(outcome === "reload bytes denied"
        ? new Response(null, { status: 403 }) : new Response("# Read from the current alias")));
      if (outcome === "duplicate was closed") {
        expect(await screen.findByRole("heading", { name: "Read from the current alias" })).toBeVisible();
        expect(screen.getByRole("tab")).toHaveTextContent("b.md");
      } else {
        expect(selected()).toBe(tab("b.md"));
        expect(screen.getByRole("heading", { name: "b.md body" })).toBeVisible();
        expect(foreground.scrollTop).toBe(95);
        fireEvent.click(tab("a.md"));
        expect(screen.getByRole("alert")).toHaveTextContent(outcome === "reload bytes denied"
          ? "don't have access" : "already open in another tab");
        expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
      }
      expect(screen.queryByText("Original A")).not.toBeInTheDocument();
    },
  );

  it("retains the origin after a path edit fails to reauthorize a known file", async () => {
    const files = documentRead({
      "a.md": "[Read B](b.md)\n\n[Read C](c.md)",
      "b.md": "# Saved B",
      "c.md": "# Save my place",
    });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Saved B" });
    fireEvent.click(tab("a.md"));
    await openLink("Read C");
    await screen.findByRole("heading", { name: "Save my place" });
    files.meta("b.md", Promise.resolve(new Response(null, { status: 403 })));
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    fireEvent.change(screen.getByRole("textbox", { name: "File path" }), { target: { value: "b.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("don't have access");
    expect(selected()).toBe(tab("c.md"));
    expect(screen.getByRole("heading", { name: "Save my place" })).toBeVisible();
    const retry = screen.getByRole("button", { name: "Retry opening b.md" });
    const pending = deferred<Response>();
    files.meta("b.md", pending.promise);
    fireEvent.click(retry);
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(4));
    closeTab("c.md");
    await act(async () => pending.resolve(metadata("b.md")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Saved B" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("b.md")[3][1]?.signal?.aborted).toBe(true);
  });

  it("ignores superseded responses when the same tab starts a newer attempt", async () => {
    const files = documentRead({ "a.md": "# Current attempt" });
    const former = files.body("a.md");
    openReader();
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    const slot = tab("a.md").getAttribute("aria-controls");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    expect(await screen.findByRole("heading", { name: "Current attempt" })).toBeVisible();
    await act(async () => former.resolve(new Response(null, { status: 403 })));
    expect(tab("a.md")).toHaveAttribute("aria-controls", slot);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")).toHaveLength(4);
  });

  it.each(["automatic", "select C", "locate in C", "page source in C"] as const)("keeps B's activation consistent after closing A and %s", async (next) => {
    const files = documentRead({
      "left.md": "# Left\n\n[Read A](a.md)", "a.md": "# A\n\n[Read C](c.md)\n\n[Read B](b.md)",
      "c.md": next === "page source in C" ? `# In C\n${"source line\n".repeat(2001)}` : "# In C\n\n[Locate](#in-c)",
    });
    const b = files.body("b.md");
    openReader("left.md");
    await openLink("Read A");
    await openLink("Read C");
    await screen.findByRole("heading", { name: "In C" });
    fireEvent.click(tab("a.md"));
    await openLink("Read B");
    closeTab("a.md");
    expect(selected()).toBe(tab("c.md"));
    if (next === "select C") fireEvent.click(tab("c.md"));
    if (next === "locate in C") fireEvent.click(screen.getByRole("link", { name: "Locate" }));
    if (next === "page source in C") {
      fireEvent.click(screen.getByRole("button", { name: "Source" }));
      fireEvent.click(screen.getByRole("button", { name: "Next lines" }));
    }
    await act(async () => b.resolve(new Response("# B ready")));
    expect(selected()).toBe(tab(next === "automatic" ? "b.md" : "c.md"));
    if (next === "automatic") closeTab("b.md");
    closeTab("c.md");
    expect(selected()).toBe(tab(next === "automatic" ? "left.md" : "b.md"));
  });

  it("discards a candidate's failure when its originating tab is closed", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    openReader();
    await openLink("Read C");
    await screen.findByRole("heading", { name: "c.md body" });
    fireEvent.click(tab("a.md"));
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    closeTab("a.md");
    await act(async () => b.resolve(new Response(null, { status: 404 })));
    expect(screen.queryByText("b.md loading…")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(selected()).toBe(tab("c.md"));
  });

  it.each([true, false])("keeps the empty viewer's selection failure after the old read fails first: %s", async (oldFirst) => {
    const files = documentRead({ "start.md": "[Read A](a.md)", "a.md": "# Selected file after retry" });
    const former = files.body("a.md");
    openReader("start.md");
    await openLink("Read A");
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    closeTab("start.md");
    fireEvent.click(screen.getByRole("button", { name: "Choose a file" }));
    const confirmation = deferred<Response>();
    files.meta("a.md", confirmation.promise);
    fireEvent.click(within(await screen.findByRole("list")).getByRole("button", { name: "a.md" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(3));
    const failFormer = () => former.resolve(new Response(null, { status: 404 }));
    if (oldFirst) await act(async () => failFormer());
    await act(async () => confirmation.resolve(new Response(null, { status: 403 })));
    if (!oldFirst) await act(async () => failFormer());
    expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
    files.meta("a.md", Promise.resolve(metadata("a.md")));
    fireEvent.click(screen.getByRole("button", { name: "Retry opening a.md" }));
    expect(await screen.findByRole("heading", { name: "Selected file after retry" })).toBeVisible();
  });

  it.each([true, false])("settles an independent open while the same file is retrying its reload (reload succeeds: %s)", async (succeeds) => {
    const files = documentRead({ "a.md": "# Keep reading A\n\n[Read B](b.md)", "b.md": "# Old B" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Old B" });
    const failed = files.body("b.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => failed.resolve(new Response(null, { status: 403 })));
    const reloading = files.body("b.md");
    const opening = files.body("b.md");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(6));
    fireEvent.click(tab("a.md"));
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(8));
    await act(async () => reloading.resolve(succeeds ? new Response("# Reloaded B") : new Response(null, { status: 403 })));
    expect(selected()).toBe(tab("a.md"));
    await act(async () => opening.resolve(succeeds ? new Response("# Independently read B") : new Response(null, { status: 404 })));
    if (succeeds) {
      expect(screen.getAllByRole("tab", { name: "b.md" })).toHaveLength(1);
      expect(selected()).toBe(tab("b.md"));
      expect(screen.getByRole("heading", { name: "Reloaded B" })).toBeVisible();
    } else {
      expect(selected()).toBe(tab("a.md"));
      expect(screen.getByRole("alert")).toHaveTextContent("File not found");
      expect(screen.getByRole("button", { name: "Retry opening b.md" })).toBeVisible();
    }
  });

  it("can reuse the source the server confirms after closing a different potential match", async () => {
    const wide = "/shared";
    const narrow = "/shared/nested";
    const absolute = `${narrow}/report.md`;
    const files = documentRead({
      "a.md": "# A\n\n[Read wide](/shared/wide.md)\n\n[Read narrow](/shared/narrow.md)\n\n[Open report](/shared/nested/report.md)",
      "nested/report.md": "# Wide snapshot",
      "report.md": "# Narrow snapshot",
    });
    files.meta("/shared/wide.md", Promise.resolve(metadata("nested/report.md", wide, absolute)));
    files.meta("/shared/narrow.md", Promise.resolve(metadata("report.md", narrow, absolute)));
    openReader();
    await openLink("Read wide");
    await screen.findByRole("heading", { name: "Wide snapshot" });
    fireEvent.click(tab("a.md"));
    await openLink("Read narrow");
    await screen.findByRole("heading", { name: "Narrow snapshot" });
    fireEvent.click(tab("a.md"));
    const confirmation = deferred<Response>();
    files.meta(absolute, confirmation.promise);
    await openLink("Open report");
    await waitFor(() => expect(files.fileRequests(absolute)).toHaveLength(1));
    fireEvent.click(screen.getAllByRole("button", { name: "Close report.md" })[0]);
    await act(async () => confirmation.resolve(metadata("report.md", narrow, absolute)));
    expect(screen.getByRole("heading", { name: "Narrow snapshot" })).toBeVisible();
    expect(selected()).toBe(tab("report.md"));
    expect(files.fileRequests("report.md")).toHaveLength(1);
  });

  it("retires a selection confirmation when its merged origin closes", async () => {
    const files = documentRead({ "picked.md": "# Stale selected file" });
    const initial = deferred<Response>();
    files.meta("start.md", initial.promise);
    files.body("picked.md");
    openReader("start.md");
    const choosePicked = async () => {
      fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
      fireEvent.click(within(await screen.findByRole("list")).getByRole("button", { name: "picked.md" }));
    };
    await choosePicked();
    await waitFor(() => expect(files.fileRequests("picked.md")).toHaveLength(2));
    const confirmation = deferred<Response>();
    files.meta("picked.md", confirmation.promise);
    await choosePicked();
    await waitFor(() => expect(files.fileRequests("picked.md")).toHaveLength(3));
    await act(async () => initial.resolve(metadata("picked.md")));
    expect(selected()).toBe(tab("picked.md"));
    expect(screen.getByRole("button", { name: "Cancel confirmation" })).toBeVisible();
    closeTab("picked.md");
    await act(async () => confirmation.resolve(metadata("picked.md")));
    expect(files.fileRequests("picked.md")).toHaveLength(3);
    expect(files.fileRequests("picked.md")[2][1]?.signal?.aborted).toBe(true);
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose a file" })).toBeVisible();
  });

  it("retries a slow confirmation before reusing a ready tab", async () => {
    const path = `${root}/b.md`;
    const files = documentRead({ "a.md": `# A\n\n[Read B](b.md)\n\n[Read again](${path})`, "b.md": "# Cached B" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Cached B" });
    fireEvent.click(tab("a.md"));
    const stale = deferred<Response>();
    const current = deferred<Response>();
    files.meta(path, stale.promise);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Read again" })));
      expect(selected()).toBe(tab("a.md"));
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta(path, current.promise);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry confirmation" })));
      expect(files.fileRequests(path)[0][1]?.signal?.aborted).toBe(true);
      await act(async () => stale.resolve(new Response(null, { status: 403 })));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(selected()).toBe(tab("a.md"));
      await act(async () => current.resolve(metadata("b.md")));
      expect(selected()).toBe(tab("b.md"));
      expect(files.fileRequests("b.md")).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels and retries parallel confirmations by their selected paths", async () => {
    const absolute = `${root}/b.md`;
    const files = documentRead({ "a.md": `# A\n\n[Relative](b.md)\n\n[Absolute](${absolute})`, "b.md": "# Cached B" });
    openReader();
    await openLink("Relative");
    await screen.findByRole("heading", { name: "Cached B" });
    fireEvent.click(tab("a.md"));
    const relative = deferred<Response>();
    const stale = deferred<Response>();
    const retried = deferred<Response>();
    files.meta("b.md", relative.promise);
    files.meta(absolute, stale.promise);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Relative" })));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Absolute" })));
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta(absolute, retried.promise);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry confirmation for ${absolute}` })));
      expect(files.fileRequests(absolute)).toHaveLength(2);
      expect(files.fileRequests(absolute)[0][1]?.signal?.aborted).toBe(true);
      expect(files.fileRequests("b.md")[2][1]?.signal?.aborted).toBe(false);
      fireEvent.click(screen.getByRole("button", { name: "Cancel confirmation for b.md" }));
      expect(files.fileRequests("b.md")[2][1]?.signal?.aborted).toBe(true);
      expect(files.fileRequests(absolute)[1][1]?.signal?.aborted).toBe(false);
      await act(async () => {
        relative.resolve(new Response(null, { status: 404 }));
        stale.resolve(new Response(null, { status: 403 }));
      });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(selected()).toBe(tab("a.md"));
      await act(async () => retried.resolve(metadata("b.md")));
      expect(selected()).toBe(tab("b.md"));
      expect(screen.getByRole("heading", { name: "Cached B" })).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a superseded confirmation from reopening after a newer choice is ready", async () => {
    const path = `${root}/b.md`;
    const files = documentRead({ "a.md": `# A\n\n[Read B](${path})` });
    openReader();
    files.meta(path, () => metadata("b.md"));
    await openLink("Read B");
    await screen.findByRole("heading", { name: "b.md body" });
    fireEvent.click(tab("a.md"));
    const older = deferred<Response>();
    files.meta(path, older.promise);
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests(path)).toHaveLength(2));
    closeTab("b.md");
    const failed = files.body("c.md");
    files.meta(path, () => metadata("c.md"));
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("c.md")).toHaveLength(1));
    await act(async () => failed.resolve(new Response(null, { status: 404 })));
    expect(screen.getByRole("alert")).toHaveTextContent("File not found");
    expect(files.fileRequests(path)[1][1]?.signal?.aborted).toBe(false);
    files.meta(path, () => metadata("d.md"));
    await openLink("Read B");
    await screen.findByRole("heading", { name: "d.md body" });
    await act(async () => older.resolve(metadata("e.md")));
    expect(files.fileRequests("e.md")).toHaveLength(0);
    expect(files.fileRequests(path)[1][1]?.signal?.aborted).toBe(true);
    expect(selected()).toBe(tab("d.md"));
  });

  it.each([
    { lateTarget: "b.md", denied: false, kind: "retry" },
    { lateTarget: "c.md", denied: false, kind: "retry" },
    { lateTarget: "b.md", denied: true, kind: "retry" },
    { lateTarget: "c.md", denied: true, kind: "retry" },
    { lateTarget: "b.md", denied: true, kind: "ordinary opening" },
    { lateTarget: "c.md", denied: true, kind: "ordinary opening" },
  ])("checks a delayed $kind to $lateTarget after reopening and closing B (newer denial: $denied)", async ({ lateTarget, denied, kind }) => {
    const path = `${root}/b.md`;
    const files = documentRead({ "a.md": `# Guide\n\n[Read B](${path})` });
    files.meta(path, () => metadata("b.md"));
    const retrying = kind === "retry";
    openReader();
    if (retrying) {
      const failed = files.body("b.md");
      await openLink("Read B");
      await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(1));
      await act(async () => failed.resolve(new Response(null, { status: 404 })));
      expect(screen.getByRole("alert")).toHaveTextContent("File not found");
      files.body("b.md");
      await openLink("Read B");
      await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    } else {
      await openLink("Read B");
      await screen.findByRole("heading", { name: "b.md body" });
      fireEvent.click(tab("a.md"));
    }
    const retry = deferred<Response>();
    files.meta(path, retry.promise);
    if (retrying) await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry opening ${path}` })));
    else await openLink("Read B");
    expect(screen.getByRole("button", { name: "Cancel confirmation" })).toBeVisible();
    if (retrying) cancel("b.md");
    else closeTab("b.md");
    const initialReads = retrying ? 2 : 1;
    if (denied) {
      const middle = deferred<Response>();
      const latestBody = files.body("b.md");
      files.meta(path, middle.promise);
      await openLink("Read B");
      await waitFor(() => expect(files.fileRequests(path)).toHaveLength(initialReads + 2));
      files.meta(path, () => metadata("b.md"));
      await openLink("Read B");
      await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(initialReads + 1));
      await act(async () => latestBody.resolve(new Response(null, { status: 403 })));
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      await act(async () => middle.resolve(metadata("b.md")));
      expect(tab("b.md")).toBeVisible();
      expect(selected()).toBe(tab("a.md"));
    } else {
      files.meta(path, () => metadata("b.md"));
      await openLink("Read B");
      await screen.findByRole("heading", { name: "b.md body" });
    }
    closeTab("b.md");
    await act(async () => retry.resolve(metadata(lateTarget)));

    expect(selected()).toBe(tab("a.md"));
    expect(screen.queryByRole("tab", { name: "b.md" })).not.toBeInTheDocument();
    expect(files.fileRequests("b.md")).toHaveLength(initialReads + (denied ? 2 : 1));
    if (denied) expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
    if (lateTarget === "c.md") {
      fireEvent.click(tab("c.md"));
      expect(screen.getByRole("heading", { name: "c.md body" })).toBeVisible();
    }
  });

  it("preserves whitespace in failed paths whose controls need distinct accessible names", async () => {
    const paths = [`${root}/report  draft.md`, `${root}/report draft.md`];
    const links = paths.map((path, index) => `[Open ${index + 1}](${encodeURI(path)})`);
    const files = documentRead({ "a.md": `# Guide\n\n${links.join("\n\n")}` });
    openReader();
    for (const [index, path] of paths.entries()) {
      files.meta(path, () => new Response(null, { status: 404 }));
      await openLink(`Open ${index + 1}`);
      await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(index + 1));
    }
    const alerts = screen.getAllByRole("alert");
    for (const [index, path] of paths.entries()) {
      expect(alerts[index].textContent).toContain(`Couldn't open ${path}, attempt ${index + 1}.`);
      expect(screen.getByRole("button", { name: `Retry opening ${paths[1]}, attempt ${index + 1}` })).toBeVisible();
    }
  });

  it("selects parallel confirmations with whitespace and attempt suffixes in their paths", async () => {
    const paths = [`${root}/report  draft.md`, `${root}/report draft.md`];
    const suffixPath = `${paths[1]}, attempt 1`;
    const names = paths.map((path) => path.slice(root.length + 1));
    const links = paths.map((path, index) => `[Open ${index + 1}](${encodeURI(path)})`);
    const files = documentRead({ "a.md": `# Guide\n\n${links.join("\n\n")}\n\n[Open unsupported](${encodeURI(suffixPath)})` });
    openReader();
    for (const [index, path] of paths.entries()) {
      files.meta(path, () => metadata(names[index]));
      await openLink(`Open ${index + 1}`);
      await screen.findByRole("heading", { name: `${names[index]} body`.replace(/ +/g, " ") });
      fireEvent.click(tab("a.md"));
    }
    files.meta(suffixPath, () => new Response(null, { status: 415 }));
    await openLink("Open unsupported");
    expect(await screen.findByRole("alert")).toHaveTextContent("format can't be previewed");
    const confirmations = paths.map(() => deferred<Response>());
    const retry = deferred<Response>();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      for (const [index, path] of paths.entries()) {
        files.meta(path, confirmations[index].promise);
        await act(async () => fireEvent.click(screen.getByRole("button", { name: `Open file: Open ${index + 1}` })));
      }
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta(paths[1], retry.promise);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: `Retry confirmation for ${root}/report draft.md, attempt 3` })));
      expect(files.fileRequests(paths[1])).toHaveLength(3);
      fireEvent.click(screen.getByRole("button", { name: `Cancel confirmation for ${root}/report draft.md, attempt 2` }));
      expect(files.fileRequests(paths[0])[1][1]?.signal?.aborted).toBe(true);
      expect(files.fileRequests(paths[1])[1][1]?.signal?.aborted).toBe(true);
      expect(files.fileRequests(paths[1])[2][1]?.signal?.aborted).toBe(false);
      expect(screen.getByRole("button", { name: `Retry opening ${suffixPath}` })).toBeVisible();
      await act(async () => retry.resolve(metadata(names[1])));
      expect(screen.getByRole("heading", { name: `${names[1]} body` })).toBeVisible();
      expect(files.fileRequests(names[1])).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reuses a ready snapshot ahead of an older candidate for the same identity", async () => {
    const files = documentRead({ "start.md": "# Start\n\n[Read B](b.md)\n\n[Read alias](alias.md)", "b.md": "# Reloaded B" });
    const former = files.body("b.md");
    openReader("start.md");
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    await openLink("Read alias");
    await screen.findByRole("heading", { name: "alias.md body" });
    files.meta("alias.md", Promise.resolve(metadata("b.md")));
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await screen.findByRole("heading", { name: "Reloaded B" });
    expect(screen.getByRole("button", { name: "Cancel opening b.md" })).toBeVisible();
    fireEvent.click(tab("start.md"));
    await openLink("Read B");
    expect(await screen.findByRole("heading", { name: "Reloaded B" })).toBeVisible();
    await act(async () => former.resolve(new Response(null, { status: 404 })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Reloaded B" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([false, true])("preserves the pending tab from an empty viewer with an explicit picker action of %s", async (pick) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)", "picked.md": "# Chosen from the source root" });
    const b = files.body("b.md");
    openReader();
    await openLink("Read B");
    closeTab("a.md");
    const choose = screen.getByRole("button", { name: "Choose a file" });
    expect(choose).toHaveFocus();
    if (pick) fireEvent.click(choose);
    await act(async () => b.resolve(new Response("# B ready")));
    if (pick) {
      expect(screen.getByRole("heading", { name: "Choose a file" })).toBeVisible();
      expect(screen.queryByRole("heading", { name: "B ready" })).not.toBeInTheDocument();
      fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
      expect(await screen.findByRole("heading", { name: "Chosen from the source root" })).toBeVisible();
      expect(tab("b.md")).toHaveAttribute("aria-selected", "false");
    } else expect(screen.getByRole("heading", { name: "B ready" })).toBeVisible();
  });

  it("merges aliases before reading bytes and leaves the newer location in place", async () => {
    const files = documentRead({
      "a.md": "# A\n\n[Older alias](alias.md#L3)\n\n[New location](target.md#L5)\n\n[Other alias](other.md#L3)",
      "target.md": "# Target\n\nEarlier position\n\nNewer position",
    });
    const alias = deferred<Response>();
    files.meta("alias.md", alias.promise);
    files.meta("other.md", Promise.resolve(metadata("target.md")));
    openReader();
    fireEvent.click(await screen.findByRole("button", { name: /Open file: Older alias/ }));
    fireEvent.click(screen.getByRole("button", { name: /Open file: New location/ }));
    expect(await screen.findByText("Newer position")).toHaveAttribute("data-located", "true");
    await act(async () => alias.resolve(metadata("target.md")));
    expect(screen.getByText("Newer position")).toHaveAttribute("data-located", "true");
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(files.fileRequests("target.md")).toHaveLength(2);
    fireEvent.click(tab("a.md"));
    fireEvent.click(screen.getByRole("button", { name: /Open file: Other alias/ }));
    await waitFor(() => expect(screen.getByText("Earlier position")).toHaveAttribute("data-located", "true"));
    expect(files.fileRequests("target.md")).toHaveLength(2);
  });

  it("reuses a pending canonical file, applying the newer alias position on readiness", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Initial](target.md)\n\n[Alias](alias.md#L3)" });
    const target = files.body("target.md");
    files.meta("alias.md", Promise.resolve(metadata("target.md")));
    openReader();
    await openLink("Initial");
    await waitFor(() => expect(files.fileRequests("target.md")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: /Open file: Alias/ }));
    await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(1));
    await act(async () => target.resolve(new Response("# Shared document\n\nLocated text")));
    expect(await screen.findByText("Located text")).toHaveAttribute("data-located", "true");
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(files.fileRequests("target.md")).toHaveLength(2);
  });

  it("keeps independent retries when two openings fail in reverse order", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    const c = files.body("c.md");
    openReader();
    await openLink("Read B");
    await openLink("Read C");
    await waitFor(() => expect(files.fileRequests("c.md")).toHaveLength(2));
    await act(async () => c.resolve(new Response(null, { status: 404 })));
    await act(async () => b.resolve(new Response(null, { status: 403 })));
    expect(screen.getAllByRole("alert")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Retry opening c.md" }));
    expect(await screen.findByRole("heading", { name: "c.md body" })).toBeVisible();
    fireEvent.click(tab("a.md"));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open b.md.");
    expect(screen.getByRole("button", { name: "Retry opening b.md" })).toBeVisible();
  });

  it("corrects a failed relative path using the failing request's parent directory", async () => {
    const directory = { path: "docs", path_id: "docs-id" };
    const files = documentRead({
      "docs/a.md": "# Original guide\n\n[Open missing plan](missing.md)",
      "docs/fixed.md": "# Corrected body",
    });
    files.meta("a.md", Promise.resolve(metadata("docs/a.md", root, undefined, undefined, directory)));
    files.meta("missing.md", Promise.resolve(new Response(null, { status: 404 })));
    files.meta("fixed.md", Promise.resolve(metadata("docs/fixed.md", root, undefined, undefined, directory)));
    const corrected = files.body("docs/fixed.md");
    openReader();
    await openLink("Open missing plan");
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't open missing.md. File not found.");
    fireEvent.click(screen.getByRole("button", { name: "Edit path for missing.md" }));
    const field = screen.getByRole("textbox", { name: "File path" });
    expect(field).toHaveValue("missing.md");
    fireEvent.change(field, { target: { value: "fixed.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    await waitFor(() => expect(files.fileRequests("fixed.md")).toHaveLength(1));
    expect(screen.getByRole("heading", { name: "Original guide" })).toBeVisible();
    const binding = new URL(String(files.fileRequests("fixed.md")[0][0])).searchParams;
    expect(binding.get("source")).toBe(root);
    expect(binding.get("source_directory")).toBe(directory.path);
    expect(binding.get("source_directory_id")).toBe(directory.path_id);
    await act(async () => corrected.resolve(new Response("# Corrected body")));
    expect(await screen.findByRole("heading", { name: "Corrected body" })).toBeVisible();
  });

  it("retries the newest alias using its own source directory after the shared body fails", async () => {
    const targetMeta = () => metadata("target.md");
    const directory = { path: "docs", path_id: "docs-id" };
    const files = documentRead({
      "a.md": "# A\n\n[Read C](c.md)\n\n[First alias](first-alias.md)",
      "docs/c.md": "# C\n\n[Latest alias](latest-alias.md#L3)",
      "target.md": "# Target\n\nRetry location",
    });
    files.meta("c.md", Promise.resolve(metadata("docs/c.md", root, "/workspace/docs/c.md", undefined, directory)));
    files.meta("first-alias.md", Promise.resolve(targetMeta()));
    files.meta("latest-alias.md", Promise.resolve(targetMeta()));
    const target = files.body("target.md");
    openReader();
    await openLink("Read C");
    await screen.findByRole("heading", { name: "C" });
    fireEvent.click(tab("a.md"));
    await openLink("First alias");
    await waitFor(() => expect(files.fileRequests("target.md")).toHaveLength(1));
    fireEvent.click(tab("c.md"));
    await openLink("Latest alias");
    await waitFor(() => expect(screen.queryByText("latest-alias.md loading…")).not.toBeInTheDocument());
    await act(async () => target.resolve(new Response(null, { status: 403 })));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open latest-alias.md.");
    files.meta("latest-alias.md", Promise.resolve(targetMeta()));
    fireEvent.click(screen.getByRole("button", { name: "Retry opening latest-alias.md" }));
    expect(await screen.findByText("Retry location")).toHaveAttribute("data-located", "true");
    const retried = new URL(String(files.fileRequests("latest-alias.md").at(-1)?.[0]));
    expect(files.fileRequests("latest-alias.md")).toHaveLength(2);
    expect(retried.searchParams.get("source")).toBe(root);
    expect(retried.searchParams.get("source_directory")).toBe(directory.path);
    expect(retried.searchParams.get("source_directory_id")).toBe(directory.path_id);
    expect(files.fileRequests("first-alias.md")).toHaveLength(1);
  });

  it.each([true, false])("applies the newest alias position in the background with its metadata first: %s", async (aliasFirst) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md#L3)\n\n[Alias of B](alias.md#L5)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    const alias = deferred<Response>();
    files.meta("alias.md", alias.promise);
    openReader();
    await openLink("Read B");
    await openLink("Alias of B");
    await openLink("Read C");
    await screen.findByRole("heading", { name: "c.md body" });
    const resolveAlias = () => alias.resolve(metadata("b.md"));
    const resolveBody = () => b.resolve(new Response("# B\n\nEarlier line\n\nNewest line"));
    await act(async () => aliasFirst ? resolveAlias() : resolveBody());
    await act(async () => aliasFirst ? resolveBody() : resolveAlias());
    expect(selected()).toBe(tab("c.md"));
    fireEvent.click(tab("b.md"));
    expect(screen.getByText("Newest line")).toHaveAttribute("data-located", "true");
    expect(files.fileRequests("b.md")).toHaveLength(2);
  });

  it("keeps a newer in-document position when an earlier alias finally confirms", async () => {
    const files = documentRead({
      "a.md": "# A\n\n[Read B](b.md)\n\n[Alias of B](alias.md#L3)",
      "b.md": "# B\n\nOld line\n\n[Local reading position](#local)\n\n## Local",
    });
    const alias = deferred<Response>();
    files.meta("alias.md", alias.promise);
    openReader();
    await openLink("Read B");
    await screen.findByRole("link", { name: "Local reading position" });
    fireEvent.click(tab("a.md"));
    await openLink("Alias of B");
    fireEvent.click(tab("b.md"));
    fireEvent.click(screen.getByRole("link", { name: "Local reading position" }));
    await act(async () => alias.resolve(metadata("b.md")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Local" })).toBeVisible();
    expect(screen.queryByText("Old line", { selector: "[data-line]" })).not.toBeInTheDocument();
  });

  it.each(["fails", "merges"] as const)("returns focus to the active tab when a candidate %s", async (outcome) => {
    const files = documentRead({ "a.md": "# A\n\n[Read alias](alias.md)" });
    const alias = deferred<Response>();
    files.meta("alias.md", alias.promise);
    openReader();
    await openLink("Read alias");
    const cancelOpening = screen.getByRole("button", { name: "Cancel opening alias.md" });
    act(() => cancelOpening.focus());
    expect(cancelOpening).toHaveFocus();
    await act(async () => alias.resolve(outcome === "fails" ? new Response(null, { status: 404 }) : metadata("a.md")));
    expect(cancelOpening).not.toBeInTheDocument();
    expect(tab("a.md")).toHaveFocus();
  });

  it("keeps the original text active through image transfer and decoding, including a later text action", async () => {
    const files = imageRead({
      "a.md": "# Original document\n\n[View diagram](diagram.png)\n\n[Read notes](notes.md)",
      "notes.md": "# Notes",
    });
    const receiveImage = files.imageBody();
    openReader();
    await openLink("View diagram");
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(2));
    expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
    await receiveImage();
    const image = screen.getByAltText("diagram.png");
    const decoded = await startImageDecode(image);
    expect(selected()).toBe(tab("a.md"));
    await openLink("Read notes");
    await screen.findByRole("heading", { name: "Notes" });
    await act(async () => decoded.resolve());
    expect(selected()).toBe(tab("notes.md"));
    fireEvent.click(tab("diagram.png"));
    expect(screen.getByRole("img", { name: "diagram.png" })).toBe(image);
    closeTab("diagram.png");
    expect(image).not.toBeInTheDocument();
    expect(files.revoke).toHaveBeenCalledExactlyOnceWith("blob:diagram-1");
  });

  it("aborts a cancelled image read and replaces a slow decode without showing its late result", async () => {
    const files = imageRead({ "a.md": "# Original document\n\n[View diagram](diagram.png)" });
    const receiveCancelled = files.imageBody();
    const receiveRetried = files.imageBody();
    const receiveCurrent = files.imageBody();
    openReader();
    await screen.findByRole("heading", { name: "Original document" });
    await openLink("View diagram");
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(2));
    cancel("diagram.png");
    expect(files.fileRequests("diagram.png")[1][1]?.signal?.aborted).toBe(true);
    await receiveCancelled();
    expect(screen.queryByAltText("diagram.png")).not.toBeInTheDocument();

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: View diagram" })));
      await receiveRetried();
      const retired = screen.getByAltText("diagram.png");
      const staleDecode = await startImageDecode(retired);
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening diagram.png" })));
      expect(retired).not.toBeInTheDocument();
      expect(files.revoke).toHaveBeenCalledExactlyOnceWith("blob:diagram-1");
      await act(async () => staleDecode.reject(new Error("Late decode failure")));
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      await receiveCurrent();
      const current = screen.getByAltText("diagram.png");
      const decoded = await startImageDecode(current);
      await act(async () => decoded.resolve());
      expect(selected()).toBe(tab("diagram.png"));
      expect(screen.getByRole("img", { name: "diagram.png" })).toBe(current);
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(current).not.toBeInTheDocument();
      expect(files.revoke.mock.calls).toEqual([["blob:diagram-1"], ["blob:diagram-2"]]);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["success", "decode failure"] as const)("settles a background image reload only in its own tab (%s)", async (outcome) => {
    const files = imageRead({ "a.md": "# Original document\n\n[View diagram](diagram.png)" });
    const receiveInitial = files.imageBody();
    openReader();
    const original = await screen.findByRole("heading", { name: "Original document" });
    const originalScroll = original.closest<HTMLElement>(".file-viewer-body")!;
    originalScroll.scrollTop = 95;
    await openLink("View diagram");
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(2));
    await receiveInitial();
    const oldImage = screen.getByAltText("diagram.png");
    const initialDecode = await startImageDecode(oldImage);
    await act(async () => initialDecode.resolve());
    expect(selected()).toBe(tab("diagram.png"));

    const receiveReload = files.imageBody();
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(4));
    await receiveReload();
    const [, replacement] = screen.getAllByAltText("diagram.png");
    const decoded = await startImageDecode(replacement);
    expect(screen.getByRole("img", { name: "diagram.png" })).toBe(oldImage);
    expect(files.revoke).not.toHaveBeenCalled();
    fireEvent.click(tab("a.md"));
    await act(async () => {
      if (outcome === "success") decoded.resolve();
      else decoded.reject(new Error("Invalid image"));
    });
    expect(selected()).toBe(tab("a.md"));
    expect(original).toBeVisible();
    expect(originalScroll.scrollTop).toBe(95);
    expect(oldImage).not.toBeInTheDocument();
    expect(files.revoke).toHaveBeenCalledWith("blob:diagram-1");

    fireEvent.click(tab("diagram.png"));
    if (outcome === "success") {
      expect(screen.getByRole("img", { name: "diagram.png" })).toBe(replacement);
      expect(files.revoke).toHaveBeenCalledTimes(1);
    } else {
      expect(replacement).not.toBeInTheDocument();
      expect(files.revoke).toHaveBeenCalledWith("blob:diagram-2");
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load the preview.");
      expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    }
  });

  it.each(["metadata", "bytes"])("retries slow PDF %s while the original text remains readable", async (stage) => {
    const files = documentRead({ "a.md": "# Original document\n\n[Read PDF](paper.pdf)" });
    const pdf = deferred<Response>();
    files.meta("paper.pdf", pdf.promise);
    files.body("paper.pdf");
    files.body("paper.pdf");
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read PDF" });
    const pdfMeta = () => metadata("paper.pdf", root, undefined, undefined, undefined, "pdf");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(link));
      if (stage === "bytes") await act(async () => pdf.resolve(pdfMeta()));
      const firstFrame = document.querySelector("iframe");
      if (stage === "bytes") expect(firstFrame?.closest("[inert]")).not.toBeNull();
      else expect(firstFrame).toBeNull();
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta("paper.pdf", Promise.resolve(pdfMeta()));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening paper.pdf" })));
      const requests = files.fileRequests("paper.pdf");
      expect(requests).toHaveLength(stage === "bytes" ? 4 : 3);
      expect(requests[stage === "bytes" ? 1 : 0][1]?.signal?.aborted).toBe(true);
      if (firstFrame) expect(firstFrame).not.toBeInTheDocument();
      expect(document.querySelector("iframe")).not.toBeNull();
      await act(async () => pdf.resolve(pdfMeta()));
      expect(files.fileRequests("paper.pdf")).toHaveLength(requests.length);
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      expect(selected()).toBe(tab("a.md"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries a slow PDF reload from another tab without changing its pending opening", async () => {
    const files = documentRead({ "a.md": "# Original guide\n\n[Read PDF](paper.pdf)\n\n[Read later](later.md)" });
    const pdfMeta = () => metadata("paper.pdf", root, undefined, undefined, undefined, "pdf");
    files.meta("paper.pdf", pdfMeta);
    openReader();
    await openLink("Read PDF");
    const pdf = await screen.findByRole("tab", { name: "paper.pdf", selected: true });
    const staleMeta = deferred<Response>();
    files.meta("paper.pdf", staleMeta.promise);
    const later = files.body("later.md");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reload file" })));
      fireEvent.click(tab("a.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Read later" })));
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta("paper.pdf", pdfMeta);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry reloading paper.pdf" })));
      expect(files.fileRequests("paper.pdf")[2][1]?.signal?.aborted).toBe(true);
      await act(async () => staleMeta.resolve(pdfMeta()));
      expect(files.fileRequests("paper.pdf")).toHaveLength(5);
      expect(screen.getByRole("heading", { name: "Original guide" })).toBeVisible();
      expect(selected()).toBe(tab("a.md"));
      await act(async () => later.resolve(new Response("# Later text")));
      expect(selected()).toBe(tab("later.md"));
      fireEvent.click(pdf);
      expect(screen.getByTitle("PDF preview")).not.toHaveAttribute("inert");
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["draws", "fails"] as const)("only commits the latest PDF reload after first-page drawing (%s)", async (result) => {
    const files = documentRead({ "a.md": "# Original guide\n\n[Read PDF](paper.pdf)" });
    files.meta("paper.pdf", () => metadata("paper.pdf", root, undefined, undefined, undefined, "pdf"));
    const initialBytes = files.body("paper.pdf");
    const staleBytes = files.body("paper.pdf");
    const currentBytes = files.body("paper.pdf");
    const firstPaint = deferred<void>();
    const stalePaint = deferred<void>();
    const currentPaint = deferred<void>();
    const cancelStale = vi.fn();
    pdfRenderer.paintPdfPage
      .mockImplementationOnce(() => ({ promise: firstPaint.promise, cancel: () => {} }))
      .mockImplementationOnce(() => ({ promise: stalePaint.promise, cancel: cancelStale }))
      .mockImplementationOnce(() => ({ promise: currentPaint.promise, cancel: () => {} }));
    openReader();
    const guide = await screen.findByRole("heading", { name: "Original guide" });
    const scroll = guide.closest<HTMLElement>(".file-viewer-body")!;
    scroll.scrollTop = 100;
    await openLink("Read PDF");
    await act(async () => initialBytes.resolve(new Response("First PDF")));
    await waitFor(() => expect(pdfRenderer.paintPdfPage).toHaveBeenCalledTimes(1));
    const initialFrame = screen.getByTitle("PDF preview");
    expect(initialFrame).toHaveAttribute("inert");
    expect(selected()).toBe(tab("a.md"));
    fireEvent.click(tab("a.md"));
    await act(async () => firstPaint.resolve());
    expect(selected()).toBe(tab("a.md"));
    fireEvent.click(tab("paper.pdf"));
    expect(screen.getByTitle("PDF preview")).toBe(initialFrame);

    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => staleBytes.resolve(new Response("Former reload")));
    await waitFor(() => expect(pdfRenderer.paintPdfPage).toHaveBeenCalledTimes(2));
    const [, staleFrame] = screen.getAllByTitle("PDF preview");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => currentBytes.resolve(new Response("Latest reload")));
    await waitFor(() => expect(pdfRenderer.paintPdfPage).toHaveBeenCalledTimes(3));
    const [, currentFrame] = screen.getAllByTitle("PDF preview");
    expect(initialFrame).toBeInTheDocument();
    expect(staleFrame).not.toBeInTheDocument();
    expect(cancelStale).toHaveBeenCalledOnce();
    await act(async () => stalePaint.resolve());
    expect(initialFrame).toBeInTheDocument();
    fireEvent.click(tab("a.md"));
    await act(async () => {
      if (result === "draws") currentPaint.resolve();
      else currentPaint.reject(new Error("Invalid PDF"));
    });
    expect(selected()).toBe(tab("a.md"));
    expect(guide).toBeVisible();
    expect(scroll.scrollTop).toBe(100);
    expect(initialFrame).not.toBeInTheDocument();
    fireEvent.click(tab("paper.pdf"));
    if (result === "draws") {
      expect(screen.getByTitle("PDF preview")).toBe(currentFrame);
      expect(currentFrame).not.toHaveAttribute("inert");
    } else {
      expect(currentFrame).not.toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load the preview.");
      expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    }
  });

  it.each(["missing", "missing after origin closes", "repointed"] as const)(
    "isolates the retry from the previous shared PDF read when the alias is %s", async (outcome) => {
      const files = documentRead({
        "a.md": "# A\n\n[Open C](c.md)\n\n[Open P](p.pdf)",
        "c.md": "# C\n\n[Open Q](q.pdf)",
      });
      const paper = (path: string) => metadata(path, root, undefined, undefined, undefined, "pdf");
      const olderP = deferred<Response>();
      openReader();
      await openLink("Open C");
      await screen.findByRole("heading", { name: "C" });
      fireEvent.click(tab("a.md"));
      if (outcome === "repointed") {
        files.meta("p.pdf", olderP.promise);
        await openLink("Open P");
        await waitFor(() => expect(files.fileRequests("p.pdf")).toHaveLength(1));
      }
      files.meta("p.pdf", Promise.resolve(paper("paper.pdf")));
      files.meta("q.pdf", Promise.resolve(paper("paper.pdf")));
      files.body("paper.pdf");

      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open P" })));
        const original = document.querySelector("iframe");
        expect(original).toBeInTheDocument();
        fireEvent.click(tab("c.md"));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open Q" })));
        expect(document.querySelectorAll("iframe")).toHaveLength(1);
        await act(async () => vi.advanceTimersByTime(5000));
        const retried = deferred<Response>();
        files.meta("q.pdf", retried.promise);
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening paper.pdf" })));
        expect(original).not.toBeInTheDocument();
        expect(files.fileRequests("q.pdf")).toHaveLength(2);

        if (outcome === "repointed") {
          const file = files.body("replacement.pdf");
          await act(async () => retried.resolve(paper("replacement.pdf")));
          const replacement = document.querySelector("iframe")!;
          expect(replacement).toBeInTheDocument();
          expect(files.fileRequests("replacement.pdf")).toHaveLength(1);
          await act(async () => file.resolve(new Response("replacement PDF")));
          vi.useRealTimers();
          await screen.findByRole("tab", { name: "replacement.pdf" });
          expect(selected()).toBe(tab("replacement.pdf"));
          await act(async () => olderP.resolve(new Response(null, { status: 404 })));
          fireEvent.click(tab("a.md"));
          expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open p.pdf. File not found.");
          expect(screen.getByRole("button", { name: "Retry opening p.pdf" })).toBeVisible();
          expect(files.fileRequests("p.pdf")).toHaveLength(2);
        } else {
          if (outcome === "missing after origin closes") closeTab("c.md");
          await act(async () => retried.resolve(new Response(null, { status: 404 })));
          if (outcome === "missing") {
            expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open q.pdf. File not found.");
            fireEvent.click(tab("a.md"));
          }
          expect(screen.getByRole("heading", { name: "A" })).toBeVisible();
          expect(screen.queryByRole("alert")).not.toBeInTheDocument();
          expect(files.fileRequests("p.pdf")).toHaveLength(1);
        }
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("keeps identical canonical paths from distinct sources in separate tabs", async () => {
    const files = documentRead({ "a.md": "# Root copy", "chosen.md": "# Nested source copy" });
    files.meta("a.md", Promise.resolve(metadata("a.md", root, `${root}/shared/a.md`)));
    files.meta("picked.md", Promise.resolve(metadata("chosen.md", `${root}/shared`, `${root}/shared/a.md`)));
    openReader();
    await screen.findByRole("heading", { name: "Root copy" });
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Source" }), { target: { value: `${root}/shared` } });
    fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
    expect(await screen.findByRole("heading", { name: "Nested source copy" })).toBeVisible();
    expect(screen.getAllByRole("tab", { name: "a.md" })).toHaveLength(2);
    expect(new URL(String(files.fileRequests("chosen.md")[0][0])).searchParams.get("source")).toBe(`${root}/shared`);
  });

  it("counts pending tabs toward capacity and still lets users reuse a confirmed file in place", async () => {
    const names = ["b", "c", "d", "e", "f", "g", "h", "i"];
    const links = names.map((name) => `[Read ${name}](${name}.md)`);
    const files = documentRead({ "a.md": [...links, "[Read B alias](b-alias.md)"].join("\n\n") });
    files.meta("b-alias.md", Promise.resolve(metadata("b.md")));
    files.body("h.md");
    openReader();
    for (const name of names.slice(0, 6)) {
      await openLink(name === "b" ? "Read B alias" : `Read ${name}`);
      await screen.findByRole("heading", { name: `${name}.md body` });
      fireEvent.click(tab("a.md"));
    }
    await openLink("Read h");
    await openLink("Read i");
    expect(screen.getByText("Close a file tab or cancel an opening to make room.")).toBeVisible();
    expect(files.fileRequests("i.md")).toHaveLength(0);
    await openLink("Read B alias");
    expect(selected()).toBe(tab("a.md"));
    expect(files.fileRequests("b-alias.md")).toHaveLength(1);
    await openLink("Read b");
    await waitFor(() => expect(selected()).toBe(tab("b.md")));
    expect(files.fileRequests("b.md")).toHaveLength(2);
    expect(within(screen.getByRole("tablist")).getAllByRole("tab").map((entry) => entry.textContent)).toEqual(
      ["a.md", ...names.slice(0, 6).map((name) => `${name}.md`)],
    );
    expect(screen.getByRole("button", { name: "Cancel opening h.md" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(within(await screen.findByRole("list")).getByRole("button", { name: "a.md" }));
    await waitFor(() => expect(selected()).toBe(tab("a.md")));
    expect(files.fileRequests("a.md")).toHaveLength(3);
    expect(new URL(String(files.fileRequests("a.md")[2][0])).pathname).toMatch(/\/meta$/);
    expect(screen.getByRole("button", { name: "Cancel opening h.md" })).toBeVisible();
  }, 15_000);

  it.each(["close", "leave", "switch", "account", "delete"] as const)("retires the text response when users %s the originating context", async (exit) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)" });
    const b = files.body("b.md");
    openReader();
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    const oldToken = localStorage.getItem("octopus_token");
    try {
      act(() => {
        const store = useSessionStore.getState();
        switch (exit) {
          case "close": store.closeViewer(); break;
          case "leave": store.openManage("account"); break;
          case "switch": store.setActiveSessionId("second"); break;
          case "account": store.setToken("tok-new"); break;
          case "delete": store.forgetSession("original"); break;
        }
      });
      await act(async () => b.resolve(new Response("# Retired response")));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(files.fileRequests("b.md")[1][1]?.signal?.aborted).toBe(true);
    } finally {
      if (oldToken === null) localStorage.removeItem("octopus_token");
      else localStorage.setItem("octopus_token", oldToken);
    }
  });
});
