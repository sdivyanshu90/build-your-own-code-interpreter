/**
 * Integration tests for the API server.
 *
 * Exercises the real Express app (routing, auth, validation, rate limiting, the job lifecycle)
 * and the WebSocket streaming endpoint end-to-end against a real http.Server, using the built-in
 * fetch + the `ws` client (no CJS test client needed). Redis and MinIO are in-memory doubles, and
 * a small in-process "fake worker" consumes the job stream and produces results + live output, so
 * the full request→queue→result→response path is verified without external infrastructure.
 */
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { describe, it, expect, beforeEach, beforeAll, afterAll, vi } from 'vitest';
import { WebSocket } from 'ws';
import { ulid } from 'ulid';

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
      return 'http://minio.local/object';
    }
    async putObject(): Promise<void> {
      return undefined;
    }
  },
}));

import * as redisModule from '../../api/src/services/redis.js';
import { createApp } from '../../api/src/app.js';
import {
  createWebSocketServer,
  setupWebSocketServer,
  handleUpgrade,
} from '../../api/src/services/websocket.js';
import { REDIS_KEYS } from '../../api/src/types/index.js';
import { resetConfigForTests } from '../../api/src/config.js';
import { resetGroupEnsuredForTests } from '../../api/src/services/jobQueue.js';
import type { FakeRedis } from '../helpers/fakeRedis.js';
import { authToken } from '../setup.js';

const fake = (redisModule as unknown as { __fake: FakeRedis }).__fake;
const token = authToken();

let server: http.Server;
let baseUrl: string;
let wsUrl: string;

interface ApiResponse {
  status: number;
  body: Record<string, unknown>;
  headers: Headers;
}

async function api(
  method: string,
  path: string,
  opts: { auth?: boolean | string; body?: unknown } = {},
): Promise<ApiResponse> {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (opts.auth === true) headers.authorization = `Bearer ${token}`;
  else if (typeof opts.auth === 'string') headers.authorization = opts.auth;
  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const text = await res.text();
  const body = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  return { status: res.status, body, headers: res.headers };
}

/** A background loop that turns enqueued jobs into COMPLETED results + live stream output. */
function startFakeWorker(stdout = 'ok\n', opts: { omitResult?: boolean } = {}): () => void {
  let running = true;
  const seen = new Set<string>();
  void (async (): Promise<void> => {
    while (running) {
      for (const entry of fake._stream(REDIS_KEYS.JOB_STREAM)) {
        if (seen.has(entry.id)) continue;
        seen.add(entry.id);
        const payload = JSON.parse(entry.fields.payload);
        const jobId: string = payload.job_id;
        const channel = REDIS_KEYS.streamChannel(jobId);
        await fake.publish(channel, JSON.stringify({ kind: 'status', status: 'RUNNING' }));
        await fake.publish(channel, JSON.stringify({ kind: 'stdout', data: stdout }));
        const result = {
          job_id: jobId, status: 'COMPLETED', stdout, stderr: '', exit_code: 0,
          wall_time_ms: 5, cpu_time_ms: 1, memory_bytes: 1024, oom_killed: false,
          timed_out: false, truncated: false, files: [],
        };
        const recRaw = fake._rawGet(REDIS_KEYS.jobRecord(jobId));
        const rec = recRaw ? JSON.parse(recRaw) : { job_id: jobId, retries: 0 };
        // opts.omitResult simulates a terminal record without a stored result (exercises the
        // route's defensive fallbackResult path).
        const finalRec = opts.omitResult
          ? { ...rec, status: 'COMPLETED' }
          : { ...rec, status: 'COMPLETED', result };
        await fake.set(REDIS_KEYS.jobRecord(jobId), JSON.stringify(finalRec));
        await fake.set(REDIS_KEYS.result(jobId), JSON.stringify(result));
        await fake.publish(channel, JSON.stringify({
          kind: 'done', exit_code: 0, status: 'COMPLETED', wall_time_ms: 5,
          timed_out: false, oom_killed: false,
        }));
      }
      await new Promise((r) => setTimeout(r, 10));
    }
  })();
  return () => {
    running = false;
  };
}

function seedRecord(id: string, fields: Record<string, unknown>): Promise<unknown> {
  const base = {
    job_id: id, user_id: 'user-123', language: 'python', status: 'PENDING',
    request: { language: 'python', code: 'x' }, created_at: 't', updated_at: 't', retries: 0,
  };
  return fake.set(REDIS_KEYS.jobRecord(id), JSON.stringify({ ...base, ...fields }));
}

beforeAll(async () => {
  const app = createApp();
  server = http.createServer(app);
  const wss = createWebSocketServer();
  setupWebSocketServer(wss);
  server.on('upgrade', (req, socket, head) => handleUpgrade(wss, req, socket, head));
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  baseUrl = `http://127.0.0.1:${port}`;
  wsUrl = `ws://127.0.0.1:${port}/v1/execute/stream`;
});

