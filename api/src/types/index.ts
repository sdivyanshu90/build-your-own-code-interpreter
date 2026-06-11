/**
 * Shared type definitions and protocol constants for the Code Interpreter Sandbox API.
 *
 * These types are the canonical data contract between the API server, the Redis queue,
 * and the Python worker. The worker mirrors them as dataclasses/Pydantic models. Any change
 * here MUST be reflected in `worker/sandbox/*` and the API documentation.
 */

/** Supported language runtime identifiers. The single source of truth for valid languages. */
export const LANGUAGES = [
  'python',
  'javascript',
  'typescript',
  'java',
  'go',
  'ruby',
  'rust',
  'bash',
] as const;

export type Language = (typeof LANGUAGES)[number];

/**
 * Job lifecycle states.
 * - PENDING:   enqueued, not yet picked up by a worker.
 * - RUNNING:   a worker is executing the sandbox.
 * - COMPLETED: the user code ran to completion (any exit code — even non-zero — counts).
 * - FAILED:    infrastructure/sandbox failure or OOM kill (not the user's fault).
 * - TIMEOUT:   the wall-clock timeout fired and the container was killed.
 * - KILLED:    cancelled by the user via DELETE /v1/jobs/:id.
 */
export const JOB_STATUSES = [
  'PENDING',
  'RUNNING',
  'COMPLETED',
  'FAILED',
  'TIMEOUT',
  'KILLED',
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

/** Terminal states — a job in one of these will never transition again. */
export const TERMINAL_STATUSES: readonly JobStatus[] = [
  'COMPLETED',
  'FAILED',
  'TIMEOUT',
  'KILLED',
];

/** User tiers, ordered by privilege. Drives rate limits and quotas. */
export type UserTier = 'anonymous' | 'authenticated' | 'premium';

/** An additional input file mounted read-only alongside the user code. */
export interface InputFile {
  /** Filename, confined to the sandbox dir. Sanitised: no path separators, no '..'. */
  name: string;
  /** UTF-8 file content. */
  content: string;
}

/** A submission to execute. */
export interface ExecutionRequest {
  language: Language;
  /** Source code to run. Size bounded by MAX_CODE_SIZE_BYTES. */
  code: string;
  /** Optional bytes piped to the program's stdin. Bounded by MAX_STDIN_BYTES. */
  stdin?: string;
  /** Wall-clock timeout in seconds. Clamped to [1, MAX_TIMEOUT_SECONDS]. */
  timeout_seconds?: number;
  /** Environment variables. Disallowed names (PATH, LD_*, etc.) are stripped. */
  env_vars?: Record<string, string>;
  /** Extra read-only files mounted with the code. */
  files?: InputFile[];
}

/** A file produced by the execution, stored in object storage. */
export interface OutputFile {
  name: string;
  size_bytes: number;
  /** Presigned MinIO URL (time-limited). */
  url: string;
}

/** The result of an execution. */
export interface ExecutionResult {
  job_id: string;
  status: JobStatus;
  stdout: string;
  stderr: string;
  /** Process exit code; null if the process was killed before exiting. */
  exit_code: number | null;
  wall_time_ms: number;
  cpu_time_ms: number;
  /** Peak memory observed via the container cgroup, in bytes. */
  memory_bytes: number;
  oom_killed: boolean;
  timed_out: boolean;
  /** True if stdout/stderr was truncated at output_max_bytes. */
  truncated: boolean;
  files: OutputFile[];
}

/** Full audit + lifecycle record for a job, persisted in Redis. */
export interface JobRecord {
  job_id: string;
  user_id: string;
  language: Language;
  status: JobStatus;
  request: ExecutionRequest;
  result?: ExecutionResult;
  created_at: string;
  updated_at: string;
  worker_id?: string;
  retries: number;
}

/** Per-language resource envelope. */
export interface ResourceConfig {
  memory_mb: number;
  cpu_quota: number;
  timeout_seconds: number;
  pids_limit: number;
  disk_mb: number;
  network_enabled: boolean;
  output_max_bytes: number;
}

/** Public metadata about a supported runtime (for GET /v1/languages). */
export interface RuntimeInfo {
  id: Language;
  name: string;
  version: string;
  default_timeout_seconds: number;
  memory_mb: number;
}

/** The authenticated principal attached to a request. */
export interface Principal {
  user_id: string;
  tier: UserTier;
  /** Distinguishes a JWT-authenticated user from an API-key client. */
  auth_method: 'jwt' | 'api_key' | 'anonymous';
}

/** WebSocket protocol — client → server frames. */
export type ClientFrame = {
  type: 'start';
  request: ExecutionRequest;
};

/** WebSocket protocol — server → client frames. */
export type ServerFrame =
  | { type: 'accepted'; job_id: string }
  | { type: 'status'; status: JobStatus }
  | { type: 'stdout'; data: string }
  | { type: 'stderr'; data: string }
  | {
      type: 'exit';
      exit_code: number | null;
      status: JobStatus;
      wall_time_ms: number;
      timed_out: boolean;
      oom_killed: boolean;
    }
  | { type: 'error'; title: string; detail: string };

/**
 * The payload published by the worker on the per-job Pub/Sub channel for live streaming.
 * Mirrors `worker/queue/publisher.py`.
 */
export type StreamMessage =
  | { kind: 'stdout'; data: string }
  | { kind: 'stderr'; data: string }
  | { kind: 'status'; status: JobStatus }
  | {
      kind: 'done';
      exit_code: number | null;
      status: JobStatus;
      wall_time_ms: number;
      timed_out: boolean;
      oom_killed: boolean;
    };

/** ───────────────────────────── Redis key conventions ─────────────────────────────
 * Centralised so the API and worker never disagree on key names.
 */
export const REDIS_KEYS = {
  /** The job stream consumed by workers. */
  JOB_STREAM: 'sandbox:jobs',
  /** Dead-letter stream for jobs that exhausted retries. */
  DEAD_LETTER_STREAM: 'sandbox:jobs:dead',
  /** Consumer group name shared by all workers. */
  CONSUMER_GROUP: 'workers',
  /** Per-job record hash:  sandbox:job:<job_id> */
  jobRecord: (jobId: string): string => `sandbox:job:${jobId}`,
  /** Per-job result (JSON):  sandbox:result:<job_id> */
  result: (jobId: string): string => `sandbox:result:${jobId}`,
  /** Live-output Pub/Sub channel:  sandbox:stream:<job_id> */
  streamChannel: (jobId: string): string => `sandbox:stream:${jobId}`,
  /** Cancellation signal key:  sandbox:cancel:<job_id> */
  cancel: (jobId: string): string => `sandbox:cancel:${jobId}`,
  /** Rate-limit sorted set:  sandbox:ratelimit:<scope>:<id> */
  rateLimit: (scope: string, id: string): string => `sandbox:ratelimit:${scope}:${id}`,
  /** Concurrency counter:  sandbox:concurrency:<user_id> */
  concurrency: (userId: string): string => `sandbox:concurrency:${userId}`,
} as const;

/** Default TTL (seconds) for job records and results held in the Redis hot tier. */
export const RESULT_TTL_SECONDS = 3600;
