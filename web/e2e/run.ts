import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { verifyOfflineAudit } from "./offline-audit";
import { collectExecutionPlan, playwrightArgs, uiModeArgs } from "./execution-plan";

const [mode, ...args] = process.argv.slice(2);
if (mode !== "offline" && mode !== "real") throw new Error("Choose offline or real E2E mode");
const evidenceRoot = process.env.OCTOPUS_E2E_EVIDENCE_DIR && path.resolve(process.env.OCTOPUS_E2E_EVIDENCE_DIR);
const sourceRoot = fileURLToPath(new URL("../../", import.meta.url));
const python = path.join(sourceRoot, ".venv/bin/python");
const targetedRun = process.env.OCTOPUS_TARGETED_CHECK_DIR;
const stoppedCode = 143;
const stopRequested = () => Boolean(targetedRun && existsSync(path.join(targetedRun, "stop")));
// Playwright splits --reporter on commas. PW_TEST_REPORTER adds one literal path.
const evidenceReporter = fileURLToPath(new URL("./playwright-evidence.ts", import.meta.url));

interface CandidateIdentity { head: string; working_tree_sha256: string }

function sourceIdentity(): CandidateIdentity {
  return JSON.parse(execFileSync(python, ["-c", [
    "import json",
    "from pathlib import Path",
    "from scripts.check_runner import source_identity",
    "print(json.dumps(source_identity(Path.cwd())))",
  ].join("\n")], { cwd: sourceRoot, encoding: "utf8" }));
}

function startEvidence(root: string): CandidateIdentity {
  if (mode !== "offline" || uiModeArgs(args).interactive) throw new Error("Partition evidence requires headless offline execution");
  const candidate = sourceIdentity();
  mkdirSync(root, { mode: 0o700 });
  writeFileSync(path.join(root, "run.json"), JSON.stringify({
    candidate, source_root: sourceRoot, argv: process.argv, cwd: process.cwd(), mode, reporter: evidenceReporter,
  }, null, 2) + "\n", { mode: 0o600, flush: true });
  return candidate;
}

function verifySourceInputs(root: string, before: CandidateIdentity): boolean {
  const candidate = sourceIdentity();
  const changed = before.head !== candidate.head || before.working_tree_sha256 !== candidate.working_tree_sha256;
  writeFileSync(path.join(root, "inputs-after.json"), JSON.stringify({
    candidate, inputs_changed: changed,
  }, null, 2) + "\n", { flag: "wx", mode: 0o600, flush: true });
  if (changed) console.error("Playwright source inputs changed during execution");
  return changed;
}

let testRoot = path.join(os.homedir(), ".cache", "octopus-tests");
if (targetedRun) {
  const reservation = spawnSync(python, ["-m", "scripts.targeted_check", "reserve-playwright", targetedRun],
    { cwd: sourceRoot, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
  if (reservation.error) throw reservation.error;
  if (reservation.status !== 0) process.exit(reservation.status ?? 1);
  testRoot = reservation.stdout.trim();
  if (evidenceRoot) {
    const actual = path.join(realpathSync(path.dirname(evidenceRoot)), path.basename(evidenceRoot));
    const relative = path.relative(realpathSync(testRoot), actual);
    if (!relative || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)))
      throw new Error("Playwright evidence must be outside the targeted runtime");
  }
}
const candidate = evidenceRoot ? startEvidence(evidenceRoot) : undefined;
if (stopRequested()) process.exit(stoppedCode);
mkdirSync(testRoot, { recursive: true, mode: 0o700 });
const selectionRoot = mkdtempSync(path.join(testRoot, "e2e-selection-"));
const plan = (() => {
  const selectedArgs = evidenceRoot ? playwrightArgs(args, ["--forbid-only"]) : args;
  try {
    const collectionDir = evidenceRoot && path.join(evidenceRoot, "collection");
    if (collectionDir) mkdirSync(collectionDir, { mode: 0o700 });
    return collectExecutionPlan(selectedArgs, { ...process.env, OCTOPUS_E2E_MODE: mode,
      OCTOPUS_E2E_AUDIT_DIR: selectionRoot, TMPDIR: selectionRoot },
    collectionDir ? { directory: collectionDir, reporter: evidenceReporter } : undefined);
  } catch (error) {
    if (evidenceRoot && candidate) {
      try {
        verifySourceInputs(evidenceRoot, candidate);
      } catch (verificationError) {
        console.error("Could not verify Playwright source inputs after collection failure:", verificationError);
      }
    }
    throw error;
  } finally {
    if (!targetedRun) rmSync(selectionRoot, { recursive: true, force: true });
  }
})();

