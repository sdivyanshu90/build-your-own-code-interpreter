/** Unit tests for the in-house HS256 JWT implementation. */
import { describe, it, expect } from 'vitest';
import { signJwt, verifyJwt, JwtError } from '../../api/src/services/jwt.js';

const SECRET = 'a-test-secret-that-is-at-least-32-chars';

describe('jwt', () => {
  it('signs and verifies a round-trip token', () => {
    const token = signJwt({ sub: 'u1', tier: 'premium' }, SECRET);
    const payload = verifyJwt(token, SECRET);
    expect(payload.sub).toBe('u1');
    expect(payload.tier).toBe('premium');
    expect(typeof payload.iat).toBe('number');
  });

  it('adds an exp claim when expiresInSec is given', () => {
    const token = signJwt({ sub: 'u' }, SECRET, { expiresInSec: 60, now: () => 1_000_000 });
    const payload = verifyJwt(token, SECRET, () => 1_000_000);
    expect(payload.exp).toBe(1000 + 60);
  });

  it('rejects an expired token', () => {
    const token = signJwt({ sub: 'u' }, SECRET, { expiresInSec: 1, now: () => 0 });
    expect(() => verifyJwt(token, SECRET, () => 10_000)).toThrow(JwtError);
  });

  it('rejects a token signed with a different secret', () => {
    const token = signJwt({ sub: 'u' }, 'another-secret-that-is-32-characters!!');
    expect(() => verifyJwt(token, SECRET)).toThrow(/signature/);
  });

  it('rejects a tampered payload', () => {
    const token = signJwt({ sub: 'u' }, SECRET);
    const [h, , s] = token.split('.');
    const forged = `${h}.${Buffer.from('{"sub":"admin"}').toString('base64url')}.${s}`;
    expect(() => verifyJwt(forged, SECRET)).toThrow(JwtError);
  });

  it('rejects a malformed token', () => {
    expect(() => verifyJwt('not.a.jwt.token', SECRET)).toThrow(JwtError);
    expect(() => verifyJwt('only-one-part', SECRET)).toThrow(JwtError);
  });

  it('rejects an unsupported algorithm (alg confusion)', () => {
    const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
    const body = Buffer.from(JSON.stringify({ sub: 'admin' })).toString('base64url');
    expect(() => verifyJwt(`${header}.${body}.`, SECRET)).toThrow(/algorithm/);
  });
});
