/**
 * Job inspection and cancellation routes.
 *
 *   GET    /v1/jobs/:id  — poll status and (when terminal) the full result.
 *   DELETE /v1/jobs/:id  — cancel a PENDING job or kill a RUNNING one.
 *
 * Jobs are owner-scoped: a principal may only see/cancel jobs it submitted (404 vs 403 chosen to
 * avoid leaking the existence of other users' jobs — we return 403 only when ownership is known).
 */
import { Router } from 'express';
import type { Request, Response } from 'express';
import { authMiddleware, getPrincipal } from '../middleware/auth.js';
import { problem } from '../middleware/errorHandler.js';
import { getRedis } from '../services/redis.js';
import { getJobRecord } from '../services/resultStore.js';
import { releaseSlot } from '../services/quota.js';
import { logger } from '../telemetry/logger.js';
import { REDIS_KEYS, TERMINAL_STATUSES } from '../types/index.js';

export const jobsRouter = Router();

/** ULID format guard so a malformed id returns 400 rather than hitting Redis. */
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/i;

/**
 * GET /v1/jobs/:id
 *
 * @openapi
 * /v1/jobs/{id}:
 *   get:
 *     summary: Get the status and result of a job.
 *     parameters:
 *       - { name: id, in: path, required: true, schema: { type: string } }
 *     responses:
 *       '200': { description: The job record, content: { application/json: { schema: { $ref: '#/components/schemas/JobRecord' } } } }
 *       '403': { description: The job belongs to another user. }
 *       '404': { description: Unknown job id. }
 */
jobsRouter.get('/jobs/:id', authMiddleware, (req: Request, res: Response) => {
  void (async (): Promise<void> => {
    const jobId = req.params.id ?? '';
    if (!ULID_RE.test(jobId)) {
      problem(res, 400, 'Invalid job id', 'The job id is not a valid ULID.', req.path);
      return;
    }
    const record = await getJobRecord(jobId);
    if (!record) {
      problem(res, 404, 'Not Found', `No job with id ${jobId}.`, req.path);
      return;
    }
    const principal = getPrincipal(req);
    if (record.user_id !== principal.user_id) {
      problem(res, 403, 'Forbidden', 'This job belongs to another user.', req.path);
      return;
    }
    res.status(200).json(record);
  })();
});

/**
 * DELETE /v1/jobs/:id
 *
 * @openapi
 * /v1/jobs/{id}:
 *   delete:
 *     summary: Cancel a pending job or kill a running one.
 *     responses:
 *       '200': { description: Cancellation acknowledged. }
 *       '404': { description: Unknown job id. }
 *       '409': { description: The job is already in a terminal state. }
 */
jobsRouter.delete('/jobs/:id', authMiddleware, (req: Request, res: Response) => {
  void (async (): Promise<void> => {
    const jobId = req.params.id ?? '';
    if (!ULID_RE.test(jobId)) {
      problem(res, 400, 'Invalid job id', 'The job id is not a valid ULID.', req.path);
      return;
    }
    const record = await getJobRecord(jobId);
    if (!record) {
      problem(res, 404, 'Not Found', `No job with id ${jobId}.`, req.path);
      return;
    }
    const principal = getPrincipal(req);
    if (record.user_id !== principal.user_id) {
      problem(res, 403, 'Forbidden', 'This job belongs to another user.', req.path);
      return;
    }
    if (TERMINAL_STATUSES.includes(record.status)) {
      problem(res, 409, 'Conflict', `Job is already ${record.status}.`, req.path, {
        code: 'already-terminal',
      });
      return;
    }

    const redis = getRedis();
    // Signal cancellation: the worker polls this key and kills the container if RUNNING; a
    // PENDING job is skipped when dequeued. We also optimistically mark the record KILLED.
    const killed = {
      ...record,
      status: 'KILLED' as const,
      updated_at: new Date().toISOString(),
    };
    await redis
      .multi()
      .set(REDIS_KEYS.cancel(jobId), '1', 'EX', 120)
      .set(REDIS_KEYS.jobRecord(jobId), JSON.stringify(killed), 'KEEPTTL')
      .exec();

    await releaseSlot(record.user_id, jobId);
    logger.info({ jobId, userId: principal.user_id }, 'job cancellation requested');
    res.status(200).json({ job_id: jobId, status: 'KILLED' });
  })();
});
