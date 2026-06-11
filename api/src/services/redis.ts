/**
 * Redis client factory and a lightweight circuit breaker.
 *
 * We keep three logical connections:
 *  - `command` for normal commands (queue writes, KV reads),
 *  - `subscriber` for Pub/Sub (a connection in subscribe mode cannot issue normal commands),
 *  - `blocking` for blocking reads if needed.
 *
 * The circuit breaker fails fast when Redis is unhealthy so the API returns 503 quickly instead
 * of piling up connections, and recovers automatically (half-open probe) once Redis is back.
 */
import Redis from 'ioredis';
import { getConfig } from '../config.js';
import { logger } from '../telemetry/logger.js';

let commandClient: Redis | null = null;
let subscriberClient: Redis | null = null;

/* v8 ignore start -- real Redis connection wiring; exercised by integration, not unit-mockable */
function build(label: string): Redis {
  const config = getConfig();
  const client = new Redis(config.REDIS_URL, {
    lazyConnect: false,
    maxRetriesPerRequest: 2,
    enableReadyCheck: true,
    retryStrategy: (times: number): number => Math.min(times * 200, 2000),
    reconnectOnError: (): boolean => true,
  });
  client.on('error', (err: Error) => {
    logger.warn({ redis: label, err: err.message }, 'redis connection error');
  });
  client.on('ready', () => {
    logger.info({ redis: label }, 'redis ready');
  });
  return client;
}

/** The shared command connection. */
export function getRedis(): Redis {
  if (commandClient === null) {
    commandClient = build('command');
  }
  return commandClient;
}

/** A dedicated shared subscriber connection (Pub/Sub mode). */
export function getSubscriber(): Redis {
  if (subscriberClient === null) {
    subscriberClient = build('subscriber');
  }
  return subscriberClient;
}

/**
 * Create a fresh, independent Redis connection. Used to give each WebSocket stream its own
 * subscriber so per-job channel subscribe/unsubscribe never interferes with other connections.
 * The caller owns the returned connection and must `.quit()` it.
 */
export function createRedisConnection(label: string): Redis {
  return build(label);
}

/** Gracefully close all Redis connections. */
export async function closeRedis(): Promise<void> {
  await Promise.allSettled([commandClient?.quit(), subscriberClient?.quit()]);
  commandClient = null;
  subscriberClient = null;
}
/* v8 ignore stop */

/** Possible breaker states. */
type BreakerState = 'closed' | 'open' | 'half-open';

/**
 * A minimal circuit breaker. After `threshold` consecutive failures it opens for `cooldownMs`;
 * the next call after cooldown is a half-open probe that closes the breaker on success.
 */
export class CircuitBreaker {
  private state: BreakerState = 'closed';
  private failures = 0;
  private openedAt = 0;

  constructor(
    private readonly name: string,
    private readonly threshold = 5,
    private readonly cooldownMs = 5000,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** Execute `fn` through the breaker, throwing fast when open. */
  async run<T>(fn: () => Promise<T>): Promise<T> {
    if (this.state === 'open') {
      if (this.now() - this.openedAt >= this.cooldownMs) {
        this.state = 'half-open';
      } else {
        throw new CircuitOpenError(this.name);
      }
    }
    try {
      const result = await fn();
      this.onSuccess();
      return result;
    } catch (err) {
      this.onFailure();
      throw err;
    }
  }

  private onSuccess(): void {
    this.failures = 0;
    this.state = 'closed';
  }

  private onFailure(): void {
    this.failures += 1;
    if (this.failures >= this.threshold) {
      this.state = 'open';
      this.openedAt = this.now();
      logger.error({ breaker: this.name }, 'circuit breaker opened');
    }
  }

  /** Current state (for health checks / tests). */
  getState(): BreakerState {
    return this.state;
  }
}

/** Thrown when a call is rejected because the breaker is open. */
export class CircuitOpenError extends Error {
  constructor(name: string) {
    super(`circuit '${name}' is open`);
    this.name = 'CircuitOpenError';
  }
}

/** The breaker guarding Redis writes/reads on the request path. */
export const redisBreaker = new CircuitBreaker('redis');