function partitionCommand(project: string, args: string[], directory?: string): string[] {
  const options = [`--project=${project}`];
  if (directory) {
    options.push("--reporter=line,json", "--trace=retain-on-failure", `--output=${path.join(directory, "artifacts")}`);
  }
  return ["./node_modules/.bin/playwright", "test", ...playwrightArgs(args, options)];
}

function run(project: string, scope: "main" | "accounts", command: string[], evidence?: PartitionEvidence): number {
  const { interactive } = uiModeArgs(command.slice(2));
  // Stay outside managed installation roots and CLI-restricted system temp dirs.
  // Each run owns fresh mutable state, even after an interrupted prior run.
  // Keep audit evidence outside runtime cleanup so late violations still count.
  const auditDir = evidence?.directory ?? mkdtempSync(path.join(targetedRun || testRoot, "e2e-audit-"));
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
  if (evidence) {
    environment.PLAYWRIGHT_JSON_OUTPUT_FILE = evidence.report;
    environment.PW_TEST_REPORTER = evidenceReporter;
    environment.OCTOPUS_E2E_EVENTS_FILE = evidence.events;
    environment.OCTOPUS_E2E_EVIDENCE_PHASE = "execution";
  }
  let child;
  try {
    if (stopRequested()) return stoppedCode;
    const started = path.join(runtimeDir, "playwright-started");
    const argv = targetedRun
      ? [python, "-m", "scripts.targeted_check", "dispatch-playwright", targetedRun, process.cwd(), started, "--", ...command]
      : command;
    child = spawnSync(argv[0], argv.slice(1), {
      cwd: targetedRun ? sourceRoot : undefined,
      stdio: "inherit",
      env: { ...environment, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: auditDir, OCTOPUS_E2E_SCOPE: scope,
        // Native UI can change its project filter after opening. Only this
        // prepared phase exists in its config, so it cannot cross state scopes.
        OCTOPUS_E2E_UI_PROJECT: interactive ? project : undefined },
    });
    if (targetedRun && stopRequested() && !existsSync(started)) return stoppedCode;
    if (evidence) Object.assign(evidence, { returncode: child.status, signal: child.signal });
  } finally {
    if (!targetedRun) rmSync(runtimeDir, { recursive: true, force: true });
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
  scope: "main" | "accounts";
  directory: string;
  report: string;
  events: string;
  status: "not-run" | "running" | "passed" | "failed" | "interrupted";
  argv: string[];
  returncode?: number | null;
  signal?: string | null;
  duration_seconds?: number;
  workers?: number | null;
  counts: Record<string, number> | null;
}

function runWithEvidence(root: string, candidate: CandidateIdentity): number {
  const partitions = plan.map<PartitionEvidence>(({ project, scope, args }, index) => {
    const directory = path.join(root, `${index + 1}-${project}-${scope}`);
    return { project, scope, directory, report: path.join(directory, "playwright.json"),
      events: path.join(directory, "events.jsonl"), argv: partitionCommand(project, args, directory), status: "not-run", counts: null };
  });
  const destination = path.join(root, "result.json");
  const save = (passed: boolean, inputsChanged?: boolean) => {
    const temporary = destination + ".tmp";
    writeFileSync(temporary, JSON.stringify({ passed, partitions, inputs_changed: inputsChanged }, null, 2) + "\n", { mode: 0o600 });
    renameSync(temporary, destination);
  };
  for (const row of partitions) {
    if (stopRequested()) return stoppedCode;
    const started = performance.now();
    row.status = "running";
    save(false);
    let status = 1;
    try {
      status = run(row.project, row.scope, row.argv, row);
    } catch (error) {
      console.error(error);
    }
    const stopped = stopRequested();
    if (stopped && (row.returncode == null || row.returncode >= 128)) {
      row.status = row.returncode === undefined ? "not-run" : "interrupted";
      save(false);
      return stoppedCode;
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
      if (stopped) {
        row.status = "interrupted";
        save(false);
        return stoppedCode;
      }
      status = 1;
      console.error(error);
    }
    row.duration_seconds = (performance.now() - started) / 1000;
    row.status = status === 0 ? "passed" : "failed";
    save(false);
  }
  if (stopRequested()) return stoppedCode;
  const inputsChanged = verifySourceInputs(root, candidate);
  if (stopRequested()) return stoppedCode;
  const passed = !inputsChanged && partitions.length > 0 && partitions.every(({ status }) => status === "passed");
  save(passed, inputsChanged);
  return passed ? 0 : 1;
}

function runSelected(): number {
  if (!plan.length) { console.error("No tests found"); return 1; }
  for (const { project, scope, args } of plan) {
    if (stopRequested()) return stoppedCode;
    const result = run(project, scope, partitionCommand(project, args));
    if (result !== 0) return result;
  }
  return 0;
}

process.exitCode = evidenceRoot && candidate ? runWithEvidence(evidenceRoot, candidate) : runSelected();
