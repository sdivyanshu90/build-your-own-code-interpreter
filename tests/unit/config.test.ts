/** Unit tests for environment configuration parsing and validation. */
import { describe, it, expect } from 'vitest';
import { loadConfig } from '../../api/src/config.js';

const VALID = {
  REDIS_URL: 'redis://localhost:6379/0',
  MINIO_ENDPOINT: 'localhost',
  MINIO_ACCESS_KEY: 'minioadmin',
  MINIO_SECRET_KEY: 'minioadmin123',
  JWT_SECRET: 'a-strong-secret-at-least-32-characters-xx',
};

describe('loadConfig', () => {
  it('parses a valid minimal environment with defaults', () => {
    const cfg = loadConfig(VALID as NodeJS.ProcessEnv);
    expect(cfg.PORT).toBe(8080);
    expect(cfg.DEFAULT_TIMEOUT_SECONDS).toBe(10);
    expect(cfg.CORS_ORIGINS).toEqual(['http://localhost:3000']);
  });

  it('throws when REDIS_URL is missing', () => {
    const { REDIS_URL: _omit, ...env } = VALID;
    expect(() => loadConfig(env as NodeJS.ProcessEnv)).toThrow(/REDIS_URL/);
  });

  it('throws on a short JWT secret', () => {
    expect(() => loadConfig({ ...VALID, JWT_SECRET: 'too-short' } as NodeJS.ProcessEnv)).toThrow(/JWT_SECRET/);
  });

  it('throws on a well-known weak JWT secret', () => {
    expect(() =>
      loadConfig({ ...VALID, JWT_SECRET: 'changeme'.padEnd(32, 'x') } as NodeJS.ProcessEnv),
    ).not.toThrow(); // padded value is not in the denylist
    expect(() =>
      loadConfig({ ...VALID, JWT_SECRET: 'changeme', } as NodeJS.ProcessEnv),
    ).toThrow();
  });

  it('rejects CORS "*" in production', () => {
    expect(() =>
      loadConfig({ ...VALID, NODE_ENV: 'production', CORS_ORIGINS: '*' } as NodeJS.ProcessEnv),
    ).toThrow(/CORS_ORIGINS/);
  });

  it('rejects MAX_TIMEOUT < DEFAULT_TIMEOUT', () => {
    expect(() =>
      loadConfig({ ...VALID, DEFAULT_TIMEOUT_SECONDS: '30', MAX_TIMEOUT_SECONDS: '10' } as NodeJS.ProcessEnv),
    ).toThrow(/MAX_TIMEOUT_SECONDS/);
  });

  it('parses API keys into a tiered map', () => {
    const cfg = loadConfig({ ...VALID, API_KEYS: 'k1:premium,k2:authenticated,k3' } as NodeJS.ProcessEnv);
    expect(cfg.API_KEY_MAP.get('k1')).toBe('premium');
    expect(cfg.API_KEY_MAP.get('k2')).toBe('authenticated');
    expect(cfg.API_KEY_MAP.get('k3')).toBe('authenticated'); // default tier
  });

  it('splits CORS origins on commas', () => {
    const cfg = loadConfig({ ...VALID, CORS_ORIGINS: 'https://a.com, https://b.com' } as NodeJS.ProcessEnv);
    expect(cfg.CORS_ORIGINS).toEqual(['https://a.com', 'https://b.com']);
  });

  it('coerces numeric and boolean fields', () => {
    const cfg = loadConfig({ ...VALID, PORT: '9999', ALLOW_ANONYMOUS: 'true', MINIO_USE_SSL: 'true' } as NodeJS.ProcessEnv);
    expect(cfg.PORT).toBe(9999);
    expect(cfg.ALLOW_ANONYMOUS).toBe(true);
    expect(cfg.MINIO_USE_SSL).toBe(true);
  });
});
