/** Unit tests for W3C trace-context propagation helpers. */
import { describe, it, expect } from 'vitest';
import { newTraceContext, extractTraceId, recordSpan } from '../../api/src/telemetry/tracing.js';

describe('tracing', () => {
  it('generates a valid traceparent', () => {
    const ctx = newTraceContext();
    expect(ctx.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
    expect(ctx.trace_id).toMatch(/^[0-9a-f]{32}$/);
  });

  it('reuses the trace id from a valid parent traceparent', () => {
    const parent = '00-' + 'a'.repeat(32) + '-' + 'b'.repeat(16) + '-01';
    const ctx = newTraceContext(parent);
    expect(ctx.trace_id).toBe('a'.repeat(32));
  });

  it('generates a fresh trace id when the parent is invalid', () => {
    const ctx = newTraceContext('garbage');
    expect(ctx.trace_id).toMatch(/^[0-9a-f]{32}$/);
    expect(ctx.trace_id).not.toBe('garbage');
  });

  it('extractTraceId validates the format', () => {
    expect(extractTraceId(undefined)).toBeNull();
    expect(extractTraceId('badformat')).toBeNull();
    expect(extractTraceId('00-' + '0'.repeat(32) + '-' + 'b'.repeat(16) + '-01')).toBeNull(); // all-zero
    const valid = '00-' + 'c'.repeat(32) + '-' + 'd'.repeat(16) + '-01';
    expect(extractTraceId(valid)).toBe('c'.repeat(32));
  });

  it('recordSpan does not throw', () => {
    const ctx = newTraceContext();
    expect(() => recordSpan('test.span', ctx, { foo: 'bar', n: 1 })).not.toThrow();
  });
});
