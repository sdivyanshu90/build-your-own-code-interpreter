/** Unit tests for the Redis circuit breaker. */
import { describe, it, expect } from 'vitest';
import { CircuitBreaker, CircuitOpenError } from '../../api/src/services/redis.js';

describe('CircuitBreaker', () => {
  it('passes through results while closed', async () => {
    const cb = new CircuitBreaker('t', 3, 1000);
    expect(await cb.run(async () => 42)).toBe(42);
    expect(cb.getState()).toBe('closed');
  });

  it('opens after the failure threshold and fails fast', async () => {
    const cb = new CircuitBreaker('t', 2, 1000, () => 0);
    await expect(cb.run(async () => { throw new Error('boom'); })).rejects.toThrow('boom');
    await expect(cb.run(async () => { throw new Error('boom'); })).rejects.toThrow('boom');
    expect(cb.getState()).toBe('open');
    // While open, the call is rejected without invoking fn.
    await expect(cb.run(async () => 1)).rejects.toBeInstanceOf(CircuitOpenError);
  });

  it('half-opens after the cooldown and closes on success', async () => {
    let now = 0;
    const cb = new CircuitBreaker('t', 1, 500, () => now);
    await expect(cb.run(async () => { throw new Error('x'); })).rejects.toThrow();
    expect(cb.getState()).toBe('open');
    now = 600; // past cooldown
    expect(await cb.run(async () => 'ok')).toBe('ok');
    expect(cb.getState()).toBe('closed');
  });

  it('re-opens if the half-open probe fails', async () => {
    let now = 0;
    const cb = new CircuitBreaker('t', 1, 500, () => now);
    await expect(cb.run(async () => { throw new Error('x'); })).rejects.toThrow();
    now = 600;
    await expect(cb.run(async () => { throw new Error('still down'); })).rejects.toThrow('still down');
    expect(cb.getState()).toBe('open');
  });

  it('CircuitOpenError carries the breaker name', () => {
    const err = new CircuitOpenError('redis');
    expect(err.name).toBe('CircuitOpenError');
    expect(err.message).toContain('redis');
  });
});
