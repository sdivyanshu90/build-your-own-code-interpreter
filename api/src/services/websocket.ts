/**
 * WebSocket streaming endpoint: real-time stdout/stderr from a running sandbox.
 *
 * Lifecycle:
 *   1. The HTTP upgrade is authenticated before the socket is accepted.
 *   2. The client sends one `start` frame containing an ExecutionRequest.
 *   3. We validate it, enqueue a job, and subscribe to that job's Redis Pub/Sub channel.
 *   4. The worker publishes stdout/stderr/status/done messages; we forward them as frames.
 *   5. On the `done` message (or timeout / disconnect) we close the socket and clean up.
 *
 * Robustness features required by the spec:
 *   - ping/pong heartbeat that terminates dead connections.
 *   - backpressure handling: if the client cannot keep up (send buffer over the high-water
 *     mark) we send an error frame and close rather than ballooning memory.
 */
import { IncomingMessage } from 'node:http';
import type { Duplex } from 'node:stream';
import { WebSocketServer, WebSocket } from 'ws';
import type Redis from 'ioredis';
import { authenticatePrincipal, headerBag, AuthError } from '../middleware/auth.js';
import { validateExecutionRequest } from '../middleware/validator.js';
import { enqueueJob, generateJobId } from './jobQueue.js';
import { createRedisConnection } from './redis.js';
import { newTraceContext } from '../telemetry/tracing.js';
import { logger } from '../telemetry/logger.js';
import { activeWebsockets } from '../telemetry/metrics.js';
import { REDIS_KEYS } from '../types/index.js';
import type { ClientFrame, Principal, ServerFrame, StreamMessage } from '../types/index.js';

/** Heartbeat interval; a socket that misses two pongs is terminated. */
const HEARTBEAT_INTERVAL_MS = 15_000;
/** Max buffered bytes before we consider the client too slow and disconnect. */
const BACKPRESSURE_HIGH_WATER_BYTES = 4 * 1024 * 1024;
/** A hard ceiling on how long a stream may stay open even if the worker never reports done. */
const MAX_STREAM_LIFETIME_MS = 120_000;

interface LiveSocket extends WebSocket {
  isAlive?: boolean;
  principal?: Principal;
}

/** Build the WebSocketServer in noServer mode; the HTTP server drives the upgrade. */
export function createWebSocketServer(): WebSocketServer {
  return new WebSocketServer({ noServer: true, maxPayload: 1024 * 1024 });
}

/**
 * Authenticate and complete an HTTP→WS upgrade for the streaming path. Rejects with a raw 401
 * if authentication fails (the socket is destroyed before the WS handshake completes).
 */
export function handleUpgrade(
  wss: WebSocketServer,
  req: IncomingMessage,
  socket: Duplex,
  head: Buffer,
): void {
  const url = new URL(req.url ?? '/', 'http://localhost');
  if (url.pathname !== '/v1/execute/stream') {
    socket.destroy();
    return;
  }

  let principal: Principal;
  try {
    const token = url.searchParams.get('token') ?? undefined;
    principal = authenticatePrincipal(headerBag(req.headers), token);
  } catch (err) {
    const status = err instanceof AuthError ? err.status : 401;
    socket.write(`HTTP/1.1 ${status} Unauthorized\r\nConnection: close\r\n\r\n`);
    socket.destroy();
    return;
  }

  wss.handleUpgrade(req, socket, head, (ws) => {
    (ws as LiveSocket).principal = principal;
    wss.emit('connection', ws, req);
  });
}

/** Wire up connection-level behaviour (heartbeat + per-connection protocol). */
export function setupWebSocketServer(wss: WebSocketServer): void {
  const heartbeat = setInterval(() => {
    for (const client of wss.clients) {
      const live = client as LiveSocket;
      if (live.isAlive === false) {
        live.terminate();
        continue;
      }
      live.isAlive = false;
      try {
        live.ping();
      } catch {
        live.terminate();
      }
    }
  }, HEARTBEAT_INTERVAL_MS);

  wss.on('close', () => clearInterval(heartbeat));

  wss.on('connection', (ws: WebSocket) => {
    handleConnection(ws as LiveSocket);
  });
}

/** Send a typed frame; returns false if the socket buffer is over the high-water mark. */
function send(ws: WebSocket, frame: ServerFrame): boolean {
  if (ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(frame));
  return ws.bufferedAmount < BACKPRESSURE_HIGH_WATER_BYTES;
}

