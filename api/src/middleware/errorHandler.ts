/**
 * Centralised error handling and RFC 7807 Problem Details responses.
 *
 * Clients never receive raw exception messages or stack traces. Every error is mapped to a
 * structured `application/problem+json` body with a stable `type`, a human `title`, and a safe
 * `detail`. Unexpected errors are logged with full context but surfaced as a generic 500.
 */
import type { Request, Response, NextFunction } from 'express';
import { logger } from '../telemetry/logger.js';
import { CircuitOpenError } from '../services/redis.js';

/** The RFC 7807 Problem Details body shape. */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  /** Optional machine-readable error code for client branching. */
  code?: string;
  [key: string]: unknown;
}

const PROBLEM_BASE = 'https://docs.sandbox.local/errors';

/** Map an HTTP status to a stable problem `type` slug. */
function typeForStatus(status: number, code?: string): string {
  if (code) return `${PROBLEM_BASE}/${code}`;
  const slug =
    {
      400: 'validation-error',
      401: 'unauthenticated',
      403: 'forbidden',
      404: 'not-found',
      408: 'request-timeout',
      409: 'conflict',
      429: 'rate-limited',
      500: 'internal-error',
      503: 'service-unavailable',
    }[status] ?? 'error';
  return `${PROBLEM_BASE}/${slug}`;
}

/** Write a Problem Details response. Returns nothing; ends the response. */
export function problem(
  res: Response,
  status: number,
  title: string,
  detail: string,
  instance?: string,
  extra?: Record<string, unknown>,
): void {
  const body: ProblemDetails = {
    type: typeForStatus(status, typeof extra?.code === 'string' ? extra.code : undefined),
    title,
    status,
    detail,
    ...(instance ? { instance } : {}),
    ...(extra ?? {}),
  };
  res.status(status).type('application/problem+json').json(body);
}

/** A typed application error carrying an HTTP status and optional code. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly title: string,
    public readonly detail: string,
    public readonly code?: string,
    public readonly extra?: Record<string, unknown>,
  ) {
    super(detail);
    this.name = 'ApiError';
  }
}

/**
 * Wrap an async route handler so a rejected promise is forwarded to `next` (and therefore to the
 * terminal error handler) instead of becoming an unhandled rejection that leaves the request
 * hanging. Express 4 does not do this on its own.
 */
export function asyncHandler(
  fn: (req: Request, res: Response) => Promise<void>,
): (req: Request, res: Response, next: NextFunction) => void {
  return (req, res, next) => {
    fn(req, res).catch(next);
  };
}

/** True for errors that mean "a backing service (Redis) is unreachable or fast-failing". */
function isDependencyError(err: unknown): boolean {
  if (err instanceof CircuitOpenError) return true;
  if (!(err instanceof Error)) return false;
  const code = (err as Error & { code?: string }).code;
  return (
    err.name === 'MaxRetriesPerRequestError' ||
    err.message === 'Connection is closed.' ||
    code === 'ECONNREFUSED' ||
    code === 'ECONNRESET'
  );
}

/** Express 404 handler for unmatched routes. */
export function notFoundHandler(req: Request, res: Response): void {
  problem(res, 404, 'Not Found', `No route for ${req.method} ${req.path}.`, req.path);
}

/** Terminal Express error-handling middleware. Must have four args to be recognised. */
export function errorHandler(
  err: unknown,
  req: Request,
  res: Response,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _next: NextFunction,
): void {
  if (res.headersSent) {
    return;
  }
  if (err instanceof ApiError) {
    problem(res, err.status, err.title, err.detail, req.path, {
      ...(err.code ? { code: err.code } : {}),
      ...(err.extra ?? {}),
    });
    return;
  }

  if (isDependencyError(err)) {
    logger.error({ err: (err as Error).message, path: req.path }, 'dependency unavailable');
    res.setHeader('Retry-After', '5');
    problem(
      res,
      503,
      'Service Unavailable',
      'A backing service is temporarily unavailable. Retry shortly.',
      req.path,
      { code: 'service-unavailable' },
    );
    return;
  }

  // Client errors raised by body-parser (malformed JSON, payload too large, ...).
  const clientStatus = (err as { status?: unknown } | null)?.status;
  const bodyType = (err as { type?: unknown } | null)?.type;
  if (
    typeof clientStatus === 'number' &&
    clientStatus >= 400 &&
    clientStatus < 500 &&
    typeof bodyType === 'string' &&
    bodyType.startsWith('entity.')
  ) {
    const detail =
      bodyType === 'entity.too.large'
        ? 'The request body is too large.'
        : 'The request body could not be parsed.';
    problem(res, clientStatus, 'Bad Request', detail, req.path, { code: 'validation-error' });
    return;
  }

  const message = err instanceof Error ? err.message : String(err);
  logger.error({ err: message, path: req.path, method: req.method }, 'unhandled error');
  problem(
    res,
    500,
    'Internal Server Error',
    'An unexpected error occurred. The incident has been logged.',
    req.path,
  );
}
