import { resolveApiBaseUrl } from '@/lib/api-base';

/** Error thrown by {@link apiJson} for non-ok responses. */
export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export interface ApiFetchInit extends RequestInit {
  /**
   * Suppress the automatic hard-redirect to /login on a 401 response.
   * Use for probes where a 401 is an expected outcome (e.g. GET /me,
   * failed login attempts).
   */
  skipAuthRedirect?: boolean;
}

/** Paths where a 401 must never trigger a redirect (would loop). */
const AUTH_PAGES = ['/login', '/setup'];

function onAuthPage(): boolean {
  if (typeof window === 'undefined') return false;
  const { pathname } = window.location;
  return AUTH_PAGES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/**
 * Fetch wrapper for all backend API calls.
 *
 * - Prefixes the runtime-resolved API base URL when `path` starts with '/'.
 * - Always sends credentials (httpOnly session cookie).
 * - On 401 (unless `skipAuthRedirect`, or already on /login or /setup),
 *   hard-redirects the browser to /login. The response is still returned.
 */
export async function apiFetch(path: string, init?: ApiFetchInit): Promise<Response> {
  const { skipAuthRedirect, ...rest } = init ?? {};
  const url = path.startsWith('/') ? `${resolveApiBaseUrl()}${path}` : path;

  const res = await fetch(url, {
    ...rest,
    credentials: 'include',
    headers: rest.headers,
  });

  if (res.status === 401 && !skipAuthRedirect && typeof window !== 'undefined' && !onAuthPage()) {
    window.location.assign('/login');
  }

  return res;
}

/**
 * Disambiguates a 401 from a password-capable job endpoint (which uses
 * skipAuthRedirect because 401 normally means "wrong/missing document
 * password"): probes GET /me, and if the session itself is dead,
 * hard-redirects to /login and returns true. Returns false when the
 * session is alive (the 401 really was about the document password)
 * or when the probe fails (can't tell — let the caller handle it).
 */
export async function redirectIfSessionExpired(): Promise<boolean> {
  try {
    const res = await apiFetch('/api/v1/auth/me', { skipAuthRedirect: true });
    if (res.status === 401 && typeof window !== 'undefined' && !onAuthPage()) {
      window.location.assign('/login');
      return true;
    }
  } catch {
    // Backend unreachable — treat as a document-password 401.
  }
  return false;
}

/**
 * Flattens a FastAPI request-validation `detail` array
 * (`[{loc, msg, type}, …]`) into a readable field-prefixed message.
 * Returns null when the shape does not match.
 */
function formatValidationDetail(detail: unknown): string | null {
  if (!Array.isArray(detail)) return null;
  const messages = detail
    .map((entry) => {
      if (typeof entry !== 'object' || entry === null) return null;
      const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
      if (typeof msg !== 'string') return null;
      const field = Array.isArray(loc)
        ? loc.filter((part): part is string => typeof part === 'string' && part !== 'body').join('.')
        : '';
      return field ? `${field}: ${msg}` : msg;
    })
    .filter((message): message is string => Boolean(message));
  return messages.length > 0 ? messages.join('; ') : null;
}

/**
 * {@link apiFetch} + ok-check + JSON parse.
 * Throws {@link ApiError} carrying the backend `detail` when available —
 * either the plain string or a flattened Pydantic validation-error array.
 */
export async function apiJson<T>(path: string, init?: ApiFetchInit): Promise<T> {
  const res = await apiFetch(path, init);

  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') {
        detail = body.detail;
      } else {
        detail = formatValidationDetail(body?.detail) ?? detail;
      }
    } catch {
      // Non-JSON error body — keep the generic message.
    }
    throw new ApiError(res.status, detail);
  }

  return (await res.json()) as T;
}

/** Filter value for GET /api/v1/auth/admin/worker-logs — acts as a floor (e.g. WARNING also returns ERROR/CRITICAL). */
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL';

/**
 * WorkerLogEntry from the backend (GET /api/v1/auth/admin/worker-logs).
 * `level` is not DB-enum-constrained on the backend — treat it as a plain
 * string when rendering (values outside {@link LogLevel} are possible).
 */
export interface WorkerLogEntry {
  id: string;
  created_at: string; // ISO 8601
  level: string;
  logger_name: string;
  worker_name: string;
  /** Null for log lines emitted outside a running Celery task. */
  task_id: string | null;
  task_name: string | null;
  /** Hard server-side truncation at 4000 chars. */
  message: string;
  /** Full traceback when the record carried exception info; hard truncation at 8000 chars. */
  exc_text: string | null;
}

/** GET /api/v1/auth/admin/worker-logs */
export interface WorkerLogsResponse {
  items: WorkerLogEntry[];
  /** Total rows matching the filters, ignoring limit/offset. */
  total: number;
}

/** One row of GET /api/v1/jobs/{id}/versions, newest-version-first. */
export interface JobVersionEntry {
  job_id: string;
  document_version: number;
  content_sha256: string | null;
  status: 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';
  created_at: string;
  uploaded_by: string | null;
  is_current: boolean;
}

