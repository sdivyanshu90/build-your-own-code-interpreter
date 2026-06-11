/** Unit tests for RFC 7807 problem responses and the central error handler. */
import { describe, it, expect, vi } from 'vitest';
import type { Request, Response } from 'express';
import {
  ApiError,
  errorHandler,
  notFoundHandler,
  problem,
} from '../../api/src/middleware/errorHandler.js';

/** A minimal Express Response double that records what was written. */
function fakeRes(): Response & { _status: number; _json: Record<string, unknown>; _type: string } {
  const res = {
    headersSent: false,
    _status: 0,
    _json: {} as Record<string, unknown>,
    _type: '',
    status(code: number) {
      this._status = code;
      return this;
    },
    type(t: string) {
      this._type = t;
      return this;
    },
    json(body: Record<string, unknown>) {
      this._json = body;
      return this;
    },
  };
  return res as unknown as Response & { _status: number; _json: Record<string, unknown>; _type: string };
}

const fakeReq = (path = '/v1/x'): Request => ({ path, method: 'POST' }) as unknown as Request;

describe('problem()', () => {
  it('writes an RFC 7807 body with the right status and content type', () => {
    const res = fakeRes();
    problem(res, 400, 'Bad', 'detail here', '/v1/x', { code: 'validation-error' });
    expect(res._status).toBe(400);
    expect(res._type).toBe('application/problem+json');
    expect(res._json.title).toBe('Bad');
    expect(res._json.status).toBe(400);
    expect(res._json.type).toContain('validation-error');
    expect(res._json.instance).toBe('/v1/x');
  });
});

describe('notFoundHandler', () => {
  it('returns a 404 problem', () => {
    const res = fakeRes();
    notFoundHandler(fakeReq('/nope'), res);
    expect(res._status).toBe(404);
    expect(res._json.title).toBe('Not Found');
  });
});

describe('errorHandler', () => {
  it('maps an ApiError to its status and code', () => {
    const res = fakeRes();
    errorHandler(new ApiError(409, 'Conflict', 'already done', 'already-terminal'), fakeReq(), res, vi.fn());
    expect(res._status).toBe(409);
    expect(res._json.code).toBe('already-terminal');
  });

  it('maps an unknown error to a generic 500 without leaking details', () => {
    const res = fakeRes();
    errorHandler(new Error('secret stack trace'), fakeReq(), res, vi.fn());
    expect(res._status).toBe(500);
    expect(JSON.stringify(res._json)).not.toContain('secret stack trace');
  });

  it('does nothing when headers are already sent', () => {
    const res = fakeRes();
    res.headersSent = true;
    errorHandler(new Error('x'), fakeReq(), res, vi.fn());
    expect(res._status).toBe(0);
  });
});
