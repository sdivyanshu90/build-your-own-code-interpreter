/**
 * Job queue producer built on Redis Streams.
 *
 * Responsibilities:
 *  - Ensure the consumer group exists (XGROUP CREATE … MKSTREAM).
 *  - Enqueue jobs with serialised payloads (XADD).
 *  - Track in-flight enqueues so graceful shutdown can drain them.
 *  - Expose queue depth for metrics and backpressure.
 *
 * Dead-letter handling and stale-job reclaim are performed by the worker's consumer
 * (`worker/queue/consumer.py`), which is the side that knows when a job has exhausted retries.
 * The producer owns the DLQ *stream key* convention and group creation.
 */
import type Redis from 'ioredis';
import { ulid } from 'ulid';
import { getRedis, redisBreaker } from './redis.js';
import { logger } from '../telemetry/logger.js';
import { REDIS_KEYS } from '../types/index.js';
import type { ExecutionRequest, JobRecord, Language } from '../types/index.js';
import type { TraceContext } from '../telemetry/tracing.js';

/** The serialised shape of a stream entry's `payload` field. */
export interface JobPayload {
  job_id: string;
  user_id: string;
  language: Language;
  request: ExecutionRequest;
  traceparent: string;
  submitted_at: string;
}

let groupEnsured = false;

/**
 * Ensure the worker consumer group exists. Idempotent: the BUSYGROUP error (group already
 * exists) is swallowed. MKSTREAM creates the stream if it does not yet exist.
 */
export async function ensureConsumerGroup(redis: Redis = getRedis()): Promise<void> {
  if (groupEnsured) return;
  try {
    await redis.xgroup(
      'CREATE',
      REDIS_KEYS.JOB_STREAM,
      REDIS_KEYS.CONSUMER_GROUP,
      '0',
      'MKSTREAM',
    );
    logger.info({ stream: REDIS_KEYS.JOB_STREAM }, 'consumer group created');
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (!message.includes('BUSYGROUP')) {
      throw err;
    }
  }
  groupEnsured = true;
}

/** Set of job ids currently mid-enqueue, used to block shutdown until drained. */
const inFlight = new Set<string>();

/** Generate a sortable, URL-safe ULID job id. */
export function generateJobId(): string {
  return ulid();
}

/**
 * Enqueue a job and persist its initial PENDING record. Returns the job id.
 * The record and the stream entry are written together so a poller immediately sees PENDING.
 */
export async function enqueueJob(params: {
  jobId: string;
  userId: string;
  request: ExecutionRequest;
  trace: TraceContext;
  nowIso: string;
}): Promise<string> {
  const { jobId, userId, request, trace, nowIso } = params;
  const redis = getRedis();

  const payload: JobPayload = {
    job_id: jobId,
    user_id: userId,
    language: request.language,
    request,
    traceparent: trace.traceparent,
    submitted_at: nowIso,
  };

  const record: JobRecord = {
    job_id: jobId,
    user_id: userId,
    language: request.language,
    status: 'PENDING',
    request,
    created_at: nowIso,
    updated_at: nowIso,
    retries: 0,
  };

  inFlight.add(jobId);
  try {
    await redisBreaker.run(async () => {
      await ensureConsumerGroup(redis);
      const pipeline = redis.multi();
      pipeline.xadd(REDIS_KEYS.JOB_STREAM, '*', 'payload', JSON.stringify(payload));
      pipeline.set(
        REDIS_KEYS.jobRecord(jobId),
        JSON.stringify(record),
        'EX',
        // Records live a bit longer than results so polling still works after TTL of the result.
        60 * 60 * 2,
      );
      const results = await pipeline.exec();
      /* v8 ignore start -- defensive transaction-failure handling (Redis-side errors) */
      if (results === null) {
        throw new Error('redis transaction aborted');
      }
      for (const [error] of results) {
        if (error) throw error;
      }
      /* v8 ignore stop */
    });
    return jobId;
  } finally {
    inFlight.delete(jobId);
  }
}

/** Approximate number of unconsumed entries (pending in the group's lag). */
export async function getQueueDepth(): Promise<number> {
  const redis = getRedis();
  try {
    const len = await redis.xlen(REDIS_KEYS.JOB_STREAM);
    return typeof len === 'number' ? len : 0;
  } catch {
    return 0;
  }
}

/**
 * Wait for all in-flight enqueues to settle, up to `timeoutMs`. Used during graceful shutdown
 * so we never drop a job that was mid-write when SIGTERM arrived.
 */
export async function drainInFlight(timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (inFlight.size > 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
  }
  if (inFlight.size > 0) {
    logger.warn({ remaining: inFlight.size }, 'shutdown drained with in-flight enqueues remaining');
  }
}

/** Test/operational helper: reset the cached group-ensured flag. */
export function resetGroupEnsuredForTests(): void {
  groupEnsured = false;
  inFlight.clear();
}
