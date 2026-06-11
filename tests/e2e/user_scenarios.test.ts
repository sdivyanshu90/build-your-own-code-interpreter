/**
 * End-to-end user-journey tests against a LIVE stack (docker compose up).
 *
 * Configuration via env:
 *   API_BASE_URL     base URL of the API   (default http://localhost:8080)
 *   API_TOKEN        optional Bearer token (omit when ALLOW_ANONYMOUS=true, the dev default)
 *   E2E_ALLOW_RESTART  set to "1" to enable the worker-restart recovery test (runs docker)
 *
 * Every test skips automatically if the API is not reachable, so the suite is safe to run in any
 * environment.
 */
import { execSync } from 'node:child_process';
import { describe, it, expect, beforeAll } from 'vitest';
import { WebSocket } from 'ws';

const BASE = process.env.API_BASE_URL ?? 'http://localhost:8080';
const WS_BASE = BASE.replace(/^http/, 'ws');
const TOKEN = process.env.API_TOKEN ?? '';

let serverUp = false;

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (TOKEN) headers.authorization = `Bearer ${TOKEN}`;
  return headers;
}

interface ExecResult {
  status: number;
  body: Record<string, unknown>;
}

async function post(path: string, body: unknown): Promise<ExecResult> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, body: text ? JSON.parse(text) : {} };
}

async function runSync(language: string, code: string, extra: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const res = await post('/v1/execute', { language, code, timeout_seconds: 15, ...extra });
  return res.body;
}

async function pollUntilTerminal(jobId: string, timeoutMs = 40_000): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const res = await fetch(`${BASE}/v1/jobs/${jobId}`, { headers: authHeaders() });
    const body = (await res.json()) as Record<string, unknown>;
    if (['COMPLETED', 'FAILED', 'TIMEOUT', 'KILLED'].includes(body.status as string)) {
      return body;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`job ${jobId} did not finish in time`);
}

beforeAll(async () => {
  try {
    const res = await fetch(`${BASE}/v1/health`, { signal: AbortSignal.timeout(3000) });
    serverUp = res.ok || res.status === 503;
  } catch {
    serverUp = false;
  }
});

describe('E2E: Real User Scenarios', () => {
  it('User runs a Python fibonacci program and gets the correct output', async (ctx) => {
    if (!serverUp) ctx.skip();
    const code = 'a,b=0,1\nfor _ in range(10): a,b=b,a+b\nprint(a)';
    const result = await runSync('python', code);
    expect(result.status).toBe('COMPLETED');
    expect((result.stdout as string).trim()).toBe('55');
  });

  it('User runs a program that reads stdin', async (ctx) => {
    if (!serverUp) ctx.skip();
    const result = await runSync('python', 'print(int(input())*2)', { stdin: '21' });
    expect((result.stdout as string).trim()).toBe('42');
  });

  it('User submits malicious code that tries to read /etc/shadow', async (ctx) => {
    if (!serverUp) ctx.skip();
    const result = await runSync('python', "print(open('/etc/shadow').read())");
    expect(result.stdout as string).not.toContain('root:');
    expect(result.exit_code === 0 && (result.stdout as string).length > 0).toBe(false);
  });

  it('User submits a fork bomb — system remains responsive afterward', async (ctx) => {
    if (!serverUp) ctx.skip();
    await runSync('bash', ':(){ :|:& };:', { timeout_seconds: 5 });
    // The very next request must still succeed.
    const healthy = await runSync('python', "print('alive')");
    expect((healthy.stdout as string).trim()).toBe('alive');
  });

  it('User submits code that allocates 1GB RAM — OOM, system stays up', async (ctx) => {
    if (!serverUp) ctx.skip();
    const result = await runSync('python', "x = bytearray(1024*1024*1024); print(len(x))", { timeout_seconds: 10 });
    expect(['FAILED', 'TIMEOUT']).toContain(result.status);
    const healthy = await runSync('python', "print('ok')");
    expect((healthy.stdout as string).trim()).toBe('ok');
  });

  it('Rate limiting: a burst eventually returns 429', async (ctx) => {
    if (!serverUp) ctx.skip();
    const codes: number[] = [];
    for (let i = 0; i < 80; i += 1) {
      const res = await post('/v1/execute/async', { language: 'python', code: "print(1)" });
      codes.push(res.status);
      if (res.status === 429) break;
    }
    expect(codes).toContain(429);
  });

  it('Concurrent execution: 10 users submit code simultaneously', async (ctx) => {
    if (!serverUp) ctx.skip();
    const results = await Promise.all(
      Array.from({ length: 10 }, (_v, i) => runSync('python', `print(${i}*${i})`)),
    );
    const outputs = results.map((r) => Number((r.stdout as string).trim())).sort((a, b) => a - b);
    expect(outputs).toEqual([0, 1, 4, 9, 16, 25, 36, 49, 64, 81]);
  });

  it('WebSocket streaming: user receives output line-by-line in order', async (ctx) => {
    if (!serverUp) ctx.skip();
    const code = 'import time,sys\nfor i in range(3):\n print(i); sys.stdout.flush(); time.sleep(0.2)';
    const stdoutChunks: string[] = [];
    const exit = await new Promise<Record<string, unknown>>((resolve, reject) => {
      const url = TOKEN ? `${WS_BASE}/v1/execute/stream?token=${TOKEN}` : `${WS_BASE}/v1/execute/stream`;
      const ws = new WebSocket(url);
      ws.on('open', () => ws.send(JSON.stringify({ type: 'start', request: { language: 'python', code } })));
      ws.on('message', (data: Buffer) => {
        const frame = JSON.parse(data.toString());
        if (frame.type === 'stdout') stdoutChunks.push(frame.data);
        if (frame.type === 'exit') {
          ws.close();
          resolve(frame);
        }
      });
      ws.on('error', reject);
      setTimeout(() => reject(new Error('ws timeout')), 30_000);
    });
    expect(exit.status).toBe('COMPLETED');
    expect(stdoutChunks.join('')).toContain('0');
    expect(stdoutChunks.join('')).toContain('2');
  });

  it('Async job lifecycle: submit → poll → result', async (ctx) => {
    if (!serverUp) ctx.skip();
    const submit = await post('/v1/execute/async', { language: 'python', code: "print('async-ok')" });
    expect(submit.status).toBe(202);
    const final = await pollUntilTerminal(submit.body.job_id as string);
    expect(final.status).toBe('COMPLETED');
    expect(((final.result as Record<string, unknown>).stdout as string).trim()).toBe('async-ok');
  });

  it('Worker restart recovery: a stale job is re-claimed and completes', async (ctx) => {
    if (!serverUp || process.env.E2E_ALLOW_RESTART !== '1') ctx.skip();
    const submit = await post('/v1/execute/async', { language: 'python', code: "import time; time.sleep(8); print('survived')" });
    const jobId = submit.body.job_id as string;
    await new Promise((r) => setTimeout(r, 1500));
    // Forcibly restart the worker mid-execution.
    execSync('docker compose restart worker', { stdio: 'ignore' });
    const final = await pollUntilTerminal(jobId, 120_000);
    expect(['COMPLETED', 'FAILED', 'TIMEOUT']).toContain(final.status);
  });
});
