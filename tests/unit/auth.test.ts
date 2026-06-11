/** Unit tests for principal authentication (API key, JWT, anonymous). */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { authenticatePrincipal, headerBag, AuthError } from '../../api/src/middleware/auth.js';
import { resetConfigForTests } from '../../api/src/config.js';
import { signJwt } from '../../api/src/services/jwt.js';
import { TEST_JWT_SECRET } from '../setup.js';

function bag(headers: Record<string, string>): ReturnType<typeof headerBag> {
  return headerBag(headers);
}

describe('authenticatePrincipal', () => {
  beforeEach(() => {
    process.env.API_KEYS = 'svc-key:premium,ci-key:authenticated';
    process.env.ALLOW_ANONYMOUS = 'false';
    resetConfigForTests();
  });

  afterEach(() => {
    delete process.env.API_KEYS;
    process.env.ALLOW_ANONYMOUS = 'false';
    resetConfigForTests();
  });

  it('authenticates a valid API key with its tier', () => {
    const p = authenticatePrincipal(bag({ 'x-api-key': 'svc-key' }));
    expect(p.tier).toBe('premium');
    expect(p.auth_method).toBe('api_key');
  });

  it('rejects an unknown API key with 403', () => {
    expect(() => authenticatePrincipal(bag({ 'x-api-key': 'nope' }))).toThrow(AuthError);
    try {
      authenticatePrincipal(bag({ 'x-api-key': 'nope' }));
    } catch (e) {
      expect((e as AuthError).status).toBe(403);
    }
  });

  it('authenticates a valid JWT and maps the tier', () => {
    const token = signJwt({ sub: 'user-9', tier: 'premium' }, TEST_JWT_SECRET, { expiresInSec: 60 });
    const p = authenticatePrincipal(bag({ authorization: `Bearer ${token}` }));
    expect(p.user_id).toBe('user-9');
    expect(p.tier).toBe('premium');
    expect(p.auth_method).toBe('jwt');
  });

  it('rejects an invalid JWT with 403', () => {
    expect(() => authenticatePrincipal(bag({ authorization: 'Bearer bad.token.here' }))).toThrow(AuthError);
  });

  it('rejects a JWT without a sub claim', () => {
    const token = signJwt({ tier: 'authenticated' }, TEST_JWT_SECRET, { expiresInSec: 60 });
    expect(() => authenticatePrincipal(bag({ authorization: `Bearer ${token}` }))).toThrow(/sub/);
  });

  it('accepts a JWT via the query token (browser WS clients)', () => {
    const token = signJwt({ sub: 'ws-user' }, TEST_JWT_SECRET, { expiresInSec: 60 });
    const p = authenticatePrincipal(bag({}), token);
    expect(p.user_id).toBe('ws-user');
    expect(p.tier).toBe('authenticated'); // default tier
  });

  it('throws 401 when no credentials and anonymous is disabled', () => {
    try {
      authenticatePrincipal(bag({}));
      throw new Error('should have thrown');
    } catch (e) {
      expect((e as AuthError).status).toBe(401);
    }
  });

  it('returns an anonymous principal when anonymous is enabled', () => {
    process.env.ALLOW_ANONYMOUS = 'true';
    resetConfigForTests();
    const p = authenticatePrincipal(bag({}));
    expect(p.tier).toBe('anonymous');
    expect(p.auth_method).toBe('anonymous');
  });
});
