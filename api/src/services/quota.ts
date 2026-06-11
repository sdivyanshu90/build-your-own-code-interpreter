/**
 * Per-user concurrency quota.
 *
 * Implemented as a time-bounded sorted set of in-flight job ids per user. Each acquired slot
 * carries a timestamp and self-expires after the maximum possible job lifetime, so slots are
 * released even if a worker dies without signalling completion (no cross-service coordination
 * needed). The acquire is a single atomic Lua script.
 */
import { getRedis } from './redis.js';
import { getConfig } from '../config.js';
import { logger } from '../telemetry/logger.js';
import { REDIS_KEYS } from '../types/index.js';
import type { UserTier } from '../types/index.js';

const ACQUIRE_LUA = `
local key = KEYS[1]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - ttl)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, ttl)
  return 1
end
return 0
`;

/** Max concurrent jobs allowed for a tier. */
export function concurrencyLimitForTier(tier: UserTier): number {
  const config = getConfig();
  return tier === 'premium' ? config.MAX_CONCURRENT_JOBS_PREMIUM : config.MAX_CONCURRENT_JOBS;
}

/**
 * Attempt to reserve a concurrency slot for a user. Returns true if granted. Fails open (returns
 * true) on Redis error to preserve availability, consistent with the rate limiter.
 */
export async function acquireSlot(userId: string, jobId: string, tier: UserTier): Promise<boolean> {
  const config = getConfig();
  const limit = concurrencyLimitForTier(tier);
  const ttlMs = (config.MAX_TIMEOUT_SECONDS + 10) * 1000;
  try {
    const granted = (await getRedis().eval(
      ACQUIRE_LUA,
      1,
      REDIS_KEYS.concurrency(userId),
      String(Date.now()),
      String(ttlMs),
      String(limit),
      jobId,
    )) as number;
    return granted === 1;
  } catch (err) {
    logger.warn({ userId, err: (err as Error).message }, 'concurrency check failed — allowing');
    return true;
  }
}

/** Release a slot early (e.g. on synchronous completion or cancellation). Best-effort. */
export async function releaseSlot(userId: string, jobId: string): Promise<void> {
  try {
    await getRedis().zrem(REDIS_KEYS.concurrency(userId), jobId);
  } catch {
    // Slot will expire on its own; ignore.
  }
}
