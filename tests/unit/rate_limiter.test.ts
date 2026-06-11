/**
 * Unit tests for the sliding-window rate limiter. Uses the in-memory FakeRedis, which faithfully
 * re-implements the Lua admission script, so the algorithm itself (not just plumbing) is tested.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import type { Redis } from 'ioredis';
import { SlidingWindowRateLimiter, limitForTier } from '../../api/src/middleware/rateLimiter.js';
import { FakeRedis } from '../helpers/fakeRedis.js';

const WINDOW = 60_000;

/** A controllable clock for deterministic window tests. */
function clockFrom(start: number): { now: () => number; advance: (ms: number) => void } {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

describe('SlidingWindowRateLimiter', () => {
  let fake: FakeRedis;

  beforeEach(() => {
    fake = new FakeRedis();
  });

  function limiter(now: () => number, failOpen = true): SlidingWindowRateLimiter {
    return new SlidingWindowRateLimiter(now, failOpen, fake as unknown as Redis);
  }

  it('allows requests within the limit', async () => {
    const rl = limiter(() => 1000);
    for (let i = 0; i < 5; i += 1) {
      const out = await rl.check('k', 5, WINDOW);
      expect(out.allowed).toBe(true);
    }
  });

  it('blocks request that exceeds limit', async () => {
    const rl = limiter(() => 1000);
    for (let i = 0; i < 5; i += 1) await rl.check('k', 5, WINDOW);
    const blocked = await rl.check('k', 5, WINDOW);
    expect(blocked.allowed).toBe(false);
    expect(blocked.retryAfterMs).toBeGreaterThan(0);
  });

  it('resets correctly after window expires', async () => {
    const clock = clockFrom(1000);
    const rl = limiter(clock.now);
    for (let i = 0; i < 3; i += 1) await rl.check('k', 3, WINDOW);
    expect((await rl.check('k', 3, WINDOW)).allowed).toBe(false);
    clock.advance(WINDOW + 1);
    expect((await rl.check('k', 3, WINDOW)).allowed).toBe(true);
  });

  it('sliding window is not fixed window (old events expire individually)', async () => {
    const clock = clockFrom(0);
    const rl = limiter(clock.now);
    // 2 requests at t=0.
    await rl.check('k', 2, WINDOW);
    await rl.check('k', 2, WINDOW);
    expect((await rl.check('k', 2, WINDOW)).allowed).toBe(false);
    // Halfway through the window the old events still count (true sliding behaviour).
    clock.advance(WINDOW / 2);
    expect((await rl.check('k', 2, WINDOW)).allowed).toBe(false);
    // Once the original events age out, capacity frees up.
    clock.advance(WINDOW / 2 + 1);
    expect((await rl.check('k', 2, WINDOW)).allowed).toBe(true);
  });

  it('different users have independent limits', async () => {
    const rl = limiter(() => 1000);
    await rl.check('user:a', 1, WINDOW);
    expect((await rl.check('user:a', 1, WINDOW)).allowed).toBe(false);
    expect((await rl.check('user:b', 1, WINDOW)).allowed).toBe(true);
  });

  it('returns correct Retry-After value when blocked', async () => {
    const clock = clockFrom(10_000);
    const rl = limiter(clock.now);
    await rl.check('k', 1, WINDOW);
    clock.advance(20_000);
    const blocked = await rl.check('k', 1, WINDOW);
    expect(blocked.allowed).toBe(false);
    // The first event was at t=10000; it expires at 70000; now is 30000 → ~40000ms remaining.
    expect(blocked.retryAfterMs).toBe(WINDOW - 20_000);
  });

  it('authenticated users get higher limits than anonymous', () => {
    expect(limitForTier('authenticated')).toBeGreaterThan(limitForTier('anonymous'));
    expect(limitForTier('premium')).toBeGreaterThan(limitForTier('authenticated'));
  });

  it('fails open on Redis timeout (allows request)', async () => {
    fake.failing = true;
    const rl = limiter(() => 1000, true);
    const out = await rl.check('k', 1, WINDOW);
    expect(out.allowed).toBe(true);
  });

  it('recovers after Redis reconnects', async () => {
    fake.failing = true;
    const rl = limiter(() => 1000);
    expect((await rl.check('k', 1, WINDOW)).allowed).toBe(true); // failed open
    fake.failing = false;
    await rl.check('k', 1, WINDOW);
    expect((await rl.check('k', 1, WINDOW)).allowed).toBe(false); // now enforcing again
  });

  it('handles concurrent requests atomically (no over-admission)', async () => {
    const rl = limiter(() => 1000);
    const outcomes = await Promise.all(
      Array.from({ length: 20 }, () => rl.check('k', 5, WINDOW)),
    );
    const allowed = outcomes.filter((o) => o.allowed).length;
    expect(allowed).toBe(5);
  });

  it('zero requests allowed returns blocked immediately', async () => {
    const rl = limiter(() => 1000);
    const out = await rl.check('k', 0, WINDOW);
    expect(out.allowed).toBe(false);
  });

  it('very large window size does not overflow', async () => {
    const rl = limiter(() => 1000);
    const out = await rl.check('k', 5, 7 * 24 * 60 * 60 * 1000);
    expect(out.allowed).toBe(true);
  });
});
