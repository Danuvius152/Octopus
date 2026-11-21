import { expect, it } from "vitest";
import { collectExecutionPlan, executionPlan, playwrightArgs } from "../e2e/execution-plan";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
it("keeps a failed release partition and still records the independent partitions", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "octopus-e2e-evidence-"));
  try {
    const bin = path.join(root, "node_modules/.bin");
    mkdirSync(bin, { recursive: true });
    writeFileSync(path.join(bin, "playwright"), `#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const args = process.argv.slice(2);
if (args.includes("--list")) {
  console.log(JSON.stringify({ errors: [], suites: [{ specs: [
    { file: "app.spec.ts", tests: [{ projectName: "chromium" }] },
    { file: "connectors.spec.ts", tests: [{ projectName: "installation" }] },
    { file: "accounts.spec.ts", tests: [{ projectName: "installation" }] }
  ] }] }));
} else {
  const failed = args.includes("--project=chromium");
  const scope = process.env.OCTOPUS_E2E_SCOPE;
  const audit = path.join(process.env.OCTOPUS_E2E_AUDIT_DIR, scope + ".jsonl");
  fs.writeFileSync(audit, '{"kind":"guard_installed"}\\n{"kind":"offline_ready"}\\n');
  fs.writeFileSync(process.env.PLAYWRIGHT_JSON_OUTPUT_FILE, JSON.stringify({
    config: { workers: failed ? 2 : 1 }, errors: [], stats: {
      expected: failed ? 0 : 1, unexpected: Number(failed), flaky: 0, skipped: 0
    }
  }));
}
`, { mode: 0o700 });
    const destination = path.join(root, "evidence");
    const child = spawnSync("bun", [path.resolve("e2e/run.ts"), "offline"], {
      cwd: root, env: { ...process.env, OCTOPUS_E2E_EVIDENCE_DIR: destination }, encoding: "utf8",
    });
    expect(child.status, child.stderr || child.stdout).toBe(1);
    const result = JSON.parse(readFileSync(path.join(destination, "result.json"), "utf8"));
    expect(result.passed).toBe(false);
    expect(result.partitions.map(({ project, scope, status, returncode, workers }: Record<string, unknown>) =>
      [project, scope, status, returncode, workers])).toEqual([
      ["chromium", "main", "failed", 0, 2],
      ["installation", "main", "passed", 0, 1],
      ["installation", "accounts", "passed", 0, 1],
    ]);
    for (const phase of result.partitions) {
      expect(JSON.parse(readFileSync(phase.report, "utf8")).stats).toBeDefined();
      expect(phase.argv).toContain("--trace=retain-on-failure");
    }
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
}, 15_000);
