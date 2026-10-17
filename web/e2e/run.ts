import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { verifyOfflineAudit } from "./offline-audit";

const [mode, ...args] = process.argv.slice(2);
if (mode !== "offline" && mode !== "real") throw new Error("Choose offline or real E2E mode");
// Keep evidence outside the directories global-teardown removes. A fresh
// directory prevents a previous successful run from satisfying this guard.
const auditDir = mkdtempSync(path.join(os.tmpdir(), "octopus-e2e-audit-"));
console.log(`E2E mode=${mode}; audit=${auditDir}`);
const child = spawnSync("bunx", ["playwright", "test", ...args], {
  stdio: "inherit",
  env: { ...process.env, OCTOPUS_E2E_MODE: mode, OCTOPUS_E2E_AUDIT_DIR: auditDir },
});
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
