import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig, searchForWorkspaceRoot, type Plugin } from 'vite'
import { configDefaults } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitestEvidence } from './tests/vitestEvidence'
import { OFFLINE_VITE_HMR_PATH } from './e2e/vite-hmr'

const apiTarget = `http://localhost:${process.env.OCTOPUS_API_PORT || '8000'}`
const evidenceDirectory = process.env.OCTOPUS_VITEST_EVIDENCE_DIR
const evidence = evidenceDirectory ? new VitestEvidence(evidenceDirectory) : undefined
const evidencePlugin: Plugin | undefined = evidence && {
  name: 'octopus-vitest-evidence',
  configureVitest({ vitest }) {
    // The inline projects use this root config's instance even when Vite
    // reloads the inherited config for each project.
    if (!vitest.config.reporters.includes(evidence)) vitest.config.reporters.push(evidence)
  },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Two dev servers run at once during e2e (playwright.config.ts: one proxying
  // to the shared backend, one to the accounts backend). They share this
  // project, so they share `node_modules/.vite` — and each one's dependency
  // pre-bundling invalidates the other's, so both keep re-optimizing and
  // reloading every page they serve. A full `:fast` run took 9.8 minutes that
  // way instead of 45 seconds. A cache dir per server costs nothing and ends
  // it.
  cacheDir: process.env.VITE_CACHE_DIR || undefined,
  server: {
    ...(process.env.OCTOPUS_E2E_MODE === 'offline' && { hmr: { path: OFFLINE_VITE_HMR_PATH } }),
    // Worktrees reuse installed dependencies; font URLs resolve to that
    // directory's real path, which can be outside this checkout.
    fs: { allow: [searchForWorkspaceRoot(fileURLToPath(new URL('.', import.meta.url))),
      realpathSync(new URL('./node_modules', import.meta.url))] },
    proxy: {
      '/api': apiTarget,
      // Applications are served by the backend out of their own directories
      // (applications.md §3); the dev server has to forward them or the
      // in-app iframe 404s on :5173/:5174.
      '/apps': apiTarget,
      '/ws': { target: apiTarget, ws: true },
      '/health': apiTarget,
    },
  },
  test: {
    ...(evidence && { includeTaskLocation: true, onUnhandledError: evidence.onUnhandledError }),
    projects: [
      {
        extends: true,
        plugins: [evidencePlugin],
        test: {
          name: 'frontend',
          exclude: [...configDefaults.exclude, 'e2e/**', 'tests/**'],
          environment: 'jsdom',
          globals: true,
          setupFiles: './src/test-setup.ts',
        },
      },
      {
        extends: true,
        plugins: [evidencePlugin],
        test: {
          name: 'tooling',
          include: configDefaults.include.map(pattern => `tests/${pattern}`),
          environment: 'node',
        },
      },
    ],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/**/*.fixture.ts', 'src/test-setup.ts', 'src/test-support/**',
        'src/components/chatViewport.test-support.ts', 'src/api/contracts.ts'],
      reporter: ['text', 'json', 'json-summary', 'html'],
    },
  },
})
