import type { BackendCatalog } from "../api";

export function backendCatalogFixture(
  available = ["claude-code", "codex"], preferred = "claude-code",
): BackendCatalog {
  return {
    supported: ["claude-code", "codex"], available, default: preferred,
    models: { "claude-code": [], codex: [] },
    tool_policy: { "claude-code": { allow: false, deny: true }, codex: { allow: false, deny: false } },
  };
}
