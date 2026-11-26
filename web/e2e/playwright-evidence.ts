import { appendFileSync } from "node:fs";

import type { FullConfig, Reporter, Suite, TestCase, TestError, TestResult } from "@playwright/test/reporter";

function testDetails(test: TestCase) {
  return {
    test_id: test.id,
    title: test.title,
    title_path: test.titlePath(),
    project: test.parent.project()?.name,
    location: test.location,
    repeat_index: test.repeatEachIndex,
    expected_status: test.expectedStatus,
  };
}

function attemptDetails(test: TestCase, result: TestResult) {
  return {
    ...testDetails(test),
    retry: result.retry,
    worker_index: result.workerIndex,
    parallel_index: result.parallelIndex,
  };
}

export default class PlaywrightEvidence implements Reporter {
  private readonly file: string;
  private readonly phase: "collection" | "execution";
  private writeFailed = false;

  constructor() {
    const file = process.env.OCTOPUS_E2E_EVENTS_FILE;
    const phase = process.env.OCTOPUS_E2E_EVIDENCE_PHASE;
    if (!file || (phase !== "collection" && phase !== "execution")) {
      throw new Error("Playwright evidence requires a destination and phase");
    }
    this.file = file;
    this.phase = phase;
  }

  printsToStdio() { return false; }

  onBegin(config: FullConfig, suite: Suite) {
    this.write({
      event: "selection",
      config_file: config.configFile,
      root_dir: config.rootDir,
      workers: config.workers,
      shard: config.shard,
      tests: suite.allTests().map(testDetails),
    });
  }

  onTestBegin(test: TestCase, result: TestResult) {
    this.write({ event: "test-begin", ...attemptDetails(test, result) });
  }

  onTestEnd(test: TestCase, result: TestResult) {
    this.write({
      event: "test-end", ...attemptDetails(test, result),
      status: result.status, duration_ms: result.duration, errors: result.errors,
    });
  }

  onError(error: TestError) {
    this.write({ event: "error", location: error.location, errors: [error] });
  }

  async onEnd(): Promise<{ status: "failed" } | void> {
    if (this.writeFailed) return { status: "failed" };
  }

  private write(event: object) {
    try {
      appendFileSync(this.file, JSON.stringify({ phase: this.phase, ...event }) + "\n", { mode: 0o600, flush: true });
    } catch (error) {
      // Playwright swallows reporter exceptions. A completed run must expose evidence loss.
      this.writeFailed = true;
      console.error("Could not persist Playwright evidence:", error);
    }
  }
}
