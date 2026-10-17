import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("Playwright execution-mode boundary", () => {
  for (const mode of ["offline", "real"]) {
    it(`${mode} starts fresh servers with the matching execution entrypoint`, async () => {
      vi.stubEnv("OCTOPUS_E2E_MODE", mode);
      vi.stubEnv("OCTOPUS_E2E_AUDIT_DIR", "/tmp/config-audit-only");
      const { default: config } = await import("../playwright.config");
      const servers = config.webServer as { command: string; reuseExistingServer: boolean }[];
      expect(servers).toHaveLength(4);
      expect(servers.every((server) => server.reuseExistingServer === false)).toBe(true);
      for (const server of [servers[0], servers[2]]) {
        expect(server.command).toContain(mode === "offline" ? "tests.e2e_backend:app" : "server.main:app");
      }
      expect(String(mode === "offline" ? config.grepInvert : config.grep)).toBe("/@llm/");
    });
  }
  it("rejects implicit mode instead of starting a default model backend", async () => {
    vi.stubEnv("OCTOPUS_E2E_MODE", undefined);
    await expect(import("../playwright.config")).rejects.toThrow("explicit execution mode");
  });
});
