/**
 * Unit tests for the Redis Streams job-queue producer. The Redis client is replaced with the
 * in-memory FakeRedis via vi.mock so we test serialization, stream/record writes, group creation,
 * ULID generation, and graceful drain without a server.
 */
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

import * as redisModule from '../../api/src/services/redis.js';
import {
  enqueueJob,
  ensureConsumerGroup,
  generateJobId,
  getQueueDepth,
  drainInFlight,
  resetGroupEnsuredForTests,
} from '../../api/src/services/jobQueue.js';
import { REDIS_KEYS } from '../../api/src/types/index.js';
import type { ExecutionRequest } from '../../api/src/types/index.js';
import type { FakeRedis } from '../helpers/fakeRedis.js';

const fake = (redisModule as unknown as { __fake: FakeRedis }).__fake;

const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

const trace = { trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), traceparent: '00-x-y-01' };

function req(overrides: Partial<ExecutionRequest> = {}): ExecutionRequest {
  return { language: 'python', code: "print('hi')", timeout_seconds: 10, ...overrides };
}

describe('JobQueue', () => {
  beforeEach(async () => {
    await fake.flushdb();
    resetGroupEnsuredForTests();
  });

  it('enqueues a job with the correct stream key and serialised payload', async () => {
    const jobId = generateJobId();
    await enqueueJob({ jobId, userId: 'u1', request: req(), trace, nowIso: '2026-01-01T00:00:00Z' });

    const entries = fake._stream(REDIS_KEYS.JOB_STREAM);
    expect(entries).toHaveLength(1);
    const payload = JSON.parse(entries[0].fields.payload);
    expect(payload.job_id).toBe(jobId);
    expect(payload.user_id).toBe('u1');
    expect(payload.language).toBe('python');
    expect(payload.request.code).toBe("print('hi')");
  });

  it('writes an initial PENDING job record', async () => {
    const jobId = generateJobId();
    await enqueueJob({ jobId, userId: 'u1', request: req(), trace, nowIso: 't' });
    const raw = fake._rawGet(REDIS_KEYS.jobRecord(jobId));
    expect(raw).toBeDefined();
    const record = JSON.parse(raw as string);
    expect(record.status).toBe('PENDING');
    expect(record.user_id).toBe('u1');
    expect(record.retries).toBe(0);
  });

  it('creates the consumer group, idempotently', async () => {
    await ensureConsumerGroup(fake as never);
    resetGroupEnsuredForTests();
    // Second create surfaces BUSYGROUP internally but must not throw.
    await expect(ensureConsumerGroup(fake as never)).resolves.toBeUndefined();
  });

  it('generates valid sortable ULID job ids', async () => {
    const a = generateJobId();
    const b = generateJobId();
    expect(a).toMatch(ULID_RE);
    expect(b).toMatch(ULID_RE);
    expect(a).not.toBe(b); // unique
    // ULIDs minted across different milliseconds sort by time.
    await new Promise((r) => setTimeout(r, 2));
    const c = generateJobId();
    expect(c > a).toBe(true);
  });

  it('serialisation round-trip preserves all request fields', async () => {
    const original = req({
      stdin: 'data',
      env_vars: { K: 'V' },
      files: [{ name: 'f.txt', content: 'c' }],
    });
    const jobId = generateJobId();
    await enqueueJob({ jobId, userId: 'u', request: original, trace, nowIso: 't' });
    const payload = JSON.parse(fake._stream(REDIS_KEYS.JOB_STREAM)[0].fields.payload);
    expect(payload.request).toEqual(original);
  });

  it('handles a large code payload', async () => {
    const big = req({ code: 'x = 1\n'.repeat(20_000) });
    const jobId = generateJobId();
    await enqueueJob({ jobId, userId: 'u', request: big, trace, nowIso: 't' });
    expect(fake._stream(REDIS_KEYS.JOB_STREAM)).toHaveLength(1);
  });

  it('reports queue depth', async () => {
    expect(await getQueueDepth()).toBe(0);
    await enqueueJob({ jobId: generateJobId(), userId: 'u', request: req(), trace, nowIso: 't' });
    expect(await getQueueDepth()).toBe(1);
  });

  it('queue depth returns 0 when Redis errors (never throws on the metrics path)', async () => {
    fake.failing = true;
    try {
      expect(await getQueueDepth()).toBe(0);
    } finally {
      fake.failing = false;
    }
  });

  it('exposes the dead-letter stream and consumer group contract', () => {
    expect(REDIS_KEYS.DEAD_LETTER_STREAM).toBe('sandbox:jobs:dead');
    expect(REDIS_KEYS.CONSUMER_GROUP).toBe('workers');
  });

  it('graceful shutdown drain resolves when no jobs are in flight', async () => {
    await expect(drainInFlight(100)).resolves.toBeUndefined();
  });
});
