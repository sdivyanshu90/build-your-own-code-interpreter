/**
 * Authentication: JWT bearer tokens and API keys.
 *
 * The core `authenticatePrincipal` function is transport-agnostic (it takes a header bag) so it
 * can be reused by both the Express middleware and the WebSocket upgrade handler. It returns a
 * `Principal` or throws an `AuthError`. Anonymous access is permitted only when explicitly
 * enabled in config.
 */
import type { Request, Response, NextFunction } from 'express';
import { getConfig } from '../config.js';
import { verifyJwt, JwtError } from '../services/jwt.js';
import type { Principal, UserTier } from '../types/index.js';
import { problem } from './errorHandler.js';

/** Raised on any authentication failure. Carries the HTTP status to return. */
export class AuthError extends Error {
  constructor(
    public readonly status: 401 | 403,
    public readonly title: string,
    public readonly detail: string,
  ) {
    super(detail);
    this.name = 'AuthError';
  }
}

/** A header accessor that works for both Express requests and raw header objects. */
export type HeaderBag = {
  get(name: string): string | undefined;
};

/** Wrap a Node IncomingHttpHeaders-like object into a HeaderBag (case-insensitive). */
export function headerBag(headers: Record<string, string | string[] | undefined>): HeaderBag {
  return {
    get(name: string): string | undefined {
      const value = headers[name.toLowerCase()];
      return Array.isArray(value) ? value[0] : value;
    },
  };
}

interface JwtClaims {
  sub?: string;
  tier?: UserTier;
}

/**
 * Resolve the authenticated principal from request headers (and an optional query token used by
 * browser WebSocket clients that cannot set headers). Throws AuthError on invalid credentials.
 */
export function authenticatePrincipal(headers: HeaderBag, queryToken?: string): Principal {
  const config = getConfig();

  // 1) API key (machine clients).
  const apiKey = headers.get('x-api-key');
  if (apiKey) {
    const tier = config.API_KEY_MAP.get(apiKey);
    if (!tier) {
      throw new AuthError(403, 'Invalid API key', 'The provided X-API-Key is not recognised.');
    }
    return { user_id: `key:${hashKey(apiKey)}`, tier, auth_method: 'api_key' };
  }

  // 2) JWT bearer token (interactive users).
  const authHeader = headers.get('authorization');
  const bearer = authHeader?.startsWith('Bearer ') ? authHeader.slice(7) : queryToken;
  if (bearer) {
    let claims: JwtClaims;
    try {
      claims = verifyJwt(bearer, config.JWT_SECRET) as JwtClaims;
    } catch (err) {
      const reason = err instanceof JwtError ? err.message : 'verification failed';
      throw new AuthError(403, 'Invalid token', `JWT verification failed: ${reason}`);
    }
    if (!claims.sub) {
      throw new AuthError(403, 'Invalid token', 'JWT is missing the required `sub` claim.');
    }
    const tier: UserTier = claims.tier === 'premium' ? 'premium' : 'authenticated';
    return { user_id: claims.sub, tier, auth_method: 'jwt' };
  }

  // 3) Anonymous (only if enabled).
  if (config.ALLOW_ANONYMOUS) {
    return { user_id: 'anonymous', tier: 'anonymous', auth_method: 'anonymous' };
  }

  throw new AuthError(
    401,
    'Authentication required',
    'Provide a Bearer JWT in Authorization or a key in X-API-Key.',
  );
}

/** Non-cryptographic short fingerprint of an API key for use as a stable user id. */
function hashKey(key: string): string {
  let h = 2166136261;
  for (let i = 0; i < key.length; i += 1) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16);
}

/** Express middleware that attaches `req.principal` or returns an RFC 7807 error. */
export function authMiddleware(req: Request, res: Response, next: NextFunction): void {
  try {
    const bag = headerBag(req.headers);
    const principal = authenticatePrincipal(bag);
    (req as Request & { principal: Principal }).principal = principal;
    next();
  } catch (err) {
    if (err instanceof AuthError) {
      problem(res, err.status, err.title, err.detail, req.path);
      return;
    }
    next(err);
  }
}

/** Helper to read the principal attached by `authMiddleware`. */
export function getPrincipal(req: Request): Principal {
  const principal = (req as Request & { principal?: Principal }).principal;
  if (!principal) {
    throw new AuthError(401, 'Authentication required', 'No principal on request.');
  }
  return principal;
}
