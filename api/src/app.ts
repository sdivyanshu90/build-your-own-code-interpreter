/**
 * Express application factory.
 *
 * Kept separate from the server bootstrap (`index.ts`) so tests can mount the app with supertest
 * without binding a port or starting the WebSocket server. Applies security headers, CORS with a
 * strict origin allowlist, JSON body limits, structured request logging, the v1 routers, and the
 * centralised 404 + error handlers (in that order — error handler last).
 */
import { randomUUID } from 'node:crypto';
import express, { type Express } from 'express';
import cors from 'cors';
import helmet from 'helmet';
import pinoHttp from 'pino-http';
import { getConfig } from './config.js';
import { logger } from './telemetry/logger.js';
import { httpRequestDuration, httpRequestsTotal } from './telemetry/metrics.js';
import { executeRouter } from './routes/execute.js';
import { jobsRouter } from './routes/jobs.js';
import { languagesRouter } from './routes/languages.js';
import { healthRouter } from './routes/health.js';
import { errorHandler, notFoundHandler } from './middleware/errorHandler.js';

/** Build a fully-configured Express app (no network binding). */
export function createApp(): Express {
  const config = getConfig();
  const app = express();

  // Behind exactly one trusted proxy/LB hop (for correct req.ip and X-Forwarded-For).
  app.set('trust proxy', 1);
  app.disable('x-powered-by');

  // Security headers: CSP, X-Frame-Options, X-Content-Type-Options, etc.
  app.use(
    helmet({
      contentSecurityPolicy: {
        directives: {
          defaultSrc: ["'none'"],
          frameAncestors: ["'none'"],
          baseUri: ["'none'"],
        },
      },
      crossOriginResourcePolicy: { policy: 'same-site' },
    }),
  );
  app.use(helmet.frameguard({ action: 'deny' }));
  app.use(helmet.noSniff());

  // CORS with an explicit allowlist — never reflect arbitrary origins.
  app.use(
    cors({
      origin: (origin, cb) => {
        if (!origin || config.CORS_ORIGINS.includes(origin)) {
          cb(null, true);
          return;
        }
        cb(new Error(`Origin ${origin} not allowed by CORS`));
      },
      methods: ['GET', 'POST', 'DELETE', 'OPTIONS'],
      allowedHeaders: ['Content-Type', 'Authorization', 'X-API-Key', 'traceparent'],
      maxAge: 600,
    }),
  );

  // Bounded JSON bodies (defence against memory-exhaustion via huge payloads).
  app.use(express.json({ limit: config.MAX_CODE_SIZE_BYTES + config.MAX_STDIN_BYTES + 64 * 1024 }));

  // Structured request logging with a generated request id.
  app.use(
    pinoHttp({
      logger,
      genReqId: (req, res) => {
        const existing = req.headers['x-request-id'];
        const id = typeof existing === 'string' ? existing : randomUUID();
        res.setHeader('x-request-id', id);
        return id;
      },
      autoLogging: { ignore: (req) => req.url === '/v1/health' || req.url === '/v1/metrics' },
    }),
  );

  // Per-request Prometheus instrumentation.
  app.use((req, res, next) => {
    const end = httpRequestDuration.startTimer();
    res.on('finish', () => {
      const matched = req.route as { path?: string } | undefined;
      const route = matched?.path ? `${req.baseUrl}${matched.path}` : req.path;
      const labels = { method: req.method, route, code: String(res.statusCode) };
      httpRequestsTotal.inc(labels);
      end(labels);
    });
    next();
  });

  // API surface (all under /v1).
  app.use('/v1', executeRouter);
  app.use('/v1', jobsRouter);
  app.use('/v1', languagesRouter);
  app.use('/v1', healthRouter);

  // 404 then the terminal error handler (must be last and have four args).
  app.use(notFoundHandler);
  app.use(errorHandler);

  return app;
}
