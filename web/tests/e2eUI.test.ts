import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { expect, it } from "vitest";

const require = createRequire(import.meta.url);
const library = path.join(path.dirname(require.resolve("playwright/package.json")), "lib");
const config = path.resolve("playwright.config.ts");

function runUI(args: string[], audit = "none", packageEntry = false) {
  const cache = path.join(os.homedir(), ".cache", "octopus-tests");
  mkdirSync(cache, { recursive: true });
  const root = mkdtempSync(path.join(cache, "ui-protocol-"));
  try {
    const bin = path.join(root, "node_modules", ".bin");
    mkdirSync(bin, { recursive: true });
    if (packageEntry) {
      const script = JSON.parse(readFileSync(path.resolve("package.json"), "utf8")).scripts["test:e2e:ui"];
      writeFileSync(path.join(root, "package.json"), JSON.stringify({ scripts: { "test:e2e:ui": script } }));
      symlinkSync(path.resolve("e2e"), path.join(root, "e2e"));
    }
    // Run the installed CLI, parser, config loader and real test collection.
    // Only its GUI execution boundary is replaced: no window, server, model
    // or dependency installation is needed to inspect the entry protocol.
    writeFileSync(path.join(bin, "playwright"), `#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const library = ${JSON.stringify(library)};
const serverPath = path.join(library, 'runner/testServer.js');
const server = require(serverPath);
require.cache[serverPath].exports = { ...server, runUIMode: async (file, overrides, options) => {
  const loaded = await require(path.join(library, 'common/configLoader.js')).loadConfigFromFile(file, overrides);
  console.log('UI_PROTOCOL ' + JSON.stringify({ options, scope: process.env.OCTOPUS_E2E_SCOPE,
    projects: loaded.projects.map(project => project.project.name), workers: loaded.config.workers }));
  const audit = process.env.UI_PROTOCOL_AUDIT;
  if (audit !== 'none') {
    const scope = audit === 'wrong-scope' ? 'wrong-scope' : process.env.OCTOPUS_E2E_SCOPE;
    const events = ['guard_installed', 'offline_ready', ...(audit === 'restart' ? ['guard_installed', 'offline_ready'] : []),
      ...(audit === 'refusal' ? ['violation'] : [])];
    fs.writeFileSync(path.join(process.env.OCTOPUS_E2E_AUDIT_DIR, scope + '.jsonl'),
      events.map(kind => JSON.stringify({kind, reason: 'process_dispatch'})).join('\\n'));
  }
  return 'passed';
} };
require(path.join(library, 'program.js')).program.parseAsync(process.argv);
`, { mode: 0o700 });
    const command = packageEntry ? ["run", "test:e2e:ui"] : [path.resolve("e2e/run.ts"), "offline"];
    return spawnSync("bun", [...command, `--config=${config}`, ...args], {
      cwd: root, encoding: "utf8", env: { ...process.env, UI_PROTOCOL_AUDIT: audit },
    });
  } finally { rmSync(root, { recursive: true, force: true }); }
}

interface Phase { options: { args: string[]; project: string[]; host?: string; port?: number; reporter?: string[] }; scope: string; projects: string[]; workers: number }
function phases(stdout: string): Phase[] {
  return stdout.split("\n").filter(line => line.startsWith("UI_PROTOCOL ")).map(line => JSON.parse(line.slice(12)));
}

it("keeps the actual package UI entry on the same scoped runner", () => {
  const child = runUI(["app.spec.ts"], "none", true);
  expect(child.status, child.stderr || child.stdout).toBe(0);
  expect(phases(child.stdout).map(({ scope, projects }) => [scope, projects])).toEqual([["main", ["chromium"]]]);
}, 15_000);

for (const [label, flags, host, port] of [
  ["UI window", ["--ui"], undefined, undefined],
  ["inline host and port", ["--ui-host=127.0.0.1", "--ui-port=0"], "127.0.0.1", 0],
  ["separate host and port", ["--ui-host", "127.0.0.1", "--ui-port", "0"], "127.0.0.1", 0],
  ["host alone", ["--ui-host=127.0.0.1"], "127.0.0.1", undefined],
  ["port alone", ["--ui-port", "0"], undefined, 0],
] as const) it(`collects before opening the native ${label} and confines each UI phase`, () => {
  const child = runUI(["app.spec.ts", "connectors.spec.ts", "accounts.spec.ts", ...flags]);
  expect(child.status, child.stderr || child.stdout).toBe(0);
  const opened = phases(child.stdout);
  expect(opened.map(({ scope, projects }) => [scope, projects])).toEqual([
    ["main", ["chromium"]], ["main", ["installation"]], ["accounts", ["installation"]],
  ]);
  for (const phase of opened) {
    expect(phase.options.args).toEqual(["app.spec.ts", "connectors.spec.ts", "accounts.spec.ts"]);
    expect(phase.options.project).toEqual(phase.projects);
    expect(phase.options.reporter).toBeUndefined();
    expect(phase.options.host).toBe(host);
    expect(phase.options.port).toBe(port);
    if (phase.projects[0] === "installation") expect(phase.workers).toBe(1);
  }
  expect(child.stdout.match(/no offline test execution was audited/g)).toHaveLength(3);
}, 20_000);

it("preserves the UI option boundary after native variadic project selection", () => {
  const child = runUI(["--project", "chromium", "--ui-port", "0", "app.spec.ts", "connectors.spec.ts"]);
  expect(child.status, child.stderr || child.stdout).toBe(0);
  expect(phases(child.stdout).map(({ projects, options }) => [projects, options.args])).toEqual([
    [["chromium"], ["app.spec.ts", "connectors.spec.ts"]],
  ]);
}, 15_000);

it("leaves interactive repetitions to the UI instead of scheduling repeated empty-install windows", () => {
  const child = runUI(["app.spec.ts", "accounts.spec.ts", "--ui", "--repeat-each=6"]);
  expect(child.status, child.stderr || child.stdout).toBe(0);
  expect(phases(child.stdout).map(({ scope, projects }) => [scope, projects])).toEqual([
    ["main", ["chromium"]], ["accounts", ["installation"]],
  ]);
}, 15_000);

it("audits native UI setup restarts and still rejects swallowed refusals or wrong-scope evidence", () => {
  const args = ["app.spec.ts", "--ui"];
  const restarted = runUI(args, "restart");
  expect(restarted.status, restarted.stderr || restarted.stdout).toBe(0);
  expect(restarted.stdout).toContain("Offline execution audit:");
  const refused = runUI(args, "refusal");
  expect(refused.status).toBe(1);
  expect(refused.stderr).toContain("boundary rejected: process_dispatch");
  const wrong = runUI(args, "wrong-scope");
  expect(wrong.status).toBe(1);
  expect(wrong.stderr).toContain("main.jsonl");
}, 20_000);