afterAll(async () => {
  server.close();
  await redisModule.closeRedis();
});

beforeEach(async () => {
  await fake.flushdb();
  resetGroupEnsuredForTests();
  resetConfigForTests();
});

describe('POST /v1/execute', () => {
  it('executes Python synchronously and returns a result', async () => {
    const stop = startFakeWorker('hello\n');
    try {
      const res = await api('POST', '/v1/execute', {
        auth: true, body: { language: 'python', code: "print('hello')" },
      });
      expect(res.status).toBe(200);
      expect(res.body.status).toBe('COMPLETED');
      expect(res.body.stdout).toBe('hello\n');
      expect(res.body.exit_code).toBe(0);
    } finally {
      stop();
    }
  });

  it('returns 400 for an unknown language', async () => {
    const res = await api('POST', '/v1/execute', { auth: true, body: { language: 'cobol', code: 'x' } });
    expect(res.status).toBe(400);
    expect(res.headers.get('content-type')).toContain('application/problem+json');
  });

  it('returns 400 for oversized code', async () => {
    process.env.MAX_CODE_SIZE_BYTES = '16';
    resetConfigForTests();
    const res = await api('POST', '/v1/execute', { auth: true, body: { language: 'python', code: 'x'.repeat(100) } });
    expect(res.status).toBe(400);
    delete process.env.MAX_CODE_SIZE_BYTES;
  });

  it('returns 401 without auth', async () => {
    const res = await api('POST', '/v1/execute', { body: { language: 'python', code: 'x' } });
    expect(res.status).toBe(401);
  });

  it('returns 403 with an invalid token', async () => {
    const res = await api('POST', '/v1/execute', { auth: 'Bearer not.a.validtoken', body: { language: 'python', code: 'x' } });
    expect(res.status).toBe(403);
  });

  it('returns a fallback result when the terminal record has no stored result', async () => {
    const stop = startFakeWorker('ignored\n', { omitResult: true });
    try {
      const res = await api('POST', '/v1/execute', { auth: true, body: { language: 'python', code: 'x' } });
      expect(res.status).toBe(200);
      expect(res.body.status).toBe('COMPLETED');
      expect(res.body.stdout).toBe(''); // synthesised fallback
    } finally {
      stop();
    }
  });

  it('returns 408 when execution exceeds the synchronous window', async () => {
    // No fake worker is running, so the job never completes within the (shortened) sync window.
    process.env.SYNC_EXECUTION_TIMEOUT_SECONDS = '1';
    resetConfigForTests();
    const res = await api('POST', '/v1/execute', {
      auth: true, body: { language: 'python', code: 'x', timeout_seconds: 1 },
    });
    expect(res.status).toBe(408);
    expect(res.body.code).toBe('sync-timeout');
    delete process.env.SYNC_EXECUTION_TIMEOUT_SECONDS;
  });
});

describe('concurrency quota', () => {
  it('returns 429 once the per-user concurrent-job limit is exceeded', async () => {
    process.env.MAX_CONCURRENT_JOBS = '1';
    resetConfigForTests();
    const first = await api('POST', '/v1/execute/async', { auth: true, body: { language: 'python', code: 'x' } });
    const second = await api('POST', '/v1/execute/async', { auth: true, body: { language: 'python', code: 'x' } });
    expect(first.status).toBe(202);
    expect(second.status).toBe(429);
    expect(second.body.code).toBe('concurrency-limit');
    delete process.env.MAX_CONCURRENT_JOBS;
  });
});

