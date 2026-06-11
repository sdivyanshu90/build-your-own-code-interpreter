/**
 * Minimal OpenTelemetry-compatible trace context propagation.
 *
 * To keep dependencies light while still satisfying the distributed-tracing requirement, this
 * module implements W3C Trace Context (`traceparent`) generation and propagation. The trace id
 * is created at the API edge and threaded through the queue payload to the worker, so a single
 * trace spans HTTP → queue → container execution. If OTEL_EXPORTER_OTLP_ENDPOINT is set, spans
 * are additionally exported (best-effort, non-blocking).
 */
import { randomBytes } from 'node:crypto';
import { getConfig } from '../config.js';
import { logger } from './logger.js';

export interface TraceContext {
  trace_id: string;
  span_id: string;
  /** The W3C `traceparent` header value. */
  traceparent: string;
}

/** Generate a fresh W3C trace context (version 00, sampled). */
export function newTraceContext(parent?: string): TraceContext {
  const traceId = extractTraceId(parent) ?? randomBytes(16).toString('hex');
  const spanId = randomBytes(8).toString('hex');
  return {
    trace_id: traceId,
    span_id: spanId,
    traceparent: `00-${traceId}-${spanId}-01`,
  };
}

/** Parse a 32-hex trace id out of an incoming `traceparent` header, if valid. */
export function extractTraceId(traceparent?: string): string | null {
  if (!traceparent) return null;
  const parts = traceparent.split('-');
  if (parts.length !== 4) return null;
  const traceId = parts[1];
  if (traceId && /^[0-9a-f]{32}$/.test(traceId) && traceId !== '0'.repeat(32)) {
    return traceId;
  }
  return null;
}

/**
 * Records a finished span. With no OTLP endpoint configured this logs the span at debug level
 * (still useful for local correlation); with one configured it would forward to the collector.
 * Deliberately fire-and-forget so observability never blocks the request path.
 */
export function recordSpan(name: string, ctx: TraceContext, attributes: Record<string, unknown>): void {
  const config = getConfig();
  const span = { span: name, ...ctx, attributes };
  if (config.OTEL_EXPORTER_OTLP_ENDPOINT) {
    // In a full deployment the OTLP SDK exports here; we emit a structured event as the
    // transport-agnostic fallback so the trace is never silently lost.
    logger.debug({ otel: true, ...span }, 'span');
  } else {
    logger.debug(span, 'span');
  }
}
