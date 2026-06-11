/** Unit tests for the result store and concurrency-quota services (Redis + MinIO mocked). */
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../../api/src/services/redis.js', async () => {
  const { FakeRedis } = await import('../helpers/fakeRedis.js');
  const fake = new FakeRedis();
  return {
    __fake: fake,
    getRedis: () => fake,
    getSubscriber: () => fake,
    createRedisConnection: () => fake,
    closeRedis: async () => undefined,
    redisBreaker: { run: (fn: () => unknown) => fn() },
    CircuitBreaker: class {},
    CircuitOpenError: class extends Error {},
  };
});

vi.mock('minio', () => ({
  Client: class {
    async bucketExists(): Promise<boolean> {
      return true;
    }
    async presignedGetObject(): Promise<string> {
      return 'http://minio.local/presigned';
    }
  },
}));

import * as redisModule from '../../api/src/services/redis.js';
import { getResult, waitForTerminal } from '../../api/src/services/resultStore.js';
import { acquireSlot, releaseSlot, concurrencyLimitForTier } from '../../api/src/services/quota.js';
import { REDIS_KEYS } from '../../api/src/types/index.js';
import { resetConfigForTests } from '../../api/src/config.js';
import type { FakeRedis } from '../helpers/fakeRedis.js';

const fake = (redisModule as unknown as { __fake: FakeRedis }).__fake;

beforeEach(async () => {
  await fake.flushdb();
  resetConfigForTests();
});

describe('resultStore', () => {
  it('getResult returns a parsed result', async () => {
    await fake.set(REDIS_KEYS.result('j1'), JSON.stringify({ job_id: 'j1', status: 'COMPLETED', stdout: 'x' }));
    const r = await getResult('j1');
    expect(r?.status).toBe('COMPLETED');
  });

  it('getResult returns null when absent', async () => {
    expect(await getResult('missing')).toBeNull();
  });

  it('getResult returns null on malformed JSON', async () => {
    await fake.set(REDIS_KEYS.result('bad'), 'not-json');
    expect(await getResult('bad')).toBeNull();
  });

  it('waitForTerminal resolves immediately for a terminal record', async () => {
    await fake.set(REDIS_KEYS.jobRecord('t1'), JSON.stringify({ job_id: 't1', status: 'COMPLETED' }));
    const rec = await waitForTerminal('t1', 1000, 10);
    expect(rec?.status).toBe('COMPLETED');
  });

  it('waitForTerminal returns the last record after the timeout', async () => {
    await fake.set(REDIS_KEYS.jobRecord('t2'), JSON.stringify({ job_id: 't2', status: 'RUNNING' }));
    const rec = await waitForTerminal('t2', 60, 10);
    expect(rec?.status).toBe('RUNNING'); // still non-terminal when the window elapsed
  });
});

describe('quota', () => {
  it('grants slots up to the limit then refuses', async () => {
    process.env.MAX_CONCURRENT_JOBS = '3';
    resetConfigForTests();
    const grants: boolean[] = [];
    for (let i = 0; i < 5; i += 1) {
      grants.push(await acquireSlot('user-q', `job-${i}`, 'authenticated'));
    }
    expect(grants.filter(Boolean)).toHaveLength(3);
    delete process.env.MAX_CONCURRENT_JOBS;
  });

  it('releaseSlot frees capacity', async () => {
    process.env.MAX_CONCURRENT_JOBS = '1';
    resetConfigForTests();
    expect(await acquireSlot('u', 'j1', 'authenticated')).toBe(true);
    expect(await acquireSlot('u', 'j2', 'authenticated')).toBe(false);
    await releaseSlot('u', 'j1');
    expect(await acquireSlot('u', 'j3', 'authenticated')).toBe(true);
    delete process.env.MAX_CONCURRENT_JOBS;
  });

  it('premium tier gets a higher concurrency limit', () => {
    expect(concurrencyLimitForTier('premium')).toBeGreaterThan(concurrencyLimitForTier('authenticated'));
  });
});
