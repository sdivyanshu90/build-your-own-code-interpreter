/**
 * Vitest config for the end-to-end suite. These tests run against a LIVE stack (docker compose
 * up) reachable at API_BASE_URL (default http://localhost:8080) and skip themselves when the
 * server is not reachable. Run sequentially with generous timeouts (real container starts).
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['**/*.test.ts'],
    testTimeout: 60_000,
    hookTimeout: 60_000,
    pool: 'forks',
    fileParallelism: false,
  },
});
