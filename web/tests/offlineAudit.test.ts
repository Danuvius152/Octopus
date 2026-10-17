import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import { verifyOfflineAudit } from "../e2e/offline-audit";

const root = mkdtempSync(path.join(os.tmpdir(), "octopus-audit-test-"));
afterAll(() => rmSync(root, { recursive: true, force: true }));
function audit(name: string, events: string[]) {
  const file = path.join(root, name);
  writeFileSync(file, events.map((kind) => JSON.stringify({ kind })).join("\n"));
  return file;
}

describe("offline runner's final audit", () => {
  it("requires guarded offline startup on both servers and counts only test dispatch", () => {
    const main = audit("main", ["guard_installed", "offline_ready", "offline_dispatch"]);
    const accounts = audit("accounts", ["guard_installed", "offline_ready"]);
    expect(verifyOfflineAudit([main, accounts])).toEqual({ offlineDispatches: 1, realDispatches: 0 });
  });
  it("fails even when a backend caught a refusal and Playwright passed", () => {
    const caught = audit("caught", ["guard_installed", "offline_ready", "violation"]);
    expect(() => verifyOfflineAudit([caught])).toThrow("boundary rejected");
  });
  it("fails closed for missing, malformed or incomplete evidence", () => {
    expect(() => verifyOfflineAudit([path.join(root, "missing")])).toThrow();
    expect(() => verifyOfflineAudit([audit("incomplete", ["guard_installed"])] )).toThrow();
    const invalid = path.join(root, "invalid");
    writeFileSync(invalid, "not json");
    expect(() => verifyOfflineAudit([invalid])).toThrow();
  });
  it("makes the actual runner fail after a successful child swallowed a refusal", () => {
    const bin = path.join(root, "bin");
    mkdirSync(bin);
    const fake = path.join(bin, "bunx");
    // Safe Playwright double: no backend or model is started. It simulates a
    // passing test process whose backend caught a guard error.
    writeFileSync(fake, `#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
for (const server of ['main', 'accounts']) {
  const events = ['guard_installed', 'offline_ready', ...(server === 'main' ? ['violation'] : [])];
  fs.writeFileSync(path.join(process.env.OCTOPUS_E2E_AUDIT_DIR, server + '.jsonl'), events.map(kind => JSON.stringify({kind, reason: 'process_dispatch'})).join('\\n'));
}
process.exit(0);
`);
    chmodSync(fake, 0o700);
    const child = spawnSync("bun", ["e2e/run.ts", "offline"], {
      cwd: process.cwd(), encoding: "utf8",
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}` },
    });
    expect(child.status).toBe(1);
    expect(child.stderr).toContain("boundary rejected: process_dispatch");
  });
});
