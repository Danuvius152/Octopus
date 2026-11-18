import { expect, test, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";
import { capturePaint } from "./paint-capture";
import { writeFile } from "node:fs/promises";

// The same fixture is run against the source revision before preparation is
// implemented. Compositor frames are paint evidence; rAF describes geometry.
for (const target of ["Other parent", "Vera child"]) {
  for (const streaming of [false, true]) {
    test(`warm presentation to ${target} with ${streaming ? "continuous" : "paused"} output`, async ({ page }, info) => {
      test.setTimeout(120_000);
      await capturePaint(page, info, async (paintFrames, onCleanup) => {
        let outputTimer: ReturnType<typeof setInterval> | undefined;
        onCleanup(() => clearInterval(outputTimer));
        await page.setViewportSize({ width: 1280, height: 900 });
        const names = ["Original parent", "Other parent", "Vera child"];
        const sessions = names.map((name, index) => ({ id: `presentation-${index}`, name, agent_id: "presentation-agent",
          backend: "codex", status: "running", archived: false, working_dir: "/tmp", created_at: "2026-11-12T00:00:00Z",
          ...(index === 2 ? { origin: "delegation", parent_session_id: "presentation-0" } : {}) }));
        const messages = (name: string): Message[] => Array.from({ length: 240 }, (_, index) => ({
          role: "assistant", type: "text", seq: index + 1,
          content: `${name} row ${index + 1}\n\n` +
            "A saved paragraph with enough text to wrap naturally in the current viewport.\n\n".repeat(index === 239 ? 30 : index % 7 + 1) +
            (index === 239 ? "### Latest saved output" : "Another saved paragraph."),
        }));
        let socket!: WebSocketRoute;
        await page.route("**/api/**", async (route) => {
          const path = new URL(route.request().url()).pathname;
          if (!path.startsWith("/api/")) { await route.continue(); return; }
          let data: unknown = [];
          if (path === "/api/auth/state") data = { accounts_exist: true };
          else if (path === "/api/auth/login") data = { token: "presentation-token" };
          else if (path === "/api/auth/identity") data = { user_id: "presentation-user", label: "Reader", is_admin: false };
          else if (path === "/api/agents") data = [{ id: "presentation-agent", name: "Octo", backend: "codex", pinned: true, is_system: true }];
          else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
          else if (path === "/api/sessions") data = sessions;
          else if (path.endsWith("/submissions")) data = { items: [], next_cursor: null };
          else {
            const session = sessions.find((session) => path === `/api/sessions/${session.id}`);
            if (session) data = { ...session, messages: messages(session.name), next_message_seq: 241,
              oldest_loaded_seq: 1, has_more_messages: false, pending_questions: [], subagents: [],
              active_attempt: { request_id: "request", attempt_id: "attempt" },
              dispatch_state: { generation: "presentation-service", revision: 1, paused: false,
                active_request_id: "request", active_operation: "message", items: [] } };
          }
          await route.fulfill({ json: data });
        });
        await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
        await page.goto("/");
        await enterAccountCredentials(page);
        await page.locator(".agent-item", { hasText: "Octo" }).click();
        await page.getByTitle("Show delegation sessions").click();
        const scroller = page.locator(".chat-messages");
        const settle = async (outputContinues = false) => {
          if (!outputContinues) await expect.poll(() => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(2);
          await page.evaluate(async () => {
            for (let index = 0; index < 12; index++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
          });
        };
        for (const name of names) {
          await page.locator(".session-item", { hasText: name }).click();
          await expect(scroller.getByRole("heading", { name: "Latest saved output" })).toBeInViewport();
          await settle();
        }
        await page.evaluate(() => document.fonts.ready);
        const recorder = await page.evaluateHandle(() => {
          type Sample = { at: number; owner: string; visible: boolean; gap: number; preparing: boolean; rows: string[];
            extent: number; height: number; footerHeight: number; footerLength: number; footerHash: number;
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
            const liveText = footer?.querySelector(".msg-streaming-text")?.lastChild;
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
              rowPositions: Object.fromEntries(Array.from(element?.querySelectorAll<HTMLElement>("[data-message-key]") ?? [])
                .map((row) => [row.dataset.messageKey!, row.getBoundingClientRect().top - (bounds?.top ?? 0) + (element?.scrollTop ?? 0)])),
              preparing: !!document.querySelector("[data-chat-preparation]"), rows,
              tailInView: !!bounds && !!tail && tail.top >= bounds.top && tail.bottom <= bounds.bottom + 1,
              latestOffset: bounds && marker && marker.top >= bounds.top && marker.bottom <= bounds.bottom + 1
                ? marker.top - bounds.top : null };
          };
          // Separate the DOM reveal from later frame sampling: output or another
          // layout commit may arrive between them. Paint evidence stays in trace.
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
        onCleanup(() => recorder.evaluate((record) => record.stop()));
        onCleanup(() => recorder.dispose());
        const results: { destination: string; latency: number; preparationFrames: number; firstGap: number; samples: unknown;
          revealLatency: number | null; revealGap: number | null; footerGrowth: number; viewportLoss: number;
          historyExtentChange: number | null; latestMovement: number | null;
          firstTailInView: boolean; revealTailInView: boolean }[] = [];
        const historyChanges: { destination: string; at: number; extent: number; movedRows: string[] }[] = [];
        if (streaming) outputTimer = setInterval(() => {
          for (const session of sessions) socket.send(JSON.stringify({ type: "assistant_delta", session_id: session.id,
            content: "Continuing output while switching.\n" }));
        }, 40);
        for (let round = 0; round < 10; round++) for (const destination of ["Original parent", target]) {
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
            footerGrowth: reveal ? Math.max(0, first!.footerHeight - reveal.footerHeight) : 0,
            viewportLoss: reveal ? Math.max(0, reveal.height - first!.height) : 0,
            historyExtentChange: reveal ? first!.extent - first!.footerHeight - (reveal.extent - reveal.footerHeight) : null,
            latestMovement: first?.latestOffset == null ? null : Math.max(...owned.filter((sample) => sample.visible)
              .map((sample) => sample.latestOffset === null ? Infinity : Math.abs(sample.latestOffset - first.latestOffset!))),
            preparationFrames: owned.filter((sample) => sample.preparing).length, firstGap: first!.gap, samples: record });
        }
        const latencies = results.map((result) => result.latency).sort((a, b) => a - b);
        const report = { baseline: process.env.OCTOPUS_CHAT_BASELINE === "1", target, streaming,
          viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1,
          timeOrigin: await page.evaluate(() => performance.timeOrigin), paintFrames, historyChanges,
          median: latencies[Math.ceil(latencies.length * 0.5) - 1], p95: latencies[Math.ceil(latencies.length * 0.95) - 1], results };
        clearInterval(outputTimer);
        await page.locator(".session-item", { hasText: "Original parent" }).click();
        await expect(page.locator(".chat-header")).toContainText("Original parent");
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
        if (!report.baseline) {
          expect(results.map((result) => result.preparationFrames), "warm views show no preparation notice").toEqual(results.map(() => 0));
          expect(results.filter((result) => result.revealGap === null || result.revealGap > 1)
            .map(({ destination, revealGap }) => ({ destination, revealGap })), "DOM reveal is already positioned").toEqual([]);
          expect(results.filter((result) => !result.firstTailInView || !result.revealTailInView)
            .map(({ destination, firstGap, revealGap }) => ({ destination, firstGap, revealGap })),
          "the current last line fits in the first visible viewport").toEqual([]);
          // Estimates above the reader may still change. Measure the readable
          // text itself in the viewport so those changes are only diagnostic.
          expect(results.filter((result) => !streaming && (result.firstGap > 1 || (result.latestMovement ?? Infinity) > 2))
            .map(({ destination, firstGap, latestMovement }) =>
              ({ destination, firstGap, latestMovement })), "the same paused tail text remains in place").toEqual([]);
        }
      });
    });
  }
}
