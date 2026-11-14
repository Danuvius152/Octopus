import { expect, it } from "vitest";
import { collectExecutionPlan, executionPlan, playwrightArgs } from "../e2e/execution-plan";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

interface Suite {
  specs?: { tests: { projectName: string }[] }[];
  suites?: Suite[];
}
function projects(suites: Suite[]): string[] {
  return suites.flatMap((suite) => [
    ...(suite.specs ?? []).flatMap((spec) => spec.tests.map((test) => test.projectName)),
    ...projects(suite.suites ?? []),
  ]);
}

const files = ["app.spec.ts", "connectors.spec.ts"];
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
    const args = [...selection];
    const env = { ...process.env, OCTOPUS_E2E_MODE: "offline", OCTOPUS_E2E_AUDIT_DIR: root, TMPDIR: root };
    const reportFile = path.join(root, "execution.json");
    writeFileSync(reportFile, "execution report must survive internal collection");
    const plan = collectExecutionPlan(args, { ...env, PLAYWRIGHT_JSON_OUTPUT_FILE: reportFile });
    expect(readFileSync(reportFile, "utf8")).toBe("execution report must survive internal collection");
    expect(plan.map((partition) => partition.project)).toEqual(expected);
    for (const partition of plan) {
      const child = spawnSync("node_modules/.bin/playwright", ["test", ...playwrightArgs(partition.args, [`--project=${partition.project}`, "--list", "--reporter=json"])], {
        encoding: "utf8", env: { ...env, OCTOPUS_E2E_SCOPE: partition.scope },
      });
      expect(child.status, child.stderr || child.stdout).toBe(0);
      const report = JSON.parse(child.stdout);
      expect(report.errors).toEqual([]);
      expect(new Set(projects(report.suites))).toEqual(new Set([partition.project]));
    }
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
    const plan = collectExecutionPlan(["app.spec.ts", "accounts.spec.ts", "connectors.spec.ts"], env);
    expect(plan.map(({ project, scope }) => [project, scope])).toEqual([
      ["chromium", "main"], ["installation", "main"], ["installation", "accounts"],
    ]);
    for (const partition of plan) {
      const child = spawnSync("node_modules/.bin/playwright", ["test", ...playwrightArgs(partition.args, [`--project=${partition.project}`, "--list", "--reporter=json"])], {
        encoding: "utf8", env: { ...env, OCTOPUS_E2E_SCOPE: partition.scope },
      });
      expect(child.status, child.stderr || child.stdout).toBe(0);
      const report = JSON.parse(child.stdout);
      const files = (suites: { specs?: { file: string }[]; suites?: never[] }[]): string[] =>
        suites.flatMap((suite) => [...(suite.specs ?? []).map((spec) => spec.file), ...files(suite.suites ?? [])]);
      const collected = files(report.suites);
      expect(collected.length).toBeGreaterThan(0);
      expect(collected.every((file) => /accounts\.spec\.ts$/.test(file) === (partition.scope === "accounts"))).toBe(true);
    }
    expect(collectExecutionPlan(["app.spec.ts", "accounts.spec.ts", "--grep=shows connection status"], env)
      .map(({ scope }) => scope)).toEqual(["main"]);
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 15_000);
