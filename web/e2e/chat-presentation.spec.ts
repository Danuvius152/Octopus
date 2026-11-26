import { backendCatalogFixture } from "../src/test-support/backendCatalog";
import { expect, test, type WebSocketRoute } from "./test";
import { accountToken, enterAccountCredentials, openAsCurrentAccount } from "./current-account";
import { isEmptyChatCollection, mockChatHttp, mockChatTest, mockChatSocket } from "./mock-chat";
import type { Message } from "../src/stores/sessionStore";
import { capturePaint, type PaintFrame } from "./paint-capture";
import { writeFile } from "node:fs/promises";

test("cold presentation keeps the paused transcript tail still from first reveal", async ({ page, request }) => {
  const api = "http://localhost:8765/api";
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const importSession = async (name: string, count: number) => {
    const messages = Array.from({ length: count }, (_, index) => ({
      role: index % 6 === 0 ? "user" : "assistant",
      type: "text",
      content: `${name} row ${index + 1}: A saved paragraph for reading and scroll measurements. ` +
        "The words and line breaks repeat at the same width in each revision.",
    }));
    const response = await request.post(`${api}/sessions/import`, { headers, data: { name, messages } });
    expect(response.ok()).toBe(true);
    return (await response.json()).id as string;
  };
  await importSession("Original chat", 1);
  const target = await importSession("Paused chat", 240);
  await page.route((url) => url.pathname === `/api/sessions/${target}`, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: { ...detail, dispatch_state: { ...detail.dispatch_state, paused: true } } });
  });
  await page.setViewportSize({ width: 1280, height: 800 });
  await openAsCurrentAccount(page);
  await expect(page.locator(".conn-status.on")).toBeVisible();
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.addStyleTag({ content: ".chat-messages { zoom: 1.1; } .msg-content { font: 14.5px/1.47 Arial, sans-serif; }" });
  await page.locator(".session-item", { hasText: "Original chat" }).click();
  await expect(page.locator(".chat-messages").getByText(/^Original chat row 1:/)).toBeVisible();
  await page.evaluate(async () => {
    await document.fonts.load("14.5px Arial");
    await document.fonts.ready;
  });

  const recorder = await page.evaluateHandle((id) => {
    let frame = 0;
    const offsets: (number | null)[] = [];
    const sample = () => {
      const viewport = document.querySelector(`[data-chat-session="${id}"] .chat-messages`);
      if (viewport && getComputedStyle(viewport).visibility === "visible") {
        const top = viewport.getBoundingClientRect().top;
        const height = viewport.querySelector('[data-viewport-type="element"]')?.getBoundingClientRect().height;
        const tail = Array.from(viewport.querySelectorAll(".msg-content p"))
          .find((row) => row.textContent?.startsWith("Paused chat row 240:"))?.getBoundingClientRect();
        offsets.push(height && tail && tail.top >= top && tail.bottom <= top + height + 1 ? tail.top : null);
      } else if (offsets.length) {
        offsets.push(null);
      }
      frame = requestAnimationFrame(sample);
    };
    frame = requestAnimationFrame(sample);
    return {
      count: () => offsets.length,
      stop: () => { cancelAnimationFrame(frame); return offsets; },
    };
  }, target);
  const viewport = page.locator(`[data-chat-session="${target}"] .chat-messages`);
  let offsets: (number | null)[];
  try {
    await page.locator(".session-item", { hasText: "Paused chat" }).click();
    await expect.poll(() => recorder.evaluate((value) => value.count())).toBeGreaterThanOrEqual(8);
  } finally {
    offsets = await recorder.evaluate((value) => value.stop());
    await recorder.dispose();
  }
  expect(offsets, "the same last paragraph is readable in every frame from reveal").not.toContain(null);
  const measured = offsets.filter((offset): offset is number => offset !== null);
  expect(Math.max(...measured) - Math.min(...measured)).toBeLessThanOrEqual(2);

  const box = (await viewport.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -270);
  await expect.poll(() => viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeGreaterThan(100);
  await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
});

const performanceRun = process.env.OCTOPUS_E2E_CHAT_PERFORMANCE === "1";
const warmRounds = performanceRun ? 10 : 1;

