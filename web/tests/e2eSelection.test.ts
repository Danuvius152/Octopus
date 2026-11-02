import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { expect, it } from "vitest";

interface Suite {
  title: string;
  specs?: { title: string }[];
  suites?: Suite[];
}

function titles(suites: Suite[], parents: string[] = []): string[] {
  return suites.flatMap((suite) => {
    const prefix = [...parents, suite.title];
    return [
      ...(suite.specs ?? []).map((spec) => [...prefix, spec.title].join(" > ")),
      ...titles(suite.suites ?? [], prefix),
    ];
  });
}

it("collects UI regressions offline and native-provider checks only in the live bucket", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-e2e-selection-"));
  try {
    const buckets = ["offline", "real"].map((mode) => {
      // --list loads the actual specs and config without setup, browsers,
      // servers or model probes. Its titles include inherited suite tags.
      const child = spawnSync("node_modules/.bin/playwright", ["test", "--list", "--reporter=json"], {
        encoding: "utf8",
        env: { ...process.env, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: root, TMPDIR: root },
      });
      expect(child.status, child.stderr || child.stdout).toBe(0);
      const report = JSON.parse(child.stdout) as { suites: Suite[]; errors: unknown[] };
      expect(report.errors).toEqual([]);
      return titles(report.suites);
    });
    const [offline, real] = buckets;
    for (const title of [
      "shows connection status",
      "the Schedules section opens the all-agents overview",
      "imported question_request renders the asked-question summary",
      "assistant turns are attributed to the owning agent",
      "clears history client-side, keeps the same sidebar name",
      "the account-menu manage page views and unarchives a session",
      "add / list / delete a webhook via the Settings dialog",
    ]) {
      expect(offline.filter((item) => item.endsWith(` > ${title}`)), title).toHaveLength(1);
      expect(real.some((item) => item.endsWith(` > ${title}`)), title).toBe(false);
    }
    expect(offline.every((title) => !title.includes("@llm"))).toBe(true);
    expect(real.every((title) => title.includes("@llm"))).toBe(true);
    for (const title of [
      "create a Codex session via the UI and get a real response @llm",
      "App Server preserves native history and original submissions across reload @llm",
      "sends a message and receives response @llm",
      "send with Enter key @llm",
      "disables input while running @llm",
      "/schedule command creates a schedule shown in the overview; toggle + delete @llm",
      "an agent sets a schedule for itself and the sidebar updates live @llm",
      "webhook fires when a real session goes idle (real-CLI) @llm",
    ]) {
      expect(real.filter((item) => item.endsWith(` > ${title}`)), title).toHaveLength(1);
      expect(offline.some((item) => item.endsWith(` > ${title}`)), title).toBe(false);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}, 15_000);
