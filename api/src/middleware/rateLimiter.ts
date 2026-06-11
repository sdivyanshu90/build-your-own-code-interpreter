/**
 * Sliding-window rate limiting backed by Redis sorted sets.
 *
 * Unlike a fixed-window counter, this counts requests in the trailing `windowMs` at the instant
 * of each request, so a burst straddling a window boundary cannot get 2× the limit. The whole
 * check is a single atomic Lua script (no read-modify-write race under concurrency).
 *
 * Enforcement combines an IP-scoped limit and a user-scoped limit; a request must satisfy both.
 * Limits are tiered (anonymous / authenticated / premium). On any Redis failure the limiter
 * FAILS OPEN — it logs a warning and allows the request — trading strict enforcement for
 * availability, a deliberate and documented choice (see ARCHITECTURE.md §1.4).
 */
import type { Request, Response, NextFunction } from 'express';
import type { Redis } from 'ioredis';
import { ulid } from 'ulid';
import { getRedis } from '../services/redis.js';
import { getConfig } from '../config.js';
import { logger } from '../telemetry/logger.js';
import { rateLimitedTotal } from '../telemetry/metrics.js';
import { REDIS_KEYS } from '../types/index.js';
import type { Principal, UserTier } from '../types/index.js';
import { getPrincipal } from './auth.js';
import { problem } from './errorHandler.js';

/** The Lua script: atomically evict, count, and conditionally admit. */
const SLIDING_WINDOW_LUA = `
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window)
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = window
if oldest[2] then
  retry = (tonumber(oldest[2]) + window) - now
end
if retry < 1 then retry = 1 end
return {0, 0, retry}
`;

export interface RateLimitOutcome {
  allowed: boolean;
  remaining: number;
  /** Milliseconds until the next request would be admitted (only meaningful when blocked). */
  retryAfterMs: number;
}

/** Encapsulates the sliding-window algorithm against a Redis instance. */
export class SlidingWindowRateLimiter {
  constructor(
    private readonly now: () => number = () => Date.now(),
    private readonly failOpen = true,
    /** Optional injected Redis client (defaults to the shared command client). */
    private readonly redisClient?: Redis,
  ) {}

  /**
   * Check a single scope. Returns the outcome. On Redis error, fails open (allowed=true) when
   * configured to, otherwise rethrows.
   */
  async check(key: string, limit: number, windowMs: number): Promise<RateLimitOutcome> {
    if (limit <= 0) {
      return { allowed: false, remaining: 0, retryAfterMs: windowMs };
    }
    const redis = this.redisClient ?? getRedis();
    try {
      const result = (await redis.eval(
        SLIDING_WINDOW_LUA,
        1,
        key,
        String(this.now()),
        String(windowMs),
        String(limit),
        ulid(),
      )) as [number, number, number];
      return {
        allowed: result[0] === 1,
        remaining: result[1],
        retryAfterMs: result[2],
      };
    } catch (err) {
      if (this.failOpen) {
        logger.warn({ key, err: (err as Error).message }, 'rate limiter Redis error — failing open');
        return { allowed: true, remaining: limit, retryAfterMs: 0 };
      }
      throw err;
    }
  }
}

/** Resolve the per-minute request limit for a tier. */
export function limitForTier(tier: UserTier): number {
  const config = getConfig();
  switch (tier) {
    case 'premium':
      return config.RATE_LIMIT_PREMIUM_PER_MINUTE;
    case 'authenticated':
      return config.RATE_LIMIT_REQUESTS_PER_MINUTE;
    case 'anonymous':
    default:
      return config.RATE_LIMIT_ANON_PER_MINUTE;
  }
}

/** Extract a best-effort client IP, honouring a single trusted proxy hop. */
export function clientIp(req: Request): string {
  const forwarded = req.headers['x-forwarded-for'];
  if (typeof forwarded === 'string' && forwarded.length > 0) {
    const first = forwarded.split(',')[0];
    if (first) return first.trim();
  }
  return req.ip ?? req.socket.remoteAddress ?? 'unknown';
}

const WINDOW_MS = 60_000;
const limiter = new SlidingWindowRateLimiter();

/**
 * Express middleware enforcing the combined IP + user sliding-window limit. Sets standard
 * `RateLimit-*` headers and, on rejection, `Retry-After` and a 429 Problem Details body.
 */
export function rateLimitMiddleware(req: Request, res: Response, next: NextFunction): void {
  let principal: Principal;
  try {
    principal = getPrincipal(req);
  } catch {
    // No principal yet (e.g. anonymous route ordering) — limit by IP at the anonymous rate.
    principal = { user_id: clientIp(req), tier: 'anonymous', auth_method: 'anonymous' };
  }

  const userLimit = limitForTier(principal.tier);
  // The IP limit is generous (protects against a single host hammering many keys) but present.
  const ipLimit = Math.max(userLimit, getConfig().RATE_LIMIT_PREMIUM_PER_MINUTE);

  void Promise.all([
    limiter.check(REDIS_KEYS.rateLimit('user', principal.user_id), userLimit, WINDOW_MS),
    limiter.check(REDIS_KEYS.rateLimit('ip', clientIp(req)), ipLimit, WINDOW_MS),
  ])
    .then(([userOutcome, ipOutcome]) => {
      const blocked = !userOutcome.allowed || !ipOutcome.allowed;
      const remaining = Math.min(userOutcome.remaining, ipOutcome.remaining);
      res.setHeader('RateLimit-Limit', String(userLimit));
      res.setHeader('RateLimit-Remaining', String(Math.max(0, remaining)));

      if (blocked) {
        const retryAfterMs = Math.max(userOutcome.retryAfterMs, ipOutcome.retryAfterMs);
        const retryAfterSec = Math.ceil(retryAfterMs / 1000);
        res.setHeader('Retry-After', String(retryAfterSec));
        rateLimitedTotal.inc({ tier: principal.tier });
        problem(
          res,
          429,
          'Too Many Requests',
          `Rate limit exceeded. Retry after ${retryAfterSec}s.`,
          req.path,
          { code: 'rate-limited', retry_after_seconds: retryAfterSec },
        );
        return;
      }
      next();
    })
    .catch((err: unknown) => {
      // Should not happen (limiter fails open internally), but never hang the request.
      logger.error({ err: (err as Error).message }, 'rate limiter unexpected error');
      next();
    });
}
