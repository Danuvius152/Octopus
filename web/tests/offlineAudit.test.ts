import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
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

function runnerDouble(refusal: boolean) {
  const runnerRoot = mkdtempSync(path.join(root, "runner-"));
  const bin = path.join(runnerRoot, "node_modules", ".bin");
  const inherited = path.join(runnerRoot, "inherited-temp");
  mkdirSync(bin, { recursive: true });
  mkdirSync(inherited);
  const fake = path.join(bin, "playwright");
  // No backend or model is started. The double exercises the real runner's
  // environment and final audit, including a swallowed boundary refusal.
  writeFileSync(fake, `#!/usr/bin/env node
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
if (process.argv.includes('--list')) {
  console.log(JSON.stringify({ suites: [{ specs: [{ file: 'audit-probe.spec.ts', tests: [{ projectName: 'chromium' }] }] }], errors: [] }));
  process.exit(0);
}
const root = os.tmpdir();
if (process.env.CODEX_HOME.startsWith(root + path.sep)) throw new Error('CLI home is inside its temporary directory');
if (fs.existsSync(path.join(root, 'prior-run'))) throw new Error('Inherited stale mutable state');
fs.writeFileSync(path.join(root, 'prior-run'), 'test state');
console.log('ISOLATION ' + JSON.stringify({root, homes: [process.env.CODEX_HOME, process.env.CLAUDE_CONFIG_DIR, process.env.OCTOPUS_CODEX_HOME_DIR, process.env.OCTOPUS_LARGE_PROMPTS_DIR]}));
for (const server of [process.env.OCTOPUS_E2E_SCOPE]) {
  const events = ['guard_installed', 'offline_ready', ...(${refusal} && server === 'main' ? ['violation'] : [])];
  fs.writeFileSync(path.join(process.env.OCTOPUS_E2E_AUDIT_DIR, server + '.jsonl'), events.map(kind => JSON.stringify({kind, reason: 'process_dispatch'})).join('\\n'));
}
process.exit(0);
`);
  chmodSync(fake, 0o700);
  return {
    inherited,
    run: () => spawnSync("bun", [path.resolve("e2e/run.ts"), "offline"], {
      cwd: runnerRoot, encoding: "utf8", env: { ...process.env, TMPDIR: inherited },
    }),
  };
}

describe("offline runner's final audit", () => {
  it("requires guarded offline startup on every selected server and counts only test dispatch", () => {
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
  it("checks every native UI cleanup/setup cycle without relaxing ordinary runs", () => {
    const restarted = audit("restarted", ["guard_installed", "offline_ready", "offline_dispatch", "guard_installed", "offline_ready"]);
    expect(() => verifyOfflineAudit([restarted])).toThrow("missing or duplicated");
    expect(verifyOfflineAudit([restarted], { allowRestarts: true })).toEqual({ offlineDispatches: 1, realDispatches: 0 });
    for (const events of [
      ["guard_installed", "offline_ready", "guard_installed"],
      ["guard_installed", "guard_installed", "offline_ready"],
      ["offline_dispatch", "guard_installed", "offline_ready"],
      ["guard_installed", "offline_ready", "guard_installed", "offline_dispatch", "offline_ready"],
      ["guard_installed", "offline_ready", "guard_installed", "offline_ready", "violation"],
    ]) expect(() => verifyOfflineAudit([audit("invalid-cycle", events)], { allowRestarts: true })).toThrow();
  });
  it("makes the actual runner fail after a successful child swallowed a refusal", () => {
    const child = runnerDouble(true).run();
    expect(child.status).toBe(1);
    expect(child.stderr).toContain("boundary rejected: process_dispatch");
  });
  it("isolates successive runs from inherited temporary state and retires their runtime", () => {
    const runner = runnerDouble(false);
    writeFileSync(path.join(runner.inherited, "prior-run"), "interrupted run");
    const roots: string[] = [];
    for (let iteration = 0; iteration < 2; iteration++) {
      const child = runner.run();
      expect(child.status, child.stderr).toBe(0);
      const evidence = JSON.parse(child.stdout.split("\n").find(line => line.startsWith("ISOLATION "))!.slice(10)) as { root: string; homes: string[] };
      expect(evidence.root).not.toBe(runner.inherited);
      const owner = path.dirname(evidence.root);
      expect(evidence.homes.every(home => home.startsWith(owner + path.sep))).toBe(true);
      expect(evidence.homes.every(home => !home.startsWith(evidence.root + path.sep))).toBe(true);
      expect(existsSync(owner)).toBe(false);
      roots.push(owner);
    }
    expect(roots[0]).not.toBe(roots[1]);
  });
});
