/**
 * Prometheus metrics for the API server.
 *
 * Cardinality is deliberately bounded: labels are limited to low-cardinality dimensions
 * (language, status, tier, method, route, code) — never per-job or per-user values.
 */
import client from 'prom-client';

export const registry = new client.Registry();

client.collectDefaultMetrics({ register: registry, prefix: 'sandbox_api_' });

export const httpRequestsTotal = new client.Counter({
  name: 'sandbox_api_http_requests_total',
  help: 'Total HTTP requests handled by the API.',
  labelNames: ['method', 'route', 'code'] as const,
  registers: [registry],
});

export const httpRequestDuration = new client.Histogram({
  name: 'sandbox_api_http_request_duration_seconds',
  help: 'HTTP request duration in seconds.',
  labelNames: ['method', 'route', 'code'] as const,
  buckets: [0.005, 0.01, 0.05, 0.1, 0.5, 1, 3, 5, 10, 30],
  registers: [registry],
});

export const executionsSubmittedTotal = new client.Counter({
  name: 'sandbox_executions_submitted_total',
  help: 'Total execution requests submitted to the queue.',
  labelNames: ['language', 'mode'] as const,
  registers: [registry],
});

export const executionsCompletedTotal = new client.Counter({
  name: 'sandbox_executions_total',
  help: 'Total executions observed reaching a terminal state (from the API perspective).',
  labelNames: ['language', 'status'] as const,
  registers: [registry],
});

export const rateLimitedTotal = new client.Counter({
  name: 'sandbox_api_rate_limited_total',
  help: 'Requests rejected by the rate limiter.',
  labelNames: ['tier'] as const,
  registers: [registry],
});

export const queueDepth = new client.Gauge({
  name: 'sandbox_queue_depth',
  help: 'Approximate number of pending jobs in the queue stream.',
  registers: [registry],
});

export const activeWebsockets = new client.Gauge({
  name: 'sandbox_api_active_websockets',
  help: 'Currently open WebSocket streaming connections.',
  registers: [registry],
});

/** Render the metrics in the Prometheus text exposition format. */
export async function renderMetrics(): Promise<string> {
  return registry.metrics();
}
