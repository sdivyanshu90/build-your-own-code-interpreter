/**
 * Vitest config for the end-to-end suite. Lives in api/ (not next to the tests) so that
 * `vitest/config` resolves from api/node_modules; run with `cd api && npx vitest run --config
 * vitest.e2e.config.ts` (`make test-e2e`).
 * These tests run against a LIVE stack (docker compose
 * up) reachable at API_BASE_URL (default http://localhost:8080) and skip themselves when the
 * server is not reachable. Run sequentially with generous timeouts (real container starts).
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['../tests/e2e/**/*.test.ts'],
    testTimeout: 60_000,
    hookTimeout: 60_000,
    pool: 'forks',
    fileParallelism: false,
  },
});
