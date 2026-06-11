/**
 * Structured JSON logging via pino.
 *
 * All logs are single-line JSON so Loki/Promtail can ingest them without a parser. Control
 * characters in user-influenced fields are escaped by pino's serialiser, neutralising log
 * forging from untrusted code output.
 */
import pino from 'pino';
import { getConfig } from '../config.js';

const config = getConfig();

export const logger = pino({
  level: config.LOG_LEVEL,
  base: {
    service: config.OTEL_SERVICE_NAME,
    env: config.NODE_ENV,
  },
  timestamp: pino.stdTimeFunctions.isoTime,
  formatters: {
    level: (label) => ({ level: label }),
  },
  redact: {
    paths: ['req.headers.authorization', 'req.headers["x-api-key"]', '*.code', '*.stdin'],
    censor: '[redacted]',
  },
});

/** Returns a child logger bound to a job/trace context for correlation. */
export function jobLogger(fields: Record<string, string | number | boolean>): pino.Logger {
  return logger.child(fields);
}
