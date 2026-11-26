import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { expect, it } from "vitest";

it("retains raw Playwright module import errors when no offline partition starts", () => {
  const cache = path.join(os.homedir(), ".cache", "octopus-tests");
  mkdirSync(cache, { recursive: true });
  const root = mkdtempSync(path.join(cache, "collection-evidence-"));
  try {
    const config = path.join(root, "playwright.config.ts");
    const spec = path.join(root, "broken-import.spec.ts");
    const evidence = path.join(root, "evidence");
    writeFileSync(config, `console.log("before import failure");\nexport default { testDir: ".", projects: [{ name: "chromium" }] };\n`);
    writeFileSync(spec, `import "./absent-fixture";\n`);
    const result = spawnSync("bun", ["run", "test:e2e:fast", `--config=${config}`, "broken-import.spec.ts"], {
      encoding: "utf8",
      env: { ...process.env, OCTOPUS_E2E_EVIDENCE_DIR: evidence },
    });
    expect(result.error).toBeUndefined();
    expect(result.status, result.stderr).toBe(1);
    const run = JSON.parse(readFileSync(path.join(evidence, "run.json"), "utf8"));
    expect(run.argv).toContain(`--config=${config}`);
    expect(run.candidate.working_tree_sha256).toMatch(/^[a-f0-9]{64}$/);
    const after = JSON.parse(readFileSync(path.join(evidence, "inputs-after.json"), "utf8"));
    expect(after.inputs_changed).toBe(false);
    expect(after.candidate.working_tree_sha256).toBe(run.candidate.working_tree_sha256);
    const collection = path.join(evidence, "collection");
    const events = readFileSync(path.join(collection, "events.jsonl"), "utf8").trim().split("\n").map((line) => JSON.parse(line));
    expect(events).toContainEqual(expect.objectContaining({
      event: "error", phase: "collection", errors: [expect.objectContaining({
        message: expect.stringContaining("Cannot find module './absent-fixture'"),
        stack: expect.stringContaining(spec),
      })],
    }));
    const report = JSON.parse(readFileSync(path.join(collection, "playwright.json"), "utf8"));
    expect(report.errors).toContainEqual(expect.objectContaining({ message: expect.stringContaining("Cannot find module './absent-fixture'") }));
    expect(readFileSync(path.join(collection, "stdout.log"), "utf8")).toContain("before import failure");
    expect(existsSync(path.join(evidence, "result.json"))).toBe(false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}, 15_000);
