/**
 * Public-facing registry of supported language runtimes.
 *
 * This is the API server's view of the runtimes (used for validation and GET /v1/languages).
 * The authoritative *execution* registry — image digests, entrypoints, seccomp profiles — lives
 * in the worker (`worker/sandbox/image_registry.py`). The two are kept in sync via the shared
 * `Language` union and the operational checklist in docs/ADDING_LANGUAGE.md.
 */
import type { Language, RuntimeInfo } from './types/index.js';

const RUNTIMES: Record<Language, RuntimeInfo> = {
  python: {
    id: 'python',
    name: 'Python',
    version: '3.12',
    default_timeout_seconds: 10,
    memory_mb: 256,
  },
  javascript: {
    id: 'javascript',
    name: 'JavaScript (Node.js)',
    version: '20',
    default_timeout_seconds: 10,
    memory_mb: 256,
  },
  typescript: {
    id: 'typescript',
    name: 'TypeScript',
    version: '5.7',
    default_timeout_seconds: 15,
    memory_mb: 320,
  },
  java: {
    id: 'java',
    name: 'Java',
    version: '21',
    default_timeout_seconds: 20,
    memory_mb: 512,
  },
  go: {
    id: 'go',
    name: 'Go',
    version: '1.22',
    default_timeout_seconds: 20,
    memory_mb: 384,
  },
  ruby: {
    id: 'ruby',
    name: 'Ruby',
    version: '3.3',
    default_timeout_seconds: 10,
    memory_mb: 256,
  },
  rust: {
    id: 'rust',
    name: 'Rust',
    version: '1.83',
    default_timeout_seconds: 25,
    memory_mb: 512,
  },
  bash: {
    id: 'bash',
    name: 'Bash',
    version: '5.2',
    default_timeout_seconds: 10,
    memory_mb: 128,
  },
};

/** Returns true if the given string is a supported language id. */
export function isSupportedLanguage(value: string): value is Language {
  return Object.prototype.hasOwnProperty.call(RUNTIMES, value);
}

/** Returns metadata for a single runtime, or undefined if unknown. */
export function getRuntimeInfo(language: Language): RuntimeInfo {
  return RUNTIMES[language];
}

/** Returns metadata for all supported runtimes, sorted by id. */
export function listRuntimes(): RuntimeInfo[] {
  return Object.values(RUNTIMES).sort((a, b) => a.id.localeCompare(b.id));
}
