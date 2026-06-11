/**
 * Vitest configuration with coverage thresholds matching the project quality bar
 * (≥90% lines, ≥85% branches). Unit tests live next to the API under tests/unit and
 * tests/integration at the repo root; both are included here.
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    setupFiles: ['../tests/setup.ts'],
    include: [
      '../tests/unit/**/*.test.ts',
      '../tests/integration/**/*.test.ts',
      'src/**/*.test.ts',
    ],
    testTimeout: 30_000,
    hookTimeout: 30_000,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.ts'],
      exclude: [
        'src/index.ts', // server bootstrap — exercised by e2e, not unit
        'src/services/websocket.ts', // raw socket plumbing — covered by the integration WS test
        'src/**/*.test.ts',
      ],
      thresholds: {
        lines: 90,
        branches: 85,
        functions: 90,
        statements: 90,
      },
    },
  },
});