describe('POST /v1/execute/async', () => {
  it('returns 202 Accepted with a ULID job_id', async () => {
    const res = await api('POST', '/v1/execute/async', { auth: true, body: { language: 'python', code: 'x' } });
    expect(res.status).toBe(202);
    expect(res.body.job_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
    expect(res.body.status).toBe('PENDING');
  });

  it('result becomes available via GET /v1/jobs/:id after completion', async () => {
    const stop = startFakeWorker('done\n');
    try {
      const submit = await api('POST', '/v1/execute/async', { auth: true, body: { language: 'python', code: 'x' } });
      const jobId = submit.body.job_id as string;
      let status = 'PENDING';
      for (let i = 0; i < 50 && status !== 'COMPLETED'; i += 1) {
        const poll = await api('GET', `/v1/jobs/${jobId}`, { auth: true });
        status = poll.body.status as string;
        if (status === 'COMPLETED') {
          expect((poll.body.result as Record<string, unknown>).stdout).toBe('done\n');
        }
        await new Promise((r) => setTimeout(r, 20));
      }
      expect(status).toBe('COMPLETED');
    } finally {
      stop();
    }
  });
});

describe('GET /v1/jobs/:id', () => {
  it('returns 404 for an unknown job', async () => {
    expect((await api('GET', `/v1/jobs/${ulid()}`, { auth: true })).status).toBe(404);
  });

  it('returns 400 for a malformed job id', async () => {
    expect((await api('GET', '/v1/jobs/not-a-ulid', { auth: true })).status).toBe(400);
  });

  it('returns the record for a known job', async () => {
    const id = ulid();
    await seedRecord(id, { status: 'COMPLETED' });
    const res = await api('GET', `/v1/jobs/${id}`, { auth: true });
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('COMPLETED');
  });

  it('returns 403 if the job belongs to another user', async () => {
    const id = ulid();
    await seedRecord(id, { user_id: 'someone-else' });
    expect((await api('GET', `/v1/jobs/${id}`, { auth: true })).status).toBe(403);
  });
});

describe('DELETE /v1/jobs/:id', () => {
  it('cancels a PENDING job', async () => {
    const id = ulid();
    await seedRecord(id, { status: 'PENDING' });
    const res = await api('DELETE', `/v1/jobs/${id}`, { auth: true });
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('KILLED');
  });

  it('kills a RUNNING job', async () => {
    const id = ulid();
    await seedRecord(id, { status: 'RUNNING' });
    expect((await api('DELETE', `/v1/jobs/${id}`, { auth: true })).status).toBe(200);
  });

  it('returns 409 if the job is already terminal', async () => {
    const id = ulid();
    await seedRecord(id, { status: 'COMPLETED' });
    expect((await api('DELETE', `/v1/jobs/${id}`, { auth: true })).status).toBe(409);
  });

  it('returns 404 if the job is not found', async () => {
    expect((await api('DELETE', `/v1/jobs/${ulid()}`, { auth: true })).status).toBe(404);
  });
});

describe('rate limiting', () => {
  it('returns 429 once the per-user limit is exceeded', async () => {
    process.env.RATE_LIMIT_REQUESTS_PER_MINUTE = '5';
    resetConfigForTests();
    const codes: number[] = [];
    for (let i = 0; i < 7; i += 1) {
      const res = await api('POST', '/v1/execute/async', { auth: true, body: { language: 'python', code: 'x' } });
      codes.push(res.status);
    }
    expect(codes.filter((c) => c === 202)).toHaveLength(5);
    expect(codes.filter((c) => c === 429).length).toBeGreaterThanOrEqual(1);
    delete process.env.RATE_LIMIT_REQUESTS_PER_MINUTE;
  });
});

describe('GET /v1/languages and /v1/health', () => {
  it('lists all supported runtimes', async () => {
    const res = await api('GET', '/v1/languages');
    expect(res.status).toBe(200);
    const languages = res.body.languages as Array<Record<string, unknown>>;
    expect(languages).toHaveLength(8);
    for (const lang of languages) {
      expect(lang).toHaveProperty('id');
      expect(lang).toHaveProperty('name');
      expect(lang).toHaveProperty('version');
    }
  });

  it('reports healthy when Redis is reachable', async () => {
    const res = await api('GET', '/v1/health');
    expect(res.status).toBe(200);
    expect(res.body.redis).toBe('up');
  });

  it('reports 503 when Redis is down', async () => {
    fake.failing = true;
    try {
      const res = await api('GET', '/v1/health');
      expect(res.status).toBe(503);
      expect(res.body.redis).toBe('down');
    } finally {
      fake.failing = false;
    }
  });

  it('exposes Prometheus metrics', async () => {
    const res = await fetch(`${baseUrl}/v1/metrics`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/plain');
    const text = await res.text();
    expect(text).toContain('sandbox_');
  });
});

describe('WS /v1/execute/stream', () => {
  it('streams stdout and an exit event', async () => {
    const stop = startFakeWorker('streamed!\n');
    try {
      const frames: Array<Record<string, unknown>> = await new Promise((resolve, reject) => {
        const collected: Array<Record<string, unknown>> = [];
        const ws = new WebSocket(`${wsUrl}?token=${token}`);
        ws.on('open', () => ws.send(JSON.stringify({
          type: 'start', request: { language: 'python', code: "print('x')" },
        })));
        ws.on('message', (data: Buffer) => {
          const frame = JSON.parse(data.toString());
          collected.push(frame);
          if (frame.type === 'exit') {
            ws.close();
            resolve(collected);
          }
        });
        ws.on('error', reject);
        setTimeout(() => reject(new Error('ws timeout')), 5000);
      });
      const types = frames.map((f) => f.type);
      expect(types).toContain('accepted');
      expect(types).toContain('stdout');
      expect(types).toContain('exit');
      expect(frames.find((f) => f.type === 'stdout')?.data).toBe('streamed!\n');
    } finally {
      stop();
    }
  });

  it('rejects an unauthenticated upgrade', async () => {
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(wsUrl); // no token
      ws.on('open', () => {
        ws.close();
        reject(new Error('should not have connected'));
      });
      ws.on('error', () => resolve());
      ws.on('unexpected-response', () => resolve());
    });
  });
});
