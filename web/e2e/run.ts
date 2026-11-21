import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { verifyOfflineAudit } from "./offline-audit";
import { collectExecutionPlan, playwrightArgs, uiModeArgs } from "./execution-plan";

const [mode, ...args] = process.argv.slice(2);
if (mode !== "offline" && mode !== "real") throw new Error("Choose offline or real E2E mode");
const evidenceRoot = process.env.OCTOPUS_E2E_EVIDENCE_DIR;
if (evidenceRoot) {
  if (mode !== "offline" || uiModeArgs(args).interactive) throw new Error("Partition evidence requires headless offline execution");
  mkdirSync(evidenceRoot, { mode: 0o700 });
}
const testRoot = path.join(os.homedir(), ".cache", "octopus-tests");
mkdirSync(testRoot, { recursive: true, mode: 0o700 });
const selectionRoot = mkdtempSync(path.join(testRoot, "e2e-selection-"));
const plan = (() => {
  const selectedArgs = evidenceRoot ? playwrightArgs(args, ["--forbid-only"]) : args;
  try {
    return collectExecutionPlan(selectedArgs, { ...process.env, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: selectionRoot, TMPDIR: selectionRoot });
  } finally { rmSync(selectionRoot, { recursive: true, force: true }); }
})();
function run(project: string, scope: "main" | "accounts", selectedArgs: string[], evidence?: PartitionEvidence): number {
  const { interactive } = uiModeArgs(selectedArgs);
  // Stay outside managed installation roots and CLI-restricted system temp dirs.
  // Each run owns fresh mutable state, even after an interrupted prior run.
  // Keep audit evidence outside runtime cleanup so late violations still count.
  const auditDir = evidence?.directory ?? mkdtempSync(path.join(testRoot, "e2e-audit-"));
  if (evidence) mkdirSync(evidence.directory, { mode: 0o700 });
  const runtimeDir = mkdtempSync(path.join(testRoot, "e2e-runtime-"));
  const environment = { ...process.env };
  // Codex refuses helper installation when CODEX_HOME is inside TMPDIR.
  // Both remain owned by this run, with provider homes beside its temp tree.
  environment.TMPDIR = path.join(runtimeDir, "tmp");
  mkdirSync(environment.TMPDIR);
  for (const [variable, directory, files] of [
    ["CODEX_HOME", ".codex", ["auth.json", "config.toml"]],
    ["CLAUDE_CONFIG_DIR", ".claude", [".credentials.json", "settings.json", ".claude.json"]],
  ] as const) {
    const source = environment[variable] || path.join(os.homedir(), directory);
    const destination = path.join(runtimeDir, directory);
    mkdirSync(destination);
    // Reuse live login references without inheriting the host installation's
    // ownership marker or writing CLI output into its managed provider home.
    if (mode === "real") {
      for (const file of files) {
        if (existsSync(path.join(source, file))) symlinkSync(path.join(source, file), path.join(destination, file));
      }
    }
    environment[variable] = destination;
  }
  environment.OCTOPUS_CODEX_HOME_DIR = path.join(runtimeDir, "credentials");
  environment.OCTOPUS_LARGE_PROMPTS_DIR = path.join(runtimeDir, "large-prompts");
  console.log(`E2E mode=${mode}; project=${project}; scope=${scope}; services=backend+vite; audit=${auditDir}`);
  const options = [`--project=${project}`];
  if (evidence) {
    options.push("--reporter=line,json", "--trace=retain-on-failure", `--output=${path.join(evidence.directory, "artifacts")}`);
    environment.PLAYWRIGHT_JSON_OUTPUT_FILE = evidence.report;
  }
  const command = ["./node_modules/.bin/playwright", "test", ...playwrightArgs(selectedArgs, options)];
  if (evidence) evidence.argv = command;
  let child;
  try {
    child = spawnSync(command[0], command.slice(1), {
      stdio: "inherit",
      env: { ...environment, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: auditDir, OCTOPUS_E2E_SCOPE: scope,
        // Native UI can change its project filter after opening. Only this
        // prepared phase exists in its config, so it cannot cross state scopes.
        OCTOPUS_E2E_UI_PROJECT: interactive ? project : undefined },
    });
    if (evidence) Object.assign(evidence, { returncode: child.status, signal: child.signal });
  } finally {
    rmSync(runtimeDir, { recursive: true, force: true });
  }
  if (child.error) console.error(child.error);
  let status = child.status ?? 1;
  // spawnSync returns after Playwright's webServer teardown: late, swallowed
  // startup/cleanup violations count too. Missing/unreadable audits fail closed.
  if (mode === "offline") {
    const auditFile = path.join(auditDir, `${scope}.jsonl`);
    if (interactive && readdirSync(auditDir).length === 0) {
      console.log("UI closed without backend setup; no offline test execution was audited.");
      return status;
    }
    try {
      console.log("Offline execution audit:", verifyOfflineAudit(
        [auditFile], { allowRestarts: interactive },
      ));
    } catch (error) {
      console.error(error);
      status = 1;
    }
  }
  return status;
}

interface PartitionEvidence {
  project: string;
  scope: string;
  directory: string;
  report: string;
  status: "not-run" | "running" | "passed" | "failed";
  argv?: string[];
  returncode?: number | null;
  signal?: string | null;
  duration_seconds?: number;
  workers?: number | null;
  counts: Record<string, number> | null;
}

function runWithEvidence(root: string): number {
  const partitions = plan.map<PartitionEvidence>(({ project, scope }, index) => {
    const directory = path.join(root, `${index + 1}-${project}-${scope}`);
    return { project, scope, directory, report: path.join(directory, "playwright.json"), status: "not-run", counts: null };
  });
  const destination = path.join(root, "result.json");
  const save = (passed: boolean) => {
    const temporary = destination + ".tmp";
    writeFileSync(temporary, JSON.stringify({ passed, partitions }, null, 2) + "\n", { mode: 0o600 });
    renameSync(temporary, destination);
  };
  for (const [index, row] of partitions.entries()) {
    const started = performance.now();
    row.status = "running";
    save(false);
    let status = 1;
    try {
      status = run(row.project, plan[index].scope, plan[index].args, row);
    } catch (error) {
      console.error(error);
    }
    try {
      const report = JSON.parse(readFileSync(row.report, "utf8"));
      const { expected, unexpected, flaky, skipped } = report.stats;
      if (![expected, unexpected, flaky, skipped].every((count) => Number.isInteger(count) && count >= 0))
        throw new Error("Invalid native Playwright test counts");
      row.workers = report.config.metadata?.actualWorkers ?? null;
      row.counts = { passed: expected, failed: unexpected, flaky, skipped, errors: report.errors.length };
      if (!expected || unexpected || flaky || report.errors.length) status = 1;
    } catch (error) {
      status = 1;
      console.error(error);
    }
    row.duration_seconds = (performance.now() - started) / 1000;
    row.status = status === 0 ? "passed" : "failed";
    save(false);
  }
  const passed = partitions.length > 0 && partitions.every(({ status }) => status === "passed");
  save(passed);
  return passed ? 0 : 1;
}

function runSelected(): number {
  if (!plan.length) { console.error("No tests found"); return 1; }
  for (const partition of plan) {
    const result = run(partition.project, partition.scope, partition.args);
    if (result !== 0) return result;
  }
  return 0;
}

process.exitCode = evidenceRoot ? runWithEvidence(path.resolve(evidenceRoot)) : runSelected();
