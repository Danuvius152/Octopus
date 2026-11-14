import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, symlinkSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { verifyOfflineAudit } from "./offline-audit";
import { collectExecutionPlan, playwrightArgs, uiModeArgs } from "./execution-plan";

const [mode, ...args] = process.argv.slice(2);
if (mode !== "offline" && mode !== "real") throw new Error("Choose offline or real E2E mode");
const testRoot = path.join(os.homedir(), ".cache", "octopus-tests");
mkdirSync(testRoot, { recursive: true, mode: 0o700 });
const selectionRoot = mkdtempSync(path.join(testRoot, "e2e-selection-"));
const plan = (() => {
  try {
    return collectExecutionPlan(args, { ...process.env, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: selectionRoot, TMPDIR: selectionRoot });
  } finally { rmSync(selectionRoot, { recursive: true, force: true }); }
})();
function run(project: string, scope: "main" | "accounts", selectedArgs: string[]): number {
  const { interactive } = uiModeArgs(selectedArgs);
  // Stay outside managed installation roots and CLI-restricted system temp dirs.
  // Each run owns fresh mutable state, even after an interrupted prior run.
  // Keep audit evidence outside runtime cleanup so late violations still count.
  const auditDir = mkdtempSync(path.join(testRoot, "e2e-audit-"));
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
  const child = spawnSync("./node_modules/.bin/playwright", ["test", ...playwrightArgs(selectedArgs, [`--project=${project}`])], {
    stdio: "inherit",
    env: { ...environment, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: auditDir, OCTOPUS_E2E_SCOPE: scope,
      // Native UI can change its project filter after opening. Only this
      // prepared phase exists in its config, so it cannot cross state scopes.
      OCTOPUS_E2E_UI_PROJECT: interactive ? project : undefined },
  });
  if (child.error) console.error(child.error);
  rmSync(runtimeDir, { recursive: true, force: true });
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

let executed = false;
let status = 0;
for (const partition of plan) {
  const result = run(partition.project, partition.scope, partition.args);
  executed = true;
  if (result !== 0) { status = result; break; }
}
if (!executed) { console.error("No tests found"); status = 1; }
process.exitCode = status;
