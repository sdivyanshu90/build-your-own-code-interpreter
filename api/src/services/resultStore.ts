/**
 * Result and job-record retrieval from the Redis hot tier, with a MinIO cold-tier fallback.
 *
 * The worker writes results to Redis (`sandbox:result:<id>`) and, for large outputs, to MinIO.
 * The API reads them here for polling (GET /v1/jobs/:id) and for the synchronous long-poll path.
 */
import { Client as MinioClient } from 'minio';
import { getRedis, redisBreaker } from './redis.js';
import { getConfig } from '../config.js';
import { logger } from '../telemetry/logger.js';
import { REDIS_KEYS, TERMINAL_STATUSES } from '../types/index.js';
import type { ExecutionResult, JobRecord } from '../types/index.js';

let minio: MinioClient | null = null;

/* v8 ignore next 13 -- real MinIO client construction; needs a live S3 endpoint */
/** Lazily construct the MinIO client. */
function getMinio(): MinioClient {
  if (minio === null) {
    const config = getConfig();
    minio = new MinioClient({
      endPoint: config.MINIO_ENDPOINT,
      port: config.MINIO_PORT,
      useSSL: config.MINIO_USE_SSL,
      accessKey: config.MINIO_ACCESS_KEY,
      secretKey: config.MINIO_SECRET_KEY,
    });
  }
  return minio;
}

/** Fetch the full job record (status + optional result) for a job id. */
export async function getJobRecord(jobId: string): Promise<JobRecord | null> {
  const redis = getRedis();
  const raw = await redisBreaker.run(() => redis.get(REDIS_KEYS.jobRecord(jobId)));
  if (!raw) return null;
  try {
    return JSON.parse(raw) as JobRecord;
  } catch (err) {
    logger.error({ jobId, err: (err as Error).message }, 'failed to parse job record');
    return null;
  }
}

/** Fetch just the execution result, if the job has produced one. */
export async function getResult(jobId: string): Promise<ExecutionResult | null> {
  const redis = getRedis();
  const raw = await redisBreaker.run(() => redis.get(REDIS_KEYS.result(jobId)));
  if (!raw) return null;
  try {
    return JSON.parse(raw) as ExecutionResult;
  } catch (err) {
    logger.error({ jobId, err: (err as Error).message }, 'failed to parse result');
    return null;
  }
}

/**
 * Long-poll for a job to reach a terminal state, returning the final record. Resolves as soon
 * as the job is terminal, or returns the last-seen (non-terminal) record when `timeoutMs`
 * elapses. Polls Redis at a fixed interval — cheap because reads are sub-millisecond.
 */
export async function waitForTerminal(
  jobId: string,
  timeoutMs: number,
  pollIntervalMs = 100,
): Promise<JobRecord | null> {
  const deadline = Date.now() + timeoutMs;
  let last: JobRecord | null = null;
  for (;;) {
    last = await getJobRecord(jobId);
    if (last && TERMINAL_STATUSES.includes(last.status)) {
      return last;
    }
    if (Date.now() >= deadline) {
      return last;
    }
    await new Promise((r) => setTimeout(r, pollIntervalMs));
  }
}

/* v8 ignore start -- MinIO IO seams; exercised against a live S3 endpoint in integration */
/** Generate a presigned download URL for a stored artifact (used in OutputFile.url). */
export async function presignArtifact(objectName: string, expirySeconds = 3600): Promise<string> {
  const config = getConfig();
  try {
    return await getMinio().presignedGetObject(config.MINIO_BUCKET, objectName, expirySeconds);
  } catch (err) {
    logger.warn({ objectName, err: (err as Error).message }, 'failed to presign artifact');
    return '';
  }
}

/** Health probe for MinIO connectivity. */
export async function minioHealthy(): Promise<boolean> {
  const config = getConfig();
  try {
    await getMinio().bucketExists(config.MINIO_BUCKET);
    return true;
  } catch {
    return false;
  }
}
/* v8 ignore stop */
