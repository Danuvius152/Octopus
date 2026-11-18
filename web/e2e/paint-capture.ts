import type { CDPSession, Page, TestInfo } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";

export interface PaintFrame { file: string; timestamp: number }

/** Keep frame acknowledgements and files inside the private CDP session's life. */
export async function capturePaint(page: Page, info: TestInfo,
  run: (frames: PaintFrame[], onCleanup: (release: () => unknown) => void, session: CDPSession) => Promise<void>) {
  const session = await page.context().newCDPSession(page);
  const frames: PaintFrame[] = [];
  const pending: Promise<void>[] = [];
  const failures: unknown[] = [];
  const cleanup: (() => unknown)[] = [];
  const collect = (work: Promise<unknown>) => {
    // Attach rejection handling at creation, including during warm-up. Report
    // the error from this test after every resource has had a cleanup attempt.
    pending.push(work.then(() => {}, (error: unknown) => { failures.push(error); }));
  };
  const recordFrame = (frame: { sessionId: number; data: string; metadata: { timestamp?: number } }) => {
    collect(session.send("Page.screencastFrameAck", { sessionId: frame.sessionId }));
    if (frame.metadata.timestamp === undefined) {
      failures.push(new Error("Compositor frame has no presentation timestamp"));
      return;
    }
    const file = `paint-frames/${frames.length}.jpeg`;
    frames.push({ file, timestamp: frame.metadata.timestamp * 1000 });
    collect(writeFile(info.outputPath(file), Buffer.from(frame.data, "base64")));
  };
  try {
    await mkdir(info.outputPath("paint-frames"), { recursive: true });
    session.on("Page.screencastFrame", recordFrame);
    await session.send("Page.startScreencast", { format: "jpeg", quality: 90, maxWidth: 1280, maxHeight: 900, everyNthFrame: 1 });
    await run(frames, (release) => { cleanup.push(release); }, session);
  } catch (error) {
    failures.push(error);
  } finally {
    session.removeListener("Page.screencastFrame", recordFrame);
    for (const release of [
      ...cleanup,
      () => session.send("Page.stopScreencast"),
      () => Promise.all(pending),
      () => session.detach(),
    ]) {
      try { await release(); } catch (error) { failures.push(error); }
    }
  }
  if (failures.length) throw new AggregateError(failures,
    ["Compositor capture failed", ...failures.map((error) => String(error))].join("\n"), { cause: failures[0] });
}
