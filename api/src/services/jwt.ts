/**
 * Minimal, dependency-free HS256 JWT signing and verification using node:crypto.
 *
 * We intentionally avoid a third-party JWT library: HS256 is a few lines of HMAC over
 * `base64url(header).base64url(payload)`, and keeping it in-house removes a CJS dependency,
 * shrinks the attack surface, and gives us exact control over claim validation. Only HS256 is
 * supported; the algorithm in the header is verified to prevent algorithm-confusion attacks.
 */
import { createHmac, timingSafeEqual } from 'node:crypto';

/** Standard registered claims we care about, plus arbitrary custom claims. */
export interface JwtPayload {
  sub?: string;
  exp?: number;
  iat?: number;
  [key: string]: unknown;
}

/** Thrown on any verification failure (bad signature, malformed, or expired). */
export class JwtError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'JwtError';
  }
}

function base64urlEncode(input: Buffer | string): string {
  return Buffer.from(input).toString('base64url');
}

function base64urlJson(obj: unknown): string {
  return base64urlEncode(JSON.stringify(obj));
}

function hmac(data: string, secret: string): Buffer {
  return createHmac('sha256', secret).update(data).digest();
}

/** Sign a payload with HS256. `expiresInSec` adds an `exp` claim relative to now. */
export function signJwt(
  payload: JwtPayload,
  secret: string,
  options: { expiresInSec?: number; now?: () => number } = {},
): string {
  const nowSec = Math.floor((options.now ? options.now() : Date.now()) / 1000);
  const fullPayload: JwtPayload = {
    iat: nowSec,
    ...(options.expiresInSec ? { exp: nowSec + options.expiresInSec } : {}),
    ...payload,
  };
  const header = base64urlJson({ alg: 'HS256', typ: 'JWT' });
  const body = base64urlJson(fullPayload);
  const signature = base64urlEncode(hmac(`${header}.${body}`, secret));
  return `${header}.${body}.${signature}`;
}

/** Verify an HS256 token and return its payload, or throw JwtError. */
export function verifyJwt(token: string, secret: string, now: () => number = Date.now): JwtPayload {
  const parts = token.split('.');
  if (parts.length !== 3) {
    throw new JwtError('malformed token');
  }
  const [headerB64, bodyB64, signatureB64] = parts as [string, string, string];

  let header: { alg?: string };
  try {
    header = JSON.parse(Buffer.from(headerB64, 'base64url').toString('utf8')) as { alg?: string };
  } catch {
    throw new JwtError('invalid header');
  }
  if (header.alg !== 'HS256') {
    throw new JwtError(`unsupported algorithm: ${String(header.alg)}`);
  }

  const expected = hmac(`${headerB64}.${bodyB64}`, secret);
  const provided = Buffer.from(signatureB64, 'base64url');
  if (expected.length !== provided.length || !timingSafeEqual(expected, provided)) {
    throw new JwtError('signature mismatch');
  }

  let payload: JwtPayload;
  try {
    payload = JSON.parse(Buffer.from(bodyB64, 'base64url').toString('utf8')) as JwtPayload;
  } catch {
    throw new JwtError('invalid payload');
  }
  if (typeof payload.exp === 'number' && Math.floor(now() / 1000) >= payload.exp) {
    throw new JwtError('token expired');
  }
  return payload;
}
