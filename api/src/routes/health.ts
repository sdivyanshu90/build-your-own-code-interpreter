/**
 * Health and metrics routes.
 *
 *   GET /v1/health  — liveness/readiness; reports the status of hard dependencies.
 *   GET /v1/metrics — Prometheus exposition.
 */
import { Router } from 'express';
import type { Request, Response } from 'express';
import { getRedis } from '../services/redis.js';
import { minioHealthy } from '../services/resultStore.js';
import { getQueueDepth } from '../services/jobQueue.js';
import { queueDepth, renderMetrics } from '../telemetry/metrics.js';

export const healthRouter = Router();

/** Ping Redis with a short timeout; returns 'up' or 'down'. */
async function redisStatus(): Promise<'up' | 'down'> {
  try {
    const pong = await getRedis().ping();
    return pong === 'PONG' ? 'up' : 'down';
  } catch {
    return 'down';
  }
}

/**
 * @openapi
 * /v1/health:
 *   get:
 *     summary: Liveness/readiness probe.
 *     responses:
 *       '200': { description: All hard dependencies are reachable. }
 *       '503': { description: A hard dependency is unavailable. }
 */
healthRouter.get('/health', (_req: Request, res: Response) => {
  void (async (): Promise<void> => {
    const [redis, minio] = await Promise.all([redisStatus(), minioHealthy()]);
    const ok = redis === 'up';
    res.status(ok ? 200 : 503).json({
      status: ok ? 'ok' : 'degraded',
      redis,
      minio: minio ? 'up' : 'down',
      uptime_seconds: Math.floor(process.uptime()),
      version: process.env.npm_package_version ?? '1.0.0',
    });
  })();
});

/**
 * @openapi
 * /v1/metrics:
 *   get:
 *     summary: Prometheus metrics.
 *     responses:
 *       '200': { description: Metrics in the Prometheus text exposition format. }
 */
healthRouter.get('/metrics', (_req: Request, res: Response) => {
  void (async (): Promise<void> => {
    // Refresh the queue-depth gauge on scrape.
    try {
      queueDepth.set(await getQueueDepth());
    } catch {
      /* best-effort */
    }
    res.setHeader('Content-Type', 'text/plain; version=0.0.4; charset=utf-8');
    res.status(200).send(await renderMetrics());
  })();
});