/** GET /api/v1/jobs/{id}/versions */
export interface JobVersionsResponse {
  items: JobVersionEntry[];
}

/** Row of GET /api/v1/auth/tokens — the full token value is never re-shown after creation. */
export interface ApiTokenSummary {
  id: string;
  name: string;
  token_prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
}

/** GET /api/v1/auth/tokens */
export interface ApiTokenListResponse {
  items: ApiTokenSummary[];
}

/** POST /api/v1/auth/tokens — the only response that carries the full token. */
export interface ApiTokenCreateResponse {
  id: string;
  name: string;
  token: string;
  token_prefix: string;
  created_at: string;
  expires_at: string | null;
}

/* -------------------------------------------------------------------------
 * Benchmarks — run the same document through multiple VL connections (and
 * optionally a baseline OCR profile) and compare output quality. Field
 * names mirror the backend benchmark schema exactly.
 * ---------------------------------------------------------------------- */

/** GET /api/v1/vl-connections — enabled connections only, no key material. */
export interface VLConnection {
  id: string;
  name: string;
  model: string;
}

/** GET /api/v1/vl-connections */
export interface VLConnectionListResponse {
  items: VLConnection[];
}

/** Computed live from child job statuses — see GET /api/v1/benchmarks for the derivation rule. */
export type BenchmarkRunStatus = 'pending' | 'running' | 'completed' | 'failed';

export type BenchmarkVariantStatus = 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';

/** 'vl' = a VL connection variant, 'ocr' = the optional baseline OCR-profile variant. */
export type BenchmarkVariantKind = 'vl' | 'ocr';

export interface BenchmarkOwner {
  id: string;
  username: string;
}

/** Row of GET /api/v1/benchmarks. */
export interface BenchmarkRun {
  id: string;
  original_filename: string;
  status: BenchmarkRunStatus;
  variant_count: number;
  created_at: string;
  updated_at: string;
  owner: BenchmarkOwner | null;
}

/** GET /api/v1/benchmarks */
export interface BenchmarkRunListResponse {
  items: BenchmarkRun[];
}

/** Lightweight, status-only variant entry — safe to poll (no markdown). */
export interface BenchmarkVariantSummary {
  job_id: string;
  label: string;
  kind: BenchmarkVariantKind;
  status: BenchmarkVariantStatus;
  error_message: string | null;
}

/** GET /api/v1/benchmarks/{id} and the response of POST /api/v1/benchmarks. */
export interface BenchmarkRunDetail extends BenchmarkRun {
  content_sha256: string;
  variants: BenchmarkVariantSummary[];
}

/** Variant entry within GET /api/v1/benchmarks/{id}/report — heavier than {@link BenchmarkVariantSummary}, carries metrics. */
export interface BenchmarkReportVariant {
  job_id: string;
  label: string;
  kind: BenchmarkVariantKind;
  status: BenchmarkVariantStatus;
  duration_seconds: number | null;
  page_count: number | null;
  output_chars: number | null;
  quality_grade: 'A' | 'B' | 'C' | null;
  used_fallback: boolean | null;
  error: string | null;
}

export interface BenchmarkReportSummary {
  fastest_variant_job_id: string | null;
  highest_quality_variant_job_id: string | null;
}

/** GET /api/v1/benchmarks/{id}/report — always 200; populated fields grow as variants finish, check `all_terminal`. */
export interface BenchmarkReport {
  id: string;
  original_filename: string;
  status: BenchmarkRunStatus;
  all_terminal: boolean;
  created_at: string;
  variants: BenchmarkReportVariant[];
  summary: BenchmarkReportSummary;
}

/** DELETE /api/v1/benchmarks/{id} */
export interface BenchmarkDeleteResponse {
  id: string;
  deleted_jobs: number;
}

/** Server caps: 0..6 vl_connection_ids, and 2..7 total variants (vl_connection_ids + optional profile_id). */
export const MAX_VL_CONNECTIONS = 6;
export const MIN_BENCHMARK_VARIANTS = 2;
export const MAX_BENCHMARK_VARIANTS = 7;

const BENCHMARK_ACTIVE_STATUSES: BenchmarkRunStatus[] = ['pending', 'running'];

export function isBenchmarkRunActive(status: BenchmarkRunStatus): boolean {
  return BENCHMARK_ACTIVE_STATUSES.includes(status);
}

/** Status chip classes — restricted to the app's emerald/slate/amber/red palette. */
export const benchmarkStatusChip: Record<BenchmarkRunStatus, string> = {
  pending: 'bg-slate-100 text-slate-700',
  running: 'bg-amber-100 text-amber-800',
  completed: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-700',
};

export const benchmarkVariantStatusChip: Record<BenchmarkVariantStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-amber-100 text-amber-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-100 text-red-700',
};

/** quality_gate.py never emits D/F — A/B/C is exhaustive. */
export const qualityGradeChip: Record<'A' | 'B' | 'C', string> = {
  A: 'bg-emerald-50 text-emerald-700',
  B: 'bg-amber-50 text-amber-700',
  C: 'bg-red-50 text-red-700',
};
