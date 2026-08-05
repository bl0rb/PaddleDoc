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
