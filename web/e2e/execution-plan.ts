import { spawnSync } from "node:child_process";
import { closeSync, existsSync, openSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

export interface Partition { project: string; scope: "main" | "accounts"; args: string[] }
interface CollectionEvidence { directory: string; reporter: string }
interface Selection { project: string; file: string }
interface Suite {
  specs?: { file: string; tests: { projectName: string }[] }[];
  suites?: Suite[];
}

function collectedTests(suites: Suite[]): Selection[] {
  return suites.flatMap((suite) => [
    ...(suite.specs ?? []).flatMap((spec) => spec.tests.map((test) => ({ project: test.projectName, file: spec.file }))),
    ...collectedTests(suite.suites ?? []),
  ]);
}

/** Options belong before the CLI's positional-only separator, if present. */
export function playwrightArgs(args: string[], options: string[]): string[] {
  const separator = args.indexOf("--");
  return separator < 0 ? [...args, ...options]
    : [...args.slice(0, separator), ...options, ...args.slice(separator)];
}

/** Keep native UI flags for execution, never for internal JSON collection.
 * --list placeholders preserve boundaries after variadic --project options. */
export function uiModeArgs(args: string[]): { collectionArgs: string[]; interactive: boolean } {
  const collectionArgs: string[] = [];
  let interactive = false;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--") { collectionArgs.push(...args.slice(i)); break; }
    if (arg === "--ui" || /^--ui-(host|port)=/.test(arg)) {
      interactive = true;
      collectionArgs.push("--list");
    } else if (arg === "--ui-host" || arg === "--ui-port") {
      if (!args[i + 1] || args[i + 1].startsWith("--")) throw new Error(`${arg} requires an argument`);
      i++;
      interactive = true;
      collectionArgs.push("--list");
    } else collectionArgs.push(arg);
  }
  return { collectionArgs, interactive };
}

/** Ask Playwright to resolve its own single/multi/wildcard project selection
 * and file/grep filters. Unknown projects and empty selections still fail in
 * Playwright. No second project selector is added to this authoritative read. */
export function collectExecutionPlan(args: string[], environment: NodeJS.ProcessEnv, evidence?: CollectionEvidence): Partition[] {
  const { collectionArgs } = uiModeArgs(args);
  const evidenceDir = evidence?.directory;
  const command = ["./node_modules/.bin/playwright", "test", ...playwrightArgs(collectionArgs, ["--list", "--reporter=json"])];
  const nativeReport = evidenceDir ? path.join(evidenceDir, "playwright.json") : undefined;
  const stdout = evidenceDir ? path.join(evidenceDir, "stdout.log") : undefined;
  const stderr = evidenceDir ? path.join(evidenceDir, "stderr.log") : undefined;
  const logs: number[] = [];
  let child;
  try {
    if (evidenceDir) {
      writeFileSync(path.join(evidenceDir, "argv.json"), JSON.stringify(command) + "\n", { mode: 0o600, flush: true });
    }
    // Also retain CLI/config errors emitted before Playwright loads reporters.
    if (stdout && stderr) {
      logs.push(openSync(stdout, "wx", 0o600));
      logs.push(openSync(stderr, "wx", 0o600));
    }
    child = spawnSync(command[0], command.slice(1), {
      encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
      stdio: logs.length ? ["ignore", ...logs] : undefined,
      env: { ...environment, OCTOPUS_E2E_SCOPE: undefined, OCTOPUS_E2E_UI_PROJECT: undefined,
        OCTOPUS_E2E_EVENTS_FILE: evidenceDir && path.join(evidenceDir, "events.jsonl"),
        OCTOPUS_E2E_EVIDENCE_PHASE: evidenceDir && "collection",
        ...(evidence && { PW_TEST_REPORTER: evidence.reporter }),
        // Internal collection must not replace the caller's execution report.
        PLAYWRIGHT_JSON_OUTPUT_FILE: nativeReport, PLAYWRIGHT_JSON_OUTPUT_DIR: undefined, PLAYWRIGHT_JSON_OUTPUT_NAME: undefined },
    });
  } finally {
    for (const descriptor of logs) closeSync(descriptor);
  }
  if (child.error) throw child.error;
  const reportText = nativeReport ? (existsSync(nativeReport) ? readFileSync(nativeReport, "utf8") : "") : child.stdout;
  const report = reportText ? JSON.parse(reportText) as { suites: Suite[]; errors: { message?: string; value?: string }[] } : undefined;
  if (child.status !== 0 || !report || report.errors.length) {
    const errors = report?.errors.map((error) => error.message ?? error.value).filter(Boolean).join("\n");
    throw new Error(errors || (stderr ? readFileSync(stderr, "utf8") : child.stderr)
      || (stdout && readFileSync(stdout, "utf8")) || "Playwright collection failed");
  }
  return executionPlan(args, collectedTests(report.suites));
}

/** Reuse services across repeats. Only first-account stories need an empty
 * installation per repetition; their separate service pair is prepared alone. */
export function executionPlan(args: string[], selected: Selection[]): Partition[] {
  const { interactive } = uiModeArgs(args);
  // Bare --project is variadic; --project=value consumes only its inline value.
  // Matching is resolved above; children receive only their exact target.
  const selectedArgs: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--") { selectedArgs.push(...args.slice(i)); break; }
    if (args[i] === "--project") {
      while (i + 1 < args.length && !args[i + 1].startsWith("-")) i++;
    } else if (!args[i].startsWith("--project=")) selectedArgs.push(args[i]);
  }
  let repeats = 1;
  const exclusiveArgs: string[] = [];
  const accountsArgs: string[] = [];
  for (let i = 0; i < selectedArgs.length; i++) {
    if (selectedArgs[i] === "--") { exclusiveArgs.push(...selectedArgs.slice(i)); break; }
    if (selectedArgs[i] === "--workers" || selectedArgs[i] === "-j") i++;
    else if (!selectedArgs[i].startsWith("--workers=")) exclusiveArgs.push(selectedArgs[i]);
  }
  for (let i = 0; i < exclusiveArgs.length; i++) {
    if (exclusiveArgs[i] === "--") { accountsArgs.push(...exclusiveArgs.slice(i)); break; }
    if (exclusiveArgs[i].startsWith("--repeat-each=")) repeats = Number(exclusiveArgs[i].split("=")[1]);
    else if (exclusiveArgs[i] === "--repeat-each") repeats = Number(exclusiveArgs[++i]);
    else accountsArgs.push(exclusiveArgs[i]);
  }
  if (!Number.isInteger(repeats) || repeats < 1) throw new Error("--repeat-each must be a positive integer");
  const projects = new Set(selected.map((test) => test.project));
  if ([...projects].some((project) => !["chromium", "installation"].includes(project))) throw new Error("Project has no E2E isolation partition");
  const accountStory = (file: string) => /(?:^|\/)accounts[^/]*\.spec\.ts$/.test(file);
  return [
    ...(projects.has("chromium") ? [{ project: "chromium", scope: "main" as const, args: selectedArgs }] : []),
    ...(selected.some((test) => test.project === "installation" && !accountStory(test.file))
      ? [{ project: "installation", scope: "main" as const, args: playwrightArgs(exclusiveArgs, ["--workers=1"]) }] : []),
    ...(selected.some((test) => test.project === "installation" && accountStory(test.file))
      ? Array.from({ length: interactive ? 1 : repeats }, () => ({ project: "installation", scope: "accounts" as const,
        args: playwrightArgs(accountsArgs, ["--workers=1", "--repeat-each=1"]) })) : []),
  ];
}
