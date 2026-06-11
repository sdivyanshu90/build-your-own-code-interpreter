/**
 * Execution routes.
 *
 *   POST /v1/execute        — synchronous: enqueue and long-poll until terminal or the sync
 *                             timeout, then return the full ExecutionResult.
 *   POST /v1/execute/async  — fire-and-forget: enqueue and return 202 + job_id immediately.
 *
 * Both paths share validation (via middleware), authentication, rate limiting, and a per-user
 * concurrency quota. Job ids are sortable, URL-safe ULIDs.
 */
import { Router } from 'express';
import type { Request, Response } from 'express';
import { getConfig } from '../config.js';
import { getPrincipal } from '../middleware/auth.js';
import { getValidated, validateExecuteBody } from '../middleware/validator.js';
import { authMiddleware } from '../middleware/auth.js';
import { rateLimitMiddleware } from '../middleware/rateLimiter.js';
import { problem } from '../middleware/errorHandler.js';
import { enqueueJob, generateJobId } from '../services/jobQueue.js';
import { acquireSlot, concurrencyLimitForTier, releaseSlot } from '../services/quota.js';
import { waitForTerminal } from '../services/resultStore.js';
import { newTraceContext, recordSpan } from '../telemetry/tracing.js';
import { logger } from '../telemetry/logger.js';
import { executionsCompletedTotal, executionsSubmittedTotal } from '../telemetry/metrics.js';
import type { ExecutionResult, JobRecord } from '../types/index.js';

export const executeRouter = Router();

/**
 * Submit a job: validate concurrency, generate id, create trace context, enqueue.
 * Returns the job id and trace context, or sends a 429 and returns null.
 */
async function submit(
  req: Request,
  res: Response,
  mode: 'sync' | 'async',
): Promise<{ jobId: string } | null> {
  const principal = getPrincipal(req);
  const request = getValidated(req);
  const jobId = generateJobId();

  const granted = await acquireSlot(principal.user_id, jobId, principal.tier);
  if (!granted) {
    problem(
      res,
      429,
      'Concurrency Limit',
      `You may run at most ${concurrencyLimitForTier(principal.tier)} concurrent jobs.`,
      req.path,
      { code: 'concurrency-limit' },
    );
    return null;
  }

  const trace = newTraceContext(req.headers['traceparent'] as string | undefined);
  const nowIso = new Date().toISOString();
  await enqueueJob({ jobId, userId: principal.user_id, request, trace, nowIso });

  executionsSubmittedTotal.inc({ language: request.language, mode });
  recordSpan('execute.submit', trace, {
    job_id: jobId,
    language: request.language,
    user_id: principal.user_id,
    mode,
  });
  logger.info({ jobId, language: request.language, userId: principal.user_id, mode }, 'job submitted');
  return { jobId };
}

/**
 * POST /v1/execute
 *
 * @openapi
 * /v1/execute:
 *   post:
 *     summary: Execute code synchronously (long-poll up to the sync timeout).
 *     security: [{ bearerAuth: [] }, { apiKey: [] }]
 *     requestBody:
 *       required: true
 *       content:
 *         application/json:
 *           schema: { $ref: '#/components/schemas/ExecutionRequest' }
 *     responses:
 *       '200': { description: Execution result, content: { application/json: { schema: { $ref: '#/components/schemas/ExecutionResult' } } } }
 *       '400': { description: Validation error (RFC 7807) }
 *       '401': { description: Authentication required }
 *       '408': { description: Execution exceeded the synchronous window }
 *       '429': { description: Rate or concurrency limit exceeded }
 *       '503': { description: Backend (Redis/Docker) unavailable }
 */
executeRouter.post(
  '/execute',
  authMiddleware,
  rateLimitMiddleware,
  validateExecuteBody,
  (req: Request, res: Response) => {
    void (async (): Promise<void> => {
      const submitted = await submit(req, res, 'sync');
      if (!submitted) return;
      const { jobId } = submitted;
      const principal = getPrincipal(req);

      const config = getConfig();
      const request = getValidated(req);
      // The sync window is the smaller of the configured cap and the job's own timeout + slack.
      const windowMs =
        Math.min(config.SYNC_EXECUTION_TIMEOUT_SECONDS, request.timeout_seconds! + 5) * 1000;

      const record = await waitForTerminal(jobId, windowMs);
      await releaseSlot(principal.user_id, jobId);

      if (!record || record.status === 'PENDING' || record.status === 'RUNNING') {
        // The job did not finish within the synchronous window.
        problem(
          res,
          408,
          'Execution Timeout',
          'The execution did not complete within the synchronous window. Use /v1/execute/async ' +
            'and poll /v1/jobs/{id} for long-running jobs.',
          req.path,
          { code: 'sync-timeout', job_id: jobId },
        );
        executionsCompletedTotal.inc({ language: record?.language ?? 'python', status: 'TIMEOUT' });
        return;
      }

      const result = record.result ?? fallbackResult(record);
      executionsCompletedTotal.inc({ language: record.language, status: record.status });
      res.status(200).json(result);
    })();
  },
);

/**
 * POST /v1/execute/async
 *
 * @openapi
 * /v1/execute/async:
 *   post:
 *     summary: Submit code for asynchronous execution.
 *     responses:
 *       '202': { description: Accepted; returns a job_id and poll URL. }
 */
executeRouter.post(
  '/execute/async',
  authMiddleware,
  rateLimitMiddleware,
  validateExecuteBody,
  (req: Request, res: Response) => {
    void (async (): Promise<void> => {
      const submitted = await submit(req, res, 'async');
      if (!submitted) return;
      res.status(202).json({
        job_id: submitted.jobId,
        status: 'PENDING',
        poll_url: `/v1/jobs/${submitted.jobId}`,
      });
    })();
  },
);

/** Build a minimal result when a terminal record lacks a stored result (defensive). */
function fallbackResult(record: JobRecord): ExecutionResult {
  return {
    job_id: record.job_id,
    status: record.status,
    stdout: '',
    stderr: '',
    exit_code: null,
    wall_time_ms: 0,
    cpu_time_ms: 0,
    memory_bytes: 0,
    oom_killed: false,
    timed_out: record.status === 'TIMEOUT',
    truncated: false,
    files: [],
  };
}
