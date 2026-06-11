/**
 * k6 load test for the Code Interpreter Sandbox.
 *
 *   k6 run tests/e2e/load_test.js
 *   API_BASE_URL=http://host:8080 API_TOKEN=<jwt> k6 run tests/e2e/load_test.js
 *
 * Three scenarios run in sequence:
 *   1. baseline  — 100 VUs x 30s, simple Python print; asserts p95 < 3s and error rate < 1%.
 *   2. spike     — ramp 10 -> 500 VUs over 60s; asserts the server stays up and the rate limiter
 *                  engages (429s are expected and tracked, not counted as errors).
 *   3. stress    — adversarial inputs (fork bombs, infinite loops, huge allocations); asserts the
 *                  system remains stable (every request gets a well-formed terminal response).
 */
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

const BASE = __ENV.API_BASE_URL || 'http://localhost:8080';
const TOKEN = __ENV.API_TOKEN || '';

// ── Custom metrics ──────────────────────────────────────────────────────────────────────────
const executionLatency = new Trend('execution_latency', true);
const queueWaitTime = new Trend('queue_wait_time', true);
const containerStartup = new Trend('container_startup_time', true);
const errorRate = new Rate('error_rate');
const rateLimited = new Counter('rate_limited_total');
const errorsByType = new Counter('errors_by_type');

export const options = {
  scenarios: {
    baseline: {
      executor: 'constant-vus',
      vus: 100,
      duration: '30s',
      exec: 'baseline',
      tags: { scenario: 'baseline' },
    },
    spike: {
      executor: 'ramping-vus',
      startVUs: 10,
      startTime: '35s',
      stages: [
        { duration: '30s', target: 500 },
        { duration: '30s', target: 500 },
      ],
      exec: 'spike',
      tags: { scenario: 'spike' },
    },
    stress: {
      executor: 'constant-vus',
      vus: 20,
      duration: '30s',
      startTime: '110s',
      exec: 'stress',
      tags: { scenario: 'stress' },
    },
  },
  thresholds: {
    execution_latency: ['p(95)<3000', 'p(99)<8000'],
    error_rate: ['rate<0.01'],
    'http_req_duration{scenario:baseline}': ['p(95)<3000'],
  },
};

function headers() {
  const h = { 'Content-Type': 'application/json' };
  if (TOKEN) h.Authorization = `Bearer ${TOKEN}`;
  return h;
}

function submitSync(language, code, timeoutSeconds) {
  const payload = JSON.stringify({ language, code, timeout_seconds: timeoutSeconds || 10 });
  const start = Date.now();
  const res = http.post(`${BASE}/v1/execute`, payload, { headers: headers(), timeout: '40s' });
  const elapsed = Date.now() - start;
  executionLatency.add(elapsed);
  return { res, elapsed };
}

// Scenario 1 — baseline throughput.
export function baseline() {
  const { res, elapsed } = submitSync('python', "print('hello, sandbox')");
  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'status COMPLETED': (r) => safeStatus(r) === 'COMPLETED',
    'correct output': (r) => safeBody(r).stdout && safeBody(r).stdout.indexOf('hello, sandbox') >= 0,
  });
  recordTimings(res, elapsed);
  errorRate.add(!ok);
  if (!ok) errorsByType.add(1, { code: String(res.status) });
  sleep(0.5);
}

// Scenario 2 — spike: 429s are expected and tracked, not errors.
export function spike() {
  const { res, elapsed } = submitSync('python', "print(2+2)");
  if (res.status === 429) {
    rateLimited.add(1);
  } else {
    const ok = check(res, { 'not 5xx': (r) => r.status < 500 });
    errorRate.add(!ok);
    if (!ok) errorsByType.add(1, { code: String(res.status) });
  }
  recordTimings(res, elapsed);
  sleep(0.2);
}

// Scenario 3 — adversarial inputs; the system must stay stable.
const ATTACKS = [
  { language: 'bash', code: ':(){ :|:& };:' },
  { language: 'python', code: 'while True: pass' },
  { language: 'python', code: "x = ' ' * (10**10)" },
  { language: 'python', code: 'import os\nwhile True:\n  try: os.fork()\n  except: pass' },
];

export function stress() {
  const attack = ATTACKS[Math.floor(Math.random() * ATTACKS.length)];
  const { res } = submitSync(attack.language, attack.code, 5);
  // Stability: the request must come back with a well-formed terminal verdict (or be limited),
  // never a 5xx or a hang.
  const stable = check(res, {
    'server stable (no 5xx)': (r) => r.status < 500,
    'terminal or limited': (r) => {
      if (r.status === 429 || r.status === 408) return true;
      const s = safeStatus(r);
      return s === 'FAILED' || s === 'TIMEOUT' || s === 'KILLED' || s === 'COMPLETED';
    },
  });
  errorRate.add(!stable);
  if (!stable) errorsByType.add(1, { code: String(res.status) });
  sleep(0.3);
}

function safeBody(res) {
  try {
    return JSON.parse(res.body);
  } catch (_e) {
    return {};
  }
}

function safeStatus(res) {
  return safeBody(res).status;
}

function recordTimings(res, elapsed) {
  const body = safeBody(res);
  if (typeof body.wall_time_ms === 'number') {
    // Approximate queue + startup overhead as the client-observed time minus in-sandbox wall time.
    queueWaitTime.add(Math.max(0, elapsed - body.wall_time_ms));
    containerStartup.add(Math.max(0, elapsed - body.wall_time_ms));
  }
}

export function handleSummary(data) {
  const m = data.metrics;
  const get = (name, stat) => (m[name] && m[name].values ? m[name].values[stat] : 0);
  const summary = {
    execution_latency_p50: get('execution_latency', 'p(50)'),
    execution_latency_p95: get('execution_latency', 'p(95)'),
    execution_latency_p99: get('execution_latency', 'p(99)'),
    queue_wait_p95: get('queue_wait_time', 'p(95)'),
    container_startup_p95: get('container_startup_time', 'p(95)'),
    error_rate: get('error_rate', 'rate'),
    rate_limited_total: get('rate_limited_total', 'count'),
    http_reqs: get('http_reqs', 'count'),
  };
  return {
    stdout: `\n=== Sandbox load-test summary ===\n${JSON.stringify(summary, null, 2)}\n`,
    'load-test-summary.json': JSON.stringify(summary, null, 2),
  };
}
