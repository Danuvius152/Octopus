import { readFileSync } from "node:fs";

export function verifyOfflineAudit(files: string[], { allowRestarts = false } = {}): { offlineDispatches: number; realDispatches: number } {
  let offlineDispatches = 0;
  for (const file of files) {
    const events = readFileSync(file, "utf8").trim().split("\n").map((line) => JSON.parse(line) as { kind: string; reason?: string });
    let starts = 0;
    let phase: "empty" | "guarded" | "ready" = "empty";
    let valid = true;
    for (const event of events) {
      if (event.kind === "guard_installed") {
        valid &&= phase !== "guarded" && (allowRestarts || starts === 0);
        starts++;
        phase = "guarded";
      } else if (event.kind === "offline_ready") {
        valid &&= phase === "guarded";
        phase = "ready";
      } else if (event.kind === "offline_dispatch") valid &&= phase === "ready";
    }
    if (!valid || !starts || phase !== "ready") {
      throw new Error("Offline backend guard/readiness audit missing or duplicated");
    }
    const violations = events.filter((event) => event.kind === "violation");
    if (violations.length) {
      throw new Error(`Offline execution boundary rejected: ${violations.map((event) => event.reason).join(", ")}`);
    }
    if (events.some((event) => !["guard_installed", "offline_ready", "offline_dispatch"].includes(event.kind))) {
      throw new Error("Unknown offline audit event");
    }
    offlineDispatches += events.filter((event) => event.kind === "offline_dispatch").length;
  }
  return { offlineDispatches, realDispatches: 0 };
}
