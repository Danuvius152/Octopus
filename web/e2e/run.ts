import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { verifyOfflineAudit } from "./offline-audit";

const [mode, ...args] = process.argv.slice(2);
if (mode !== "offline" && mode !== "real") throw new Error("Choose offline or real E2E mode");
// Stay outside managed installation roots and CLI-restricted system temp dirs.
// Each run owns fresh mutable state, even after an interrupted prior run.
const testRoot = path.join(os.homedir(), ".cache", "octopus-tests");
mkdirSync(testRoot, { recursive: true, mode: 0o700 });
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
console.log(`E2E mode=${mode}; audit=${auditDir}`);
const child = spawnSync("./node_modules/.bin/playwright", ["test", ...args], {
  stdio: "inherit",
  env: { ...environment, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: auditDir },
});
if (child.error) console.error(child.error);
rmSync(runtimeDir, { recursive: true, force: true });
let status = child.status ?? 1;
// spawnSync returns after Playwright's webServer teardown: late, swallowed
// startup/cleanup violations count too. Missing/unreadable audits fail closed.
if (mode === "offline") {
  try {
    console.log("Offline execution audit:", verifyOfflineAudit(
      ["main", "accounts"].map((name) => path.join(auditDir, `${name}.jsonl`)),
    ));
  } catch (error) {
    console.error(error);
    status = 1;
  }
}
process.exitCode = status;