/** Drive a single streaming connection end-to-end. */
function handleConnection(ws: LiveSocket): void {
  activeWebsockets.inc();
  ws.isAlive = true;
  ws.on('pong', () => {
    ws.isAlive = true;
  });

  const principal = ws.principal;
  if (!principal) {
    send(ws, { type: 'error', title: 'Unauthenticated', detail: 'No principal on socket.' });
    ws.close();
    activeWebsockets.dec();
    return;
  }

  let subscriber: Redis | null = null;
  let lifetimeTimer: NodeJS.Timeout | null = null;
  let settled = false;

  const cleanup = async (): Promise<void> => {
    if (lifetimeTimer) clearTimeout(lifetimeTimer);
    if (subscriber) {
      try {
        await subscriber.quit();
      } catch {
        subscriber.disconnect();
      }
      subscriber = null;
    }
    activeWebsockets.dec();
  };

  const finish = async (): Promise<void> => {
    if (settled) return;
    settled = true;
    await cleanup();
    if (ws.readyState === WebSocket.OPEN) ws.close();
  };

  ws.on('close', () => {
    void cleanup();
  });
  ws.on('error', () => {
    void finish();
  });

  ws.once('message', (raw: Buffer) => {
    void onStart(raw).catch(async (err: unknown) => {
      send(ws, {
        type: 'error',
        title: 'Stream error',
        detail: err instanceof Error ? err.message : 'unknown error',
      });
      await finish();
    });
  });

  async function onStart(raw: Buffer): Promise<void> {
    let frame: ClientFrame;
    try {
      frame = JSON.parse(raw.toString('utf8')) as ClientFrame;
    } catch {
      send(ws, { type: 'error', title: 'Bad frame', detail: 'First frame must be valid JSON.' });
      await finish();
      return;
    }
    if (frame.type !== 'start') {
      send(ws, { type: 'error', title: 'Bad frame', detail: 'Expected a `start` frame.' });
      await finish();
      return;
    }

    const validation = validateExecutionRequest(frame.request);
    if (!validation.ok) {
      send(ws, {
        type: 'error',
        title: 'Validation Error',
        detail: validation.errors.map((e) => `${e.field}: ${e.message}`).join('; '),
      });
      await finish();
      return;
    }

    const jobId = generateJobId();
    const trace = newTraceContext();
    const nowIso = new Date().toISOString();

    // Subscribe BEFORE enqueuing so we cannot miss early output.
    subscriber = createRedisConnection(`ws:${jobId}`);
    const channel = REDIS_KEYS.streamChannel(jobId);
    await subscriber.subscribe(channel);

    lifetimeTimer = setTimeout(() => {
      send(ws, { type: 'error', title: 'Stream timeout', detail: 'Maximum stream lifetime reached.' });
      void finish();
    }, MAX_STREAM_LIFETIME_MS);

    subscriber.on('message', (_ch: string, message: string) => {
      void onWorkerMessage(message);
    });

    await enqueueJob({
      jobId,
      userId: principal!.user_id,
      request: validation.value,
      trace,
      nowIso,
    });

    send(ws, { type: 'accepted', job_id: jobId });
    logger.info({ jobId, userId: principal!.user_id, mode: 'stream' }, 'stream job accepted');
  }

  async function onWorkerMessage(message: string): Promise<void> {
    let msg: StreamMessage;
    try {
      msg = JSON.parse(message) as StreamMessage;
    } catch {
      return;
    }

    let ok = true;
    switch (msg.kind) {
      case 'stdout':
        ok = send(ws, { type: 'stdout', data: msg.data });
        break;
      case 'stderr':
        ok = send(ws, { type: 'stderr', data: msg.data });
        break;
      case 'status':
        ok = send(ws, { type: 'status', status: msg.status });
        break;
      case 'done':
        send(ws, {
          type: 'exit',
          exit_code: msg.exit_code,
          status: msg.status,
          wall_time_ms: msg.wall_time_ms,
          timed_out: msg.timed_out,
          oom_killed: msg.oom_killed,
        });
        await finish();
        return;
    }

    if (!ok) {
      send(ws, {
        type: 'error',
        title: 'Backpressure',
        detail: 'Client is too slow to consume output; closing the stream.',
      });
      await finish();
    }
  }
}
