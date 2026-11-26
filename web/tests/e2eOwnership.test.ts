import { expect, it } from "vitest";
import { collectExecutionPlan, executionPlan, playwrightArgs } from "../e2e/execution-plan";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

interface Spec {
  file: string;
  tests: { projectName: string }[];
}
interface Suite {
  specs?: Spec[];
  suites?: Suite[];
}
function collectedSpecs(suites: Suite[]): Spec[] {
  return suites.flatMap((suite) => [
    ...(suite.specs ?? []),
    ...collectedSpecs(suite.suites ?? []),
  ]);
}

const files = ["app.spec.ts", "connectors.spec.ts"];
it.each([false, true])("keeps a failed release partition and still records the independent partitions (cleanup error: %s)", (cleanupError) => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-e2e-evidence-"));
  try {
    const bin = path.join(root, "node_modules/.bin");
    mkdirSync(bin, { recursive: true });
    writeFileSync(path.join(bin, "playwright"), `#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const args = process.argv.slice(2);
if (args.includes("--list")) {
  fs.writeFileSync(process.env.PLAYWRIGHT_JSON_OUTPUT_FILE, JSON.stringify({ errors: [], suites: [{ specs: [
    { file: "app.spec.ts", tests: [{ projectName: "chromium" }] },
    { file: "connectors.spec.ts", tests: [{ projectName: "installation" }] },
    { file: "accounts.spec.ts", tests: [{ projectName: "installation" }] }
  ] }] }));
} else {
  const failed = args.includes("--project=chromium");
  const startupFailure = failed && !${cleanupError};
  const scope = process.env.OCTOPUS_E2E_SCOPE;
  const audit = path.join(process.env.OCTOPUS_E2E_AUDIT_DIR, scope + ".jsonl");
  fs.writeFileSync(audit, '{"kind":"guard_installed"}\\n{"kind":"offline_ready"}\\n');
  fs.writeFileSync(process.env.PLAYWRIGHT_JSON_OUTPUT_FILE, JSON.stringify({
    config: { workers: 2, metadata: startupFailure ? {} : { actualWorkers: 1 } },
    errors: startupFailure ? [{ message: "Service startup failed" }] : [], stats: {
      expected: startupFailure ? 0 : 1, unexpected: 0, flaky: 0, skipped: 0
    }
  }));
  if (failed && ${cleanupError}) process.exitCode = 7;
}
`, { mode: 0o700 });
    const destination = path.join(root, "evidence");
    const preload = path.join(root, "cleanup.ts");
    writeFileSync(preload, `
import { mock } from "bun:test";
import * as fs from "node:fs";
import path from "node:path";
const remove = fs.rmSync;
let failed = false;
mock.module("node:fs", () => ({
  ...fs,
  rmSync(directory, options) {
    remove(directory, options);
    if (!failed && path.basename(directory).startsWith("e2e-runtime-")) {
      failed = true;
      throw new Error("Injected cleanup failure");
    }
  },
}));
`);
    const child = spawnSync("bun", [...(cleanupError ? ["--preload", preload] : []), path.resolve("e2e/run.ts"), "offline"], {
      cwd: root, env: { ...process.env, OCTOPUS_E2E_EVIDENCE_DIR: destination }, encoding: "utf8",
    });
    expect(child.status, child.stderr || child.stdout).toBe(1);
    const result = JSON.parse(readFileSync(path.join(destination, "result.json"), "utf8"));
    expect(result.passed).toBe(false);
    expect(result.partitions.map(({ project, scope, status, returncode, workers }: Record<string, unknown>) =>
      [project, scope, status, returncode, workers])).toEqual([
      ["chromium", "main", "failed", cleanupError ? 7 : 0, cleanupError ? 1 : null],
      ["installation", "main", "passed", 0, 1],
      ["installation", "accounts", "passed", 0, 1],
    ]);
    for (const phase of result.partitions) {
      const native = JSON.parse(readFileSync(phase.report, "utf8"));
      expect(phase.counts).toEqual({
        passed: native.stats.expected, failed: native.stats.unexpected, flaky: native.stats.flaky,
        skipped: native.stats.skipped, errors: native.errors.length,
      });
      expect(phase.argv).toContain("--trace=retain-on-failure");
      expect(phase.argv).toContain("--forbid-only");
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 10_000);

it("rejects focused Playwright tests during release collection", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-e2e-focused-"));
  try {
    symlinkSync(path.resolve("node_modules"), path.join(root, "node_modules"));
    writeFileSync(path.join(root, "playwright.config.cjs"), 'module.exports = { projects: [{ name: "chromium" }] };');
    writeFileSync(path.join(root, "app.spec.cjs"), `
const { test } = require("@playwright/test");
const { writeFileSync } = require("node:fs");
const path = require("node:path");
test.only("focused", () => {
  writeFileSync(path.join(process.env.OCTOPUS_E2E_AUDIT_DIR, "main.jsonl"),
    '{"kind":"guard_installed"}\\n{"kind":"offline_ready"}\\n');
});
test("case omitted by focus", () => { throw new Error("This case must not be silently omitted"); });
`);
    const destination = path.join(root, "evidence");
    const child = spawnSync("bun", [path.resolve("e2e/run.ts"), "offline"], {
      cwd: root, env: { ...process.env, OCTOPUS_E2E_EVIDENCE_DIR: destination }, encoding: "utf8",
    });
    expect(child.status, child.stderr || child.stdout).toBe(1);
    expect(child.stderr).toContain("item focused with '.only'");
    expect(child.stdout).not.toContain("services=backend+vite");
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 10_000);

for (const [label, selection, expected] of [
  ["installation only", [...files, "--project=installation"], ["installation"]],
  ["chromium only", [...files, "--project", "chromium"], ["chromium"]],
  ["default", files, ["chromium", "installation"]],
  ["variadic", [...files, "--project", "chromium", "installation"], ["chromium", "installation"]],
  ["repeated options", [...files, "--project=chromium", "--project=installation"], ["chromium", "installation"]],
  ["wildcard", [...files, "--project=*"], ["chromium", "installation"]],
  ["partial wildcard", [...files, "--project=INSTALL*"], ["installation"]],
  ["inline before files", ["--project=installation", ...files], ["installation"]],
] as const) it(`intersects ${label} with each real configuration partition`, () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-project-selection-"));
  try {
    const env = { ...process.env, OCTOPUS_E2E_MODE: "offline", OCTOPUS_E2E_AUDIT_DIR: root, TMPDIR: root };
    expect(collectExecutionPlan([...selection], env)).toEqual(expected.map(project => ({
      project, scope: "main", args: project === "installation" ? [...files, "--workers=1"] : files,
    })));
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 15_000);

it("preserves native unknown-project, unmatched-pattern and empty file selection failures", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-project-rejection-"));
  try {
    const env = { ...process.env, OCTOPUS_E2E_MODE: "offline", OCTOPUS_E2E_AUDIT_DIR: root, TMPDIR: root };
    for (const args of [["--project=unknown"], ["--project=chromium", "--project=unknown"], ["--project=unknown*"], ["no-such-file.spec.ts"]]) {
      expect(() => collectExecutionPlan(args, env)).toThrow(/Project|No tests found/);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 15_000);

it("reuses ordinary and exclusive services across repeats; only first-account stories restart", () => {
  const args = ["--repeat-each=6", "--workers=2", "--grep", "example"];
  const selected = [{ project: "chromium", file: "app.spec.ts" }, { project: "installation", file: "connectors.spec.ts" },
    { project: "installation", file: "accounts.spec.ts" }];
  const plan = executionPlan(args, selected);
  expect(plan).toHaveLength(8);
  expect(plan[0]).toEqual({ project: "chromium", scope: "main", args });
  expect(plan[1]).toEqual({ project: "installation", scope: "main", args: ["--repeat-each=6", "--grep", "example", "--workers=1"] });
  for (const partition of plan.slice(2)) {
    expect(partition).toEqual({ project: "installation", scope: "accounts", args: ["--grep", "example", "--workers=1", "--repeat-each=1"] });
  }
  expect(executionPlan(["--repeat-each", "2", "--workers", "2"], selected)).toHaveLength(4);
});

it("prepares account services only when the actual file/grep selection includes its story", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-scope-selection-"));
  try {
    const env = { ...process.env, OCTOPUS_E2E_MODE: "offline", OCTOPUS_E2E_AUDIT_DIR: root, TMPDIR: root };
    const reportFile = path.join(root, "execution.json");
    writeFileSync(reportFile, "execution report must survive internal collection");
    const plan = collectExecutionPlan(["app.spec.ts", "accounts.spec.ts", "connectors.spec.ts"],
      { ...env, PLAYWRIGHT_JSON_OUTPUT_FILE: reportFile });
    expect(readFileSync(reportFile, "utf8")).toBe("execution report must survive internal collection");
    expect(plan.map(({ project, scope }) => [project, scope])).toEqual([
      ["chromium", "main"], ["installation", "main"], ["installation", "accounts"],
    ]);
    for (const partition of plan) {
      const child = spawnSync("node_modules/.bin/playwright", ["test", ...playwrightArgs(partition.args, [`--project=${partition.project}`, "--list", "--reporter=json"])], {
        encoding: "utf8", env: { ...env, OCTOPUS_E2E_SCOPE: partition.scope },
      });
      expect(child.status, child.stderr || child.stdout).toBe(0);
      const report = JSON.parse(child.stdout);
      expect(report.errors).toEqual([]);
      const collected = collectedSpecs(report.suites);
      expect(new Set(collected.flatMap(spec => spec.tests.map(test => test.projectName)))).toEqual(new Set([partition.project]));
      expect(collected.every(({ file }) => /accounts\.spec\.ts$/.test(file) === (partition.scope === "accounts"))).toBe(true);
    }
    expect(collectExecutionPlan(["app.spec.ts", "accounts.spec.ts", "--grep=shows connection status"], env)
      .map(({ scope }) => scope)).toEqual(["main"]);
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 20_000);
