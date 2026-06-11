/**
 * Server bootstrap.
 *
 * Wires the Express app onto an HTTP server, attaches the WebSocket streaming endpoint to the
 * same port via the `upgrade` event, ensures the Redis consumer group exists, and installs a
 * graceful-shutdown handler that stops accepting traffic, drains in-flight enqueues, and closes
 * Redis cleanly on SIGINT/SIGTERM.
 */
import http from 'node:http';
import { getConfig } from './config.js';
import { createApp } from './app.js';
import { logger } from './telemetry/logger.js';
import { ensureConsumerGroup, drainInFlight } from './services/jobQueue.js';
import { closeRedis } from './services/redis.js';
import {
  createWebSocketServer,
  handleUpgrade,
  setupWebSocketServer,
} from './services/websocket.js';

async function main(): Promise<void> {
  const config = getConfig();
  const app = createApp();
  const server = http.createServer(app);

  // WebSocket streaming on the same port.
  const wss = createWebSocketServer();
  setupWebSocketServer(wss);
  server.on('upgrade', (req, socket, head) => {
    handleUpgrade(wss, req, socket, head);
  });

  // Make sure the worker consumer group / stream exist before we accept traffic.
  try {
    await ensureConsumerGroup();
  } catch (err) {
    logger.warn({ err: (err as Error).message }, 'could not pre-create consumer group (will retry on first enqueue)');
  }

  await new Promise<void>((resolve) => {
    server.listen(config.PORT, config.HOST, resolve);
  });
  logger.info({ port: config.PORT, host: config.HOST, env: config.NODE_ENV }, 'API server listening');

  let shuttingDown = false;
  const shutdown = (signal: string): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info({ signal }, 'shutting down');

    void (async (): Promise<void> => {
      // Stop accepting new connections.
      server.close(() => logger.info('http server closed'));
      // Close open WebSockets.
      for (const client of wss.clients) {
        client.close(1001, 'server shutting down');
      }
      wss.close();
      // Let in-flight enqueues finish, then close Redis.
      await drainInFlight();
      await closeRedis();
      logger.info('shutdown complete');
      process.exit(0);
    })();

    // Hard exit if graceful shutdown stalls.
    setTimeout(() => {
      logger.error('graceful shutdown timed out; forcing exit');
      process.exit(1);
    }, 15_000).unref();
  };

  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('unhandledRejection', (reason) => {
    logger.error({ reason: String(reason) }, 'unhandled promise rejection');
  });
  process.on('uncaughtException', (err) => {
    logger.fatal({ err: err.message, stack: err.stack }, 'uncaught exception');
    shutdown('uncaughtException');
  });
}

main().catch((err: unknown) => {
  logger.fatal({ err: err instanceof Error ? err.message : String(err) }, 'failed to start');
  process.exit(1);
});
