import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("Playwright execution-mode boundary", () => {
  for (const mode of ["offline", "real"]) {
    for (const scope of [undefined, "main", "accounts"]) it(`${mode}/${scope ?? "collection"} prepares only its service pair`, async () => {
      vi.stubEnv("OCTOPUS_E2E_MODE", mode);
      vi.stubEnv("OCTOPUS_E2E_AUDIT_DIR", "/tmp/config-audit-only");
      vi.stubEnv("OCTOPUS_E2E_SCOPE", scope);
      const { default: config } = await import("../playwright.config");
      const servers = config.webServer as { command: string; port: number; reuseExistingServer: boolean }[];
      expect(servers).toHaveLength(2);
      expect(servers.map((server) => server.port)).toEqual(scope === "accounts" ? [8766, 5175] : [8765, 5174]);
      expect(servers.every((server) => server.reuseExistingServer === false)).toBe(true);
      expect(servers[0].command).toContain(mode === "offline" ? "tests.e2e_backend:app" : "server.main:app");
      expect(String(mode === "offline" ? config.grepInvert : config.grep)).toBe("/@llm/");
      const projects = config.projects!;
      expect(projects.map((project) => project.name)).toEqual(["chromium", "installation"]);
      expect(projects[1].workers).toBe(1);
      if (scope === "accounts") expect(projects[1].testMatch).toEqual(["**/accounts*.spec.ts"]);
      else {
        expect(projects[0].testIgnore).toEqual(projects[1].testMatch);
        expect(projects[1].testMatch).toContain("**/connectors.spec.ts");
        expect(projects[1].testMatch).toContain("**/agents.spec.ts");
        expect(projects[1].testMatch).toContain("**/wiki-job-cards.spec.ts");
        expect(projects[1].testIgnore).toEqual(scope === "main" ? ["**/accounts*.spec.ts"] : undefined);
      }
    });
  }
  it("rejects implicit mode instead of starting a default model backend", async () => {
    vi.stubEnv("OCTOPUS_E2E_MODE", undefined);
    await expect(import("../playwright.config")).rejects.toThrow("explicit execution mode");
  });
});
