/**
 * Request validation with zod, producing RFC 7807 problems on failure.
 *
 * The validator is the API's first line of defence: it enforces size caps, clamps timeouts,
 * strips dangerous environment variable names, and sanitises filenames so nothing downstream
 * ever sees an unbounded or malformed `ExecutionRequest`. The core `validateExecutionRequest`
 * is reused by both the HTTP route and the WebSocket handler.
 */
import { z } from 'zod';
import type { Request, Response, NextFunction } from 'express';
import { getConfig } from '../config.js';
import { isSupportedLanguage } from '../languages.js';
import { LANGUAGES } from '../types/index.js';
import type { ExecutionRequest } from '../types/index.js';
import { problem } from './errorHandler.js';

/**
 * Environment variable names that must never be set by user code, because they alter the
 * runtime's behaviour or the dynamic linker (a classic injection vector). Stripped silently.
 */
const DISALLOWED_ENV_NAMES = new Set([
  'PATH',
  'LD_PRELOAD',
  'LD_LIBRARY_PATH',
  'LD_AUDIT',
  'NODE_OPTIONS',
  'PYTHONPATH',
  'PYTHONSTARTUP',
  'BASH_ENV',
  'ENV',
  'IFS',
  'SHELLOPTS',
  'RUBYOPT',
  'PERL5LIB',
  'GEM_PATH',
  'CLASSPATH',
  'JAVA_TOOL_OPTIONS',
  'HOME',
  'SANDBOX',
]);

/** Valid env var name: letters, digits, underscore; not starting with a digit. */
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Sanitise an input filename so it is confined to the sandbox directory:
 * reject absolute paths and any path component, collapse to a safe basename. Returns null if the
 * name cannot be made safe (empty after stripping).
 */
export function sanitizeFilename(name: string): string | null {
  // Disallow null bytes outright.
  if (name.includes('\0')) return null;
  // Strip any directory components and traversal — keep only the final path segment.
  const base = name.replace(/\\/g, '/').split('/').filter(Boolean).pop() ?? '';
  if (base === '' || base === '.' || base === '..') return null;
  // Allow a conservative character set only.
  if (!/^[A-Za-z0-9._-]+$/.test(base)) return null;
  if (base.length > 255) return null;
  return base;
}

/** Build the zod schema using current config limits. */
function buildSchema(): z.ZodType<ExecutionRequest> {
  const config = getConfig();

  const envSchema = z
    .record(z.string(), z.string().max(config.MAX_ENV_VALUE_LENGTH))
    .optional()
    .transform((env) => {
      if (!env) return undefined;
      const cleaned: Record<string, string> = {};
      let count = 0;
      for (const [name, value] of Object.entries(env)) {
        if (count >= config.MAX_ENV_VARS) break;
        if (!ENV_NAME_RE.test(name)) continue;
        if (DISALLOWED_ENV_NAMES.has(name.toUpperCase())) continue;
        cleaned[name] = value;
        count += 1;
      }
      return cleaned;
    });

  const filesSchema = z
    .array(
      z.object({
        name: z.string().min(1),
        content: z.string(),
      }),
    )
    .max(config.MAX_FILES)
    .optional()
    .superRefine((files, ctx) => {
      if (!files) return;
      for (const [i, f] of files.entries()) {
        if (sanitizeFilename(f.name) === null) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: [i, 'name'],
            message: `Unsafe filename '${f.name}'`,
          });
        }
        if (Buffer.byteLength(f.content, 'utf8') > config.MAX_FILE_SIZE_BYTES) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: [i, 'content'],
            message: `File '${f.name}' exceeds ${config.MAX_FILE_SIZE_BYTES} bytes`,
          });
        }
      }
    })
    .transform((files) =>
      files?.map((f) => ({ name: sanitizeFilename(f.name) as string, content: f.content })),
    );

  return z
    .object({
      language: z
        .string()
        .refine(isSupportedLanguage, { message: `language must be one of: ${LANGUAGES.join(', ')}` }),
      code: z
        .string()
        .min(1, { message: 'code must not be empty' })
        .refine((c) => Buffer.byteLength(c, 'utf8') <= config.MAX_CODE_SIZE_BYTES, {
          message: `code exceeds MAX_CODE_SIZE_BYTES (${config.MAX_CODE_SIZE_BYTES})`,
        }),
      stdin: z
        .string()
        .refine((s) => Buffer.byteLength(s, 'utf8') <= config.MAX_STDIN_BYTES, {
          message: `stdin exceeds MAX_STDIN_BYTES (${config.MAX_STDIN_BYTES})`,
        })
        .optional(),
      timeout_seconds: z
        .number()
        .int()
        .positive({ message: 'timeout_seconds must be a positive integer' })
        .optional()
        .transform((t) => {
          if (t === undefined) return config.DEFAULT_TIMEOUT_SECONDS;
          return Math.min(t, config.MAX_TIMEOUT_SECONDS);
        }),
      env_vars: envSchema,
      files: filesSchema,
    })
    .strict() as unknown as z.ZodType<ExecutionRequest>;
}

/** The result of validation: either a clean request or a list of field errors. */
export type ValidationResult =
  | { ok: true; value: ExecutionRequest }
  | { ok: false; errors: Array<{ field: string; message: string }> };

/** Validate and normalise an arbitrary input into an ExecutionRequest. */
export function validateExecutionRequest(input: unknown): ValidationResult {
  const parsed = buildSchema().safeParse(input);
  if (parsed.success) {
    return { ok: true, value: parsed.data };
  }
  return {
    ok: false,
    errors: parsed.error.issues.map((i) => ({
      field: i.path.join('.') || '(root)',
      message: i.message,
    })),
  };
}

/** Express middleware: validate `req.body` into a normalised request on `req.validated`. */
export function validateExecuteBody(req: Request, res: Response, next: NextFunction): void {
  const result = validateExecutionRequest(req.body);
  if (!result.ok) {
    problem(res, 400, 'Validation Error', 'The request body failed validation.', req.path, {
      code: 'validation-error',
      errors: result.errors,
    });
    return;
  }
  (req as Request & { validated: ExecutionRequest }).validated = result.value;
  next();
}

/** Read the validated request attached by `validateExecuteBody`. */
export function getValidated(req: Request): ExecutionRequest {
  return (req as Request & { validated: ExecutionRequest }).validated;
}
