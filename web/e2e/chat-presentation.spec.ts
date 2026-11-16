import { expect, test, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";
import { capturePaint } from "./paint-capture";

test("public Virtuoso size snapshots require separate real tail measurement evidence", async ({ page }, info) => {
  await page.goto("/");
  const probe = await page.evaluateHandle(async () => {
    const path = "/e2e/virtuoso-measurement.ts";
    const module = await import(path) as typeof import("./virtuoso-measurement");
    return module.mountMeasurementProbe();
  });
  try {
    await expect(page.getByText("Probe row 0", { exact: true })).toBeVisible();
    await expect.poll(() => probe.evaluate((probe) => probe.geometry().height)).toBe(400);
    await expect.poll(() => probe.evaluate((probe) => probe.snapshot()?.ranges.length ?? 0)).toBeGreaterThan(0);
    const estimated = await probe.evaluate((probe) => probe.snapshot());
    expect(estimated?.ranges.some((range) => range.startIndex <= 39 && range.endIndex >= 39 && range.size === 80)).toBe(true);
    expect(await probe.evaluate((probe) => probe.measurements())).toEqual([]);
    await probe.evaluate((probe) => probe.tail());
    await expect.poll(() => probe.evaluate((probe) => probe.measurements().length)).toBeGreaterThan(0);
    expect((await probe.evaluate((probe) => probe.measurements()))[0].height).toBe(80);
    await probe.evaluate((probe) => probe.grow());
    await expect.poll(() => probe.evaluate((probe) => probe.measurements().at(-1)?.height)).toBe(1000);
    const measured = await probe.evaluate((probe) => probe.measurements());
    const duringPublish = measured.find((measurement) => measurement.height === 1000)?.installed;
    expect(duringPublish?.ranges.some((range) => range.startIndex <= 39 && range.endIndex >= 39 && range.size === 80)).toBe(true);
    await expect.poll(() => probe.evaluate((probe) => probe.snapshot()?.ranges.some(
      (range) => range.startIndex <= 39 && range.endIndex >= 39 && range.size === 1000))).toBe(true);
    await info.attach("public-size-timing.json", { contentType: "application/json", body: JSON.stringify({ estimated, measured,
      installed: await probe.evaluate((probe) => probe.snapshot()) }) });
  } finally {
    await info.attach("probe-final-layout.json", { contentType: "application/json", body: JSON.stringify(
      await probe.evaluate((probe) => ({ geometry: probe.geometry(), measured: probe.measurements(), state: probe.snapshot() }))) });
    await probe.evaluate((probe) => probe.dispose()); await probe.dispose();
  }
});

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
            rowPositions: Record<string, number> };
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
            const footerText = element?.querySelector("[data-chat-footer]")?.parentElement?.textContent ?? "";
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
              preparing: !!document.querySelector("[data-chat-preparation]"), rows };
          };
          // Separate the DOM reveal from later frame sampling: output or another
          // layout commit may arrive between them. Paint evidence stays in trace.
          let observed: Element | null = null;
          let visible = false;
          const observer = new MutationObserver(() => {
            const element = document.querySelector(".chat-messages");
            if (element !== observed) { observed = element; visible = false; }
            const next = !!element && getComputedStyle(element).visibility === "visible";
            if (next && !visible) reveals.push(snapshot());
            visible = next;
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
          revealLatency: number | null; revealGap: number | null; footerGrowth: number; viewportLoss: number; historyExtentChange: number | null }[] = [];
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
            footerGrowth: reveal ? Math.max(0, first!.footerHeight - reveal.footerHeight) : 0,
            viewportLoss: reveal ? Math.max(0, reveal.height - first!.height) : 0,
            historyExtentChange: reveal ? first!.extent - first!.footerHeight - (reveal.extent - reveal.footerHeight) : null,
            preparationFrames: owned.filter((sample) => sample.preparing).length, firstGap: first!.gap, samples: record });
        }
        const latencies = results.map((result) => result.latency).sort((a, b) => a - b);
        const report = { baseline: process.env.OCTOPUS_CHAT_BASELINE === "1", target, streaming,
          viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1,
          timeOrigin: await page.evaluate(() => performance.timeOrigin), paintFrames, historyChanges,
          median: latencies[Math.ceil(latencies.length * 0.5) - 1], p95: latencies[Math.ceil(latencies.length * 0.95) - 1], results };
        await info.attach("warm-presentation.json", { body: JSON.stringify(report), contentType: "application/json" });
        if (!report.baseline) {
          // Report latency for comparison. The user accepted the measured 131.8ms
          // p95 rather than adding complexity solely to meet the initial 100ms goal.
          expect(results.filter((result) => !streaming && (result.revealGap === null || result.revealGap > 1))
            .map(({ destination, revealGap }) => ({ destination, revealGap })), "DOM reveal is already positioned").toEqual([]);
          // With no new output, both observations must already be positioned.
          // During output, MO and rAF can follow further commits. They cannot
          // timestamp the layout handoff or prove a paint. Keep content versions
          // and compositor frames for that review, and reject historical layout
          // corrections after reveal independently of normal Footer following.
          expect(results.filter((result) => !streaming && (result.firstGap > 1 || Math.abs(result.historyExtentChange ?? Infinity) > 1))
            .map(({ destination, firstGap, historyExtentChange }) =>
              ({ destination, firstGap, historyExtentChange })), "paused first visible sample is already positioned").toEqual([]);
          expect(historyChanges, "revealed historical extent and mounted row positions stay calibrated").toEqual([]);
        }
      });
    });
  }
}
