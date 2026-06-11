/**
 * Typed, validated application configuration.
 *
 * Every environment variable is parsed and validated with zod at startup. If anything is
 * missing, malformed, or insecure (e.g. a weak JWT secret), the process refuses to boot with
 * a descriptive error rather than failing unpredictably at runtime.
 */
import { z } from 'zod';

/** Minimum acceptable JWT secret length, in characters, to resist brute force. */
const MIN_JWT_SECRET_LENGTH = 32;

/** A weak/placeholder secret denylist — refuse to boot if one of these is configured. */
const FORBIDDEN_JWT_SECRETS = new Set([
  'secret',
  'changeme',
  'change-me',
  'password',
  'jwt-secret',
  'your-secret-here',
  'dev',
  'test',
]);

/** Coerce a comma-separated string into a trimmed, non-empty array. */
const csvToArray = (value: string): string[] =>
  value
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);

const ConfigSchema = z.object({
  NODE_ENV: z.enum(['development', 'test', 'production']).default('development'),

  PORT: z.coerce.number().int().positive().max(65535).default(8080),

  /** Bind host; default to all interfaces inside a container. */
  HOST: z.string().default('0.0.0.0'),

  REDIS_URL: z
    .string()
    .url()
    .refine((u) => u.startsWith('redis://') || u.startsWith('rediss://'), {
      message: 'REDIS_URL must start with redis:// or rediss://',
    }),

  MINIO_ENDPOINT: z.string().min(1),
  MINIO_PORT: z.coerce.number().int().positive().max(65535).default(9000),
  MINIO_USE_SSL: z
    .enum(['true', 'false'])
    .default('false')
    .transform((v) => v === 'true'),
  MINIO_ACCESS_KEY: z.string().min(3),
  MINIO_SECRET_KEY: z.string().min(8),
  MINIO_BUCKET: z.string().min(1).default('sandbox-artifacts'),

  JWT_SECRET: z
    .string()
    .min(MIN_JWT_SECRET_LENGTH, {
      message: `JWT_SECRET must be at least ${MIN_JWT_SECRET_LENGTH} characters`,
    })
    .refine((s) => !FORBIDDEN_JWT_SECRETS.has(s.toLowerCase()), {
      message: 'JWT_SECRET is a well-known weak value; use a strong random secret',
    }),

  /** Comma-separated API keys (machine clients). Format: "<key>:<tier>". Optional. */
  API_KEYS: z.string().default(''),

  /** Whether unauthenticated (anonymous) execution is allowed. */
  ALLOW_ANONYMOUS: z
    .enum(['true', 'false'])
    .default('false')
    .transform((v) => v === 'true'),

  MAX_CODE_SIZE_BYTES: z.coerce.number().int().positive().default(256 * 1024),
  MAX_STDIN_BYTES: z.coerce.number().int().positive().default(256 * 1024),
  MAX_FILES: z.coerce.number().int().nonnegative().default(8),
  MAX_FILE_SIZE_BYTES: z.coerce.number().int().positive().default(256 * 1024),
  MAX_ENV_VARS: z.coerce.number().int().nonnegative().default(32),
  MAX_ENV_VALUE_LENGTH: z.coerce.number().int().positive().default(4096),

  DEFAULT_TIMEOUT_SECONDS: z.coerce.number().int().positive().default(10),
  MAX_TIMEOUT_SECONDS: z.coerce.number().int().positive().default(30),

  /** The wall-clock ceiling for the synchronous /v1/execute long-poll. */
  SYNC_EXECUTION_TIMEOUT_SECONDS: z.coerce.number().int().positive().default(30),

  RATE_LIMIT_REQUESTS_PER_MINUTE: z.coerce.number().int().positive().default(60),
  RATE_LIMIT_ANON_PER_MINUTE: z.coerce.number().int().positive().default(10),
  RATE_LIMIT_PREMIUM_PER_MINUTE: z.coerce.number().int().positive().default(600),

  /** Concurrent in-flight jobs allowed per user. */
  MAX_CONCURRENT_JOBS: z.coerce.number().int().positive().default(5),
  MAX_CONCURRENT_JOBS_PREMIUM: z.coerce.number().int().positive().default(25),

  WORKER_CONCURRENCY: z.coerce.number().int().positive().default(4),

  /** Comma-separated allowlist of CORS origins. Never '*' in production. */
  CORS_ORIGINS: z.string().default('http://localhost:3000'),

  /** OpenTelemetry OTLP endpoint; empty disables tracing export. */
  OTEL_EXPORTER_OTLP_ENDPOINT: z.string().default(''),
  OTEL_SERVICE_NAME: z.string().default('sandbox-api'),

  LOG_LEVEL: z.enum(['trace', 'debug', 'info', 'warn', 'error', 'fatal']).default('info'),
});

export type AppConfig = Omit<z.infer<typeof ConfigSchema>, 'CORS_ORIGINS' | 'API_KEYS'> & {
  CORS_ORIGINS: string[];
  /** Parsed API keys: map of key → tier. */
  API_KEY_MAP: Map<string, UserTierLike>;
};

type UserTierLike = 'authenticated' | 'premium';

/** Parse and validate the given environment, throwing a readable error on failure. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const parsed = ConfigSchema.safeParse(env);
  if (!parsed.success) {
    const issues = parsed.error.issues
      .map((i) => `  - ${i.path.join('.') || '(root)'}: ${i.message}`)
      .join('\n');
    throw new Error(`Invalid configuration:\n${issues}`);
  }

  const data = parsed.data;

  if (data.NODE_ENV === 'production' && data.CORS_ORIGINS.includes('*')) {
    throw new Error('CORS_ORIGINS must not be "*" in production');
  }
  if (data.MAX_TIMEOUT_SECONDS < data.DEFAULT_TIMEOUT_SECONDS) {
    throw new Error('MAX_TIMEOUT_SECONDS must be >= DEFAULT_TIMEOUT_SECONDS');
  }

  const apiKeyMap = new Map<string, UserTierLike>();
  for (const entry of csvToArray(data.API_KEYS)) {
    const [key, tier] = entry.split(':');
    if (!key) continue;
    apiKeyMap.set(key, tier === 'premium' ? 'premium' : 'authenticated');
  }

  return {
    ...data,
    CORS_ORIGINS: csvToArray(data.CORS_ORIGINS),
    API_KEY_MAP: apiKeyMap,
  };
}

/** The singleton config, loaded once at module import. */
let cached: AppConfig | null = null;

/** Returns the validated config, loading it on first call. */
export function getConfig(): AppConfig {
  if (cached === null) {
    cached = loadConfig();
  }
  return cached;
}

/** Test helper: reset the cached config so a new environment can be loaded. */
export function resetConfigForTests(): void {
  cached = null;
}
