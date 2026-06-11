/**
 * An in-memory Redis double for unit tests.
 *
 * Implements the subset of commands the API uses, including a faithful re-implementation of the
 * two Lua scripts (sliding-window rate limit and concurrency quota) so tests exercise the real
 * admission logic without a Redis server. Cast to `unknown as Redis` where a typed client is
 * required.
 */

interface ZEntry {
  member: string;
  score: number;
}

/** A pipeline that records queued commands and applies them on exec(). */
class FakePipeline {
  private readonly ops: Array<() => unknown> = [];
  constructor(private readonly redis: FakeRedis) {}

  xadd(stream: string, id: string, ...fieldValues: string[]): this {
    this.ops.push(() => this.redis.xaddSync(stream, id, fieldValues));
    return this;
  }

  set(key: string, value: string, ...args: string[]): this {
    this.ops.push(() => this.redis.setSync(key, value, args));
    return this;
  }

  async exec(): Promise<Array<[Error | null, unknown]>> {
    return this.ops.map((op) => {
      try {
        return [null, op()] as [Error | null, unknown];
      } catch (err) {
        return [err as Error, null] as [Error | null, unknown];
      }
    });
  }
}

export class FakeRedis {
  private readonly kv = new Map<string, string>();
  private readonly zsets = new Map<string, ZEntry[]>();
  private readonly streams = new Map<string, Array<{ id: string; fields: Record<string, string> }>>();
  private readonly groups = new Set<string>();
  private readonly subscribedChannels = new Set<string>();
  private readonly messageListeners: Array<(channel: string, message: string) => void> = [];
  private seq = 0;
  /** Set to true to make every operation throw (simulate a Redis outage). */
  public failing = false;

  private guard(): void {
    if (this.failing) throw new Error('FakeRedis: simulated outage');
  }

  async ping(): Promise<string> {
    this.guard();
    return 'PONG';
  }

  async get(key: string): Promise<string | null> {
    this.guard();
    return this.kv.has(key) ? (this.kv.get(key) as string) : null;
  }

  setSync(key: string, value: string, _args: string[]): 'OK' {
    this.guard();
    this.kv.set(key, value);
    return 'OK';
  }

  async set(key: string, value: string, ...args: string[]): Promise<'OK'> {
    return this.setSync(key, value, args);
  }

  async del(key: string): Promise<number> {
    this.guard();
    return this.kv.delete(key) ? 1 : 0;
  }

  async exists(key: string): Promise<number> {
    this.guard();
    return this.kv.has(key) ? 1 : 0;
  }

  xaddSync(stream: string, _id: string, fieldValues: string[]): string {
    this.guard();
    const entries = this.streams.get(stream) ?? [];
    const fields: Record<string, string> = {};
    for (let i = 0; i < fieldValues.length; i += 2) {
      fields[fieldValues[i] as string] = fieldValues[i + 1] as string;
    }
    this.seq += 1;
    const id = `${Date.now()}-${this.seq}`;
    entries.push({ id, fields });
    this.streams.set(stream, entries);
    return id;
  }

  async xadd(stream: string, id: string, ...fieldValues: string[]): Promise<string> {
    return this.xaddSync(stream, id, fieldValues);
  }

  async xlen(stream: string): Promise<number> {
    this.guard();
    return this.streams.get(stream)?.length ?? 0;
  }

  async xgroup(action: string, stream: string, group: string, ..._rest: string[]): Promise<string> {
    this.guard();
    const key = `${stream}:${group}`;
    if (action === 'CREATE') {
      if (this.groups.has(key)) {
        throw new Error('BUSYGROUP Consumer Group name already exists');
      }
      this.groups.add(key);
      if (!this.streams.has(stream)) this.streams.set(stream, []);
    }
    return 'OK';
  }

  multi(): FakePipeline {
    return new FakePipeline(this);
  }

  async publish(channel: string, message: string): Promise<number> {
    this.guard();
    let delivered = 0;
    if (this.subscribedChannels.has(channel)) {
      for (const listener of this.messageListeners) {
        listener(channel, message);
        delivered += 1;
      }
    }
    return delivered;
  }

  async subscribe(...channels: string[]): Promise<number> {
    for (const channel of channels) this.subscribedChannels.add(channel);
    return this.subscribedChannels.size;
  }

  on(event: string, handler: (channel: string, message: string) => void): this {
    if (event === 'message') this.messageListeners.push(handler);
    return this;
  }

  disconnect(): void {
    /* no-op for the fake */
  }

  async zrem(key: string, member: string): Promise<number> {
    this.guard();
    const set = this.zsets.get(key);
    if (!set) return 0;
    const before = set.length;
    this.zsets.set(key, set.filter((e) => e.member !== member));
    return before - (this.zsets.get(key)?.length ?? 0);
  }

  /**
   * Re-implementation of the rate-limit / quota Lua scripts. Recognises the rate-limit script by
   * its `limit - count - 1` remaining computation; everything else is treated as the quota script
   * (returns 1/0). Both share the sliding-window admission against a sorted set.
   */
  async eval(script: string, _numkeys: number, ...args: string[]): Promise<unknown> {
    this.guard();
    const [key, nowStr, windowStr, limitStr, member] = args;
    const now = Number(nowStr);
    const window = Number(windowStr);
    const limit = Number(limitStr);
    const isRateLimit = script.includes('limit - count - 1');

    const set = (this.zsets.get(key as string) ?? []).filter((e) => e.score > now - window);
    const count = set.length;

    if (count < limit) {
      set.push({ member: member as string, score: now });
      this.zsets.set(key as string, set);
      return isRateLimit ? [1, limit - count - 1, 0] : 1;
    }
    this.zsets.set(key as string, set);
    if (!isRateLimit) return 0;
    const oldest = Math.min(...set.map((e) => e.score));
    const retry = Math.max(1, oldest + window - now);
    return [0, 0, retry];
  }

  async flushdb(): Promise<'OK'> {
    this.kv.clear();
    this.zsets.clear();
    this.streams.clear();
    this.groups.clear();
    this.subscribedChannels.clear();
    this.messageListeners.length = 0;
    return 'OK';
  }

  async quit(): Promise<'OK'> {
    return 'OK';
  }

  // Test inspection helpers.
  _stream(name: string): Array<{ id: string; fields: Record<string, string> }> {
    return this.streams.get(name) ?? [];
  }

  _rawGet(key: string): string | undefined {
    return this.kv.get(key);
  }
}
