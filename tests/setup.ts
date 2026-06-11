/**
 * Vitest global setup for the API test suites.
 *
 * Provides safe default environment variables (so config validation passes), and exports test
 * helpers: JWT minting, the in-memory FakeRedis, and small fixtures used across files.
 */
import { signJwt } from '../api/src/services/jwt.js';

// ── Environment defaults (applied before any module reads config) ──────────────────────────────
process.env.NODE_ENV ??= 'test';
process.env.REDIS_URL ??= 'redis://localhost:6379/15';
process.env.MINIO_ENDPOINT ??= 'localhost';
process.env.MINIO_ACCESS_KEY ??= 'minioadmin';
process.env.MINIO_SECRET_KEY ??= 'minioadmin123';
process.env.JWT_SECRET ??= 'test-jwt-secret-at-least-32-characters-long';
process.env.ALLOW_ANONYMOUS ??= 'false';
process.env.CORS_ORIGINS ??= 'http://localhost:3000';
process.env.RATE_LIMIT_REQUESTS_PER_MINUTE ??= '60';
process.env.RATE_LIMIT_ANON_PER_MINUTE ??= '10';
process.env.RATE_LIMIT_PREMIUM_PER_MINUTE ??= '600';

export const TEST_JWT_SECRET = process.env.JWT_SECRET;

/** Mint a JWT for tests. */
export function makeToken(sub: string, tier: 'authenticated' | 'premium' = 'authenticated'): string {
  return signJwt({ sub, tier }, TEST_JWT_SECRET, { expiresInSec: 3600 });
}

/** A valid authenticated-user token. */
export const authToken = (): string => makeToken('user-123', 'authenticated');

/** A valid premium-user token. */
export const premiumAuthToken = (): string => makeToken('premium-456', 'premium');

/** An invalid token (wrong signature). */
export const invalidAuthToken = (): string =>
  signJwt({ sub: 'x' }, 'a-different-wrong-secret-32-characters-xx', { expiresInSec: 3600 });

export { FakeRedis } from './helpers/fakeRedis.js';