for (const target of ["Other parent", "Vera child"]) {
  for (const streaming of [false, true]) {
    mockChatTest(`warm presentation to ${target} with ${streaming ? "continuous" : "paused"} output`, async ({ page }, info) => {
      if (performanceRun) mockChatTest.setTimeout(120_000);
      const checkWarmPresentation = async (paintFrames: PaintFrame[]) => {
        let outputTimer: ReturnType<typeof setInterval> | undefined;
        await page.setViewportSize({ width: 1280, height: 900 });
        const names = ["Original parent", "Other parent", "Vera child"];
        const sessions = names.map((name, index) => ({ id: `presentation-${index}`, name, agent_id: "presentation-agent",
          backend: "codex", status: "running", archived: false, working_dir: "/tmp", created_at: "2026-11-12T00:00:00Z",
          ...(index === 2 ? { origin: "delegation", parent_session_id: "presentation-0" } : {}) }));
        const sessionIds = sessions.map(({ id }) => id);
        const messages = (name: string): Message[] => Array.from({ length: 240 }, (_, index) => ({
          role: "assistant", type: "text", seq: index + 1,
          content: `${name} row ${index + 1}\n\n` +
            "A saved paragraph with enough text to wrap naturally in the current viewport.\n\n".repeat(index === 239 ? 30 : index % 7 + 1) +
            (index === 239 ? "### Latest saved output" : "Another saved paragraph."),
        }));
        let socket!: WebSocketRoute;
        await mockChatHttp(page, (url) => url.pathname.startsWith("/api/"), async (route) => {
          const request = route.request();
          const path = new URL(request.url()).pathname;
          let data: unknown;
          if (request.method() === "POST" && path === "/api/auth/login") data = { token: "presentation-token" };
          else if (request.method() !== "GET") return route.fallback();
          else if (path === "/api/auth/state") data = { accounts_exist: true };
          else if (path === "/api/auth/identity") data = { user_id: "presentation-user", label: "Reader", is_admin: false };
          else if (path === "/api/agents") data = [{ id: "presentation-agent", name: "Octo", backend: "codex", pinned: true, is_system: true }];
          else if (path === "/api/backends") data = backendCatalogFixture(["codex"], "codex");
          else if (path === "/api/sessions") data = sessions;
          else if (isEmptyChatCollection(path, sessionIds)) data = [];
          else if (sessions.some(({ id }) => path === `/api/sessions/${id}/submissions`)) data = { items: [], next_cursor: null };
          else {
            const session = sessions.find((session) => path === `/api/sessions/${session.id}`);
            if (session) data = { ...session, messages: messages(session.name), next_message_seq: 241,
              oldest_loaded_seq: 1, has_more_messages: false, pending_questions: [], subagents: [],
              active_attempt: { request_id: "request", attempt_id: "attempt" },
              dispatch_state: { generation: "presentation-service", revision: 1, paused: false,
                active_request_id: "request", active_operation: "message", items: [] } };
          }
          return data === undefined ? route.fallback() : route.fulfill({ json: data });
        });
        await mockChatSocket(page, "presentation-token", (ws) => { socket = ws; });
        await page.goto("/");
        await enterAccountCredentials(page, { username: "reader", password: "mock-password" });
        await page.locator(".agent-item", { hasText: "Octo" }).click();
        await page.getByTitle("Show delegation sessions").click();
        const scroller = page.locator(".chat-messages");
        const settle = async (outputContinues = false) => {
          if (!outputContinues) await expect.poll(() => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(2);
          await page.evaluate(async () => {
            for (let index = 0; index < 12; index++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
          });
        };
        for (const name of ["Other parent", "Vera child", "Original parent"]) {
          await page.locator(".session-item", { hasText: name }).click();
          await expect(scroller.getByRole("heading", { name: "Latest saved output" })).toBeInViewport();
          await settle();
        }
        await page.evaluate(() => document.fonts.ready);
        await expect(page.locator(".chat-header")).toContainText("Original parent");
        const recorder = await page.evaluateHandle(() => {
          type Sample = { at: number; owner: string; visible: boolean; gap: number; preparing: boolean; rows: string[];
            extent: number; height: number; footerHeight: number; footerLength: number; footerHash: number; liveLength: number;
            rowPositions: Record<string, number>; latestOffset: number | null; tailInView: boolean };
          const samples: Sample[] = [];
          const reveals: Sample[] = [];
          let clickAt = 0;
          const clicked = (event: Event) => {
            if ((event.target as Element)?.closest(".session-item")) clickAt = performance.now();
          };
          document.addEventListener("click", clicked, true);
          let frame = 0;
          const snapshot = (): Sample => {
            const element = document.querySelector<HTMLElement>(".chat-messages");
            const bounds = element?.getBoundingClientRect();
            const latest = Array.from(element?.querySelectorAll("h3") ?? []).find((heading) => heading.textContent === "Latest saved output");
            const marker = latest?.getBoundingClientRect();
            const footer = element?.querySelector("[data-chat-footer]");
            const footerText = footer?.parentElement?.textContent ?? "";
            const streamingText = footer?.querySelector(".msg-streaming-text");
            const liveText = streamingText?.lastChild;
            let tail = marker;
            if (liveText instanceof Text && liveText.data.trimEnd()) {
              const end = liveText.data.trimEnd().length;
              const range = document.createRange();
              range.setStart(liveText, end - 1);
              range.setEnd(liveText, end);
              tail = range.getBoundingClientRect();
            }
            // An idle/running spinner is not first visible transcript content.
            const rows = element ? Array.from(element.querySelectorAll<HTMLElement>("[data-message-key], .msg-streaming"))
              .filter((row) => {
                const box = row.getBoundingClientRect();
                return bounds && box.bottom > bounds.top && box.top < bounds.bottom;
              }).map((row) => row.dataset.messageKey ?? "footer") : [];
            return { at: performance.now(), owner: document.querySelector(".chat-header")?.textContent ?? "",
              visible: !!element?.clientHeight && getComputedStyle(element).visibility === "visible" && rows.length > 0,
              gap: element ? element.scrollHeight - element.clientHeight - element.scrollTop : 0,
              extent: element?.scrollHeight ?? 0, height: element?.clientHeight ?? 0,
              footerHeight: element?.querySelector("[data-chat-footer]")?.parentElement?.getBoundingClientRect().height ?? 0,
              footerLength: footerText.length, footerHash: Array.from(footerText).reduce((hash, char) => Math.imul(hash ^ char.charCodeAt(0), 16777619), 2166136261),
              liveLength: streamingText?.textContent?.length ?? 0,
              rowPositions: Object.fromEntries(Array.from(element?.querySelectorAll<HTMLElement>("[data-message-key]") ?? [])
                .map((row) => [row.dataset.messageKey!, row.getBoundingClientRect().top - (bounds?.top ?? 0) + (element?.scrollTop ?? 0)])),
              preparing: !!document.querySelector("[data-chat-preparation]"), rows,
              tailInView: !!bounds && !!tail && tail.top >= bounds.top && tail.bottom <= bounds.bottom + 1,
              latestOffset: bounds && marker && marker.top >= bounds.top && marker.bottom <= bounds.bottom + 1
                ? marker.top - bounds.top : null };
          };
          // Separate the DOM reveal from later frame sampling: output or another
          // layout commit may arrive between them.
          let observed: Element | null = null;
          let visible = false;
          const observer = new MutationObserver(() => {
            const element = document.querySelector(".chat-messages");
            if (element !== observed) { observed = element; visible = false; }
            if (!element || getComputedStyle(element).visibility !== "visible") { visible = false; return; }
            if (visible) return;
            const next = snapshot();
            if (next.visible) {
              reveals.push(next);
              visible = true;
            }
          });
          observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["style"] });
          const sample = () => {
            samples.push(snapshot());
            frame = requestAnimationFrame(sample);
          };
          sample();
          return { begin: () => { samples.length = 0; reveals.length = 0; clickAt = 0; },
            read: () => ({ clickAt, samples: [...samples], reveals: [...reveals] }),
            stop: () => { observer.disconnect(); cancelAnimationFrame(frame); document.removeEventListener("click", clicked, true); } };
        });
        try {
          const results: { destination: string; latency: number; preparationFrames: number; firstGap: number; samples: unknown;
            revealLatency: number | null; revealGap: number | null; footerGrowth: number; viewportLoss: number;
            historyExtentChange: number | null; latestMovement: number | null;
            firstTailInView: boolean; revealTailInView: boolean; liveAdvanced: boolean }[] = [];
          const historyChanges: { destination: string; at: number; extent: number; movedRows: string[] }[] = [];
          if (streaming) {
            outputTimer = setInterval(() => {
              for (const session of sessions) socket.send(JSON.stringify({ type: "assistant_delta", session_id: session.id,
                content: "Continuing output while switching.\n" }));
            }, 40);
            await expect(scroller.getByTestId("streaming-text")).toContainText("Continuing output while switching.");
          }
          for (let round = 0; round < warmRounds; round++) for (const destination of [target, "Original parent"]) {
            await recorder.evaluate((record) => record.begin());
            await page.locator(".session-item", { hasText: destination }).click();
            await expect.poll(async () => {
              const record = await recorder.evaluate((record) => record.read());
              return record.samples.some((sample) => sample.at >= record.clickAt && sample.owner.includes(destination) && sample.visible);
            }, { message: "the selected warm transcript becomes visible" }).toBe(true);
            await settle(streaming);
            const record = await recorder.evaluate((record) => record.read());
            const owned = record.samples.filter((sample) => sample.at >= record.clickAt && sample.owner.includes(destination));
            const first = owned.find((sample) => sample.visible);
            expect(first, "the selected warm transcript becomes visible").toBeDefined();
            const reveal = record.reveals.find((sample) => sample.at >= record.clickAt && sample.owner.includes(destination) && sample.visible);
            if (reveal) for (const sample of owned.filter((sample) => sample.at >= reveal.at && sample.visible)) {
              const extent = sample.extent - sample.footerHeight - (reveal.extent - reveal.footerHeight);
              const movedRows = Object.entries(sample.rowPositions).filter(([key, top]) => key in reveal.rowPositions &&
                Math.abs(top - reveal.rowPositions[key]) > 1).map(([key]) => key);
              if (Math.abs(extent) > 1 || movedRows.length) historyChanges.push({ destination, at: sample.at, extent, movedRows });
            }
            results.push({ destination, latency: first!.at - record.clickAt,
              revealLatency: reveal ? reveal.at - record.clickAt : null, revealGap: reveal?.gap ?? null,
              firstTailInView: first!.tailInView, revealTailInView: reveal?.tailInView ?? false,
              liveAdvanced: owned.some((sample) => sample.visible && sample.liveLength > first!.liveLength),
              footerGrowth: reveal ? Math.max(0, first!.footerHeight - reveal.footerHeight) : 0,
              viewportLoss: reveal ? Math.max(0, reveal.height - first!.height) : 0,
              historyExtentChange: reveal ? first!.extent - first!.footerHeight - (reveal.extent - reveal.footerHeight) : null,
              latestMovement: first?.latestOffset == null ? null : Math.max(...owned.filter((sample) => sample.visible)
                .map((sample) => sample.latestOffset === null ? Infinity : Math.abs(sample.latestOffset - first.latestOffset!))),
              preparationFrames: owned.filter((sample) => sample.preparing).length, firstGap: first!.gap, samples: record });
          }
          const latencies = results.map((result) => result.latency).sort((a, b) => a - b);
          const report = { rounds: warmRounds, target, streaming,
            viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1,
            timeOrigin: await page.evaluate(() => performance.timeOrigin), paintFrames, historyChanges,
            ...(performanceRun && { median: latencies[Math.ceil(latencies.length * 0.5) - 1],
              p95: latencies[Math.ceil(latencies.length * 0.95) - 1] }), results };
          clearInterval(outputTimer);
          await page.locator(".session-item", { hasText: target }).click();
          const viewport = (await scroller.boundingBox())!;
          await page.mouse.move(viewport.x + viewport.width / 2, viewport.y + viewport.height / 2);
          await expect(scroller).toBeVisible();
          const gap = () => scroller.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop);
          const beforeWheel = await gap();
          await page.mouse.wheel(0, -420);
          try {
            // Rows measured above the reader can change scrollTop even when the
            // reader moves through the expected amount of visible content.
            await expect.poll(gap, { message: "the first upward wheel leaves the tail" }).toBeGreaterThan(beforeWheel + 120);
          } finally {
            const path = info.outputPath("warm-presentation.json");
            await writeFile(path, JSON.stringify({ ...report, wheel: { before: beforeWheel, after: await gap() } }));
            await info.attach("warm-presentation.json", { path, contentType: "application/json" });
          }
          expect(results.map((result) => result.preparationFrames), "warm views show no preparation notice").toEqual(results.map(() => 0));
          expect(results.filter((result) => result.revealGap === null || result.revealGap > 1)
            .map(({ destination, revealGap }) => ({ destination, revealGap })), "DOM reveal is already positioned").toEqual([]);
          expect(results.filter((result) => !result.firstTailInView || !result.revealTailInView)
            .map(({ destination, firstGap, revealGap }) => ({ destination, firstGap, revealGap })),
          "the current last line fits in the first visible viewport").toEqual([]);
          if (streaming) expect(results.filter((result) => !result.liveAdvanced).map(({ destination }) => destination),
            "the visible transcript receives more live text on each switch").toEqual([]);
          // Estimates above the reader may still change. Measure the readable
          // text itself in the viewport so those changes are only diagnostic.
          expect(results.filter((result) => !streaming && (result.firstGap > 1 || (result.latestMovement ?? Infinity) > 2))
            .map(({ destination, firstGap, latestMovement }) =>
              ({ destination, firstGap, latestMovement })), "the same paused tail text remains in place").toEqual([]);
        } finally {
          clearInterval(outputTimer);
          try { await recorder.evaluate((record) => record.stop()); }
          finally { await recorder.dispose(); }
        }
      };
      if (performanceRun) await capturePaint(page, info, checkWarmPresentation);
      else await checkWarmPresentation([]);
    });
  }
}
