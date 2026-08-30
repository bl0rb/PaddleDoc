import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

/**
 * Security response headers for every page the frontend serves.
 *
 * These live in middleware rather than next.config.ts's `headers()` because
 * the CSP has to name the backend origin in `connect-src`, and that origin is
 * only known at container start (PADDLEDOC_PUBLIC_API_URL, set by the Helm
 * chart's `frontend.apiUrl` or docker-compose). `headers()` is evaluated once
 * at `next build`, which would freeze whatever value the build machine had --
 * the same trap next.config.ts documents for the `env` block. Middleware runs
 * per request, so it reads the live value.
 */

// Next injects inline <style> and bootstrap <script> blocks, so 'unsafe-inline'
// cannot be dropped without moving the whole app to nonces. The rest of the
// policy still does real work: no foreign origins, no framing, no <base>
// rewriting, and form posts only back to us.
function contentSecurityPolicy(apiOrigin: string | null): string {
  const connect = ["'self'", apiOrigin].filter(Boolean).join(' ');
  return [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src ${connect}`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join('; ');
}

/** Origin of PADDLEDOC_PUBLIC_API_URL, or null when it is unset/unparsable. */
function configuredApiOrigin(): string | null {
  const raw = process.env.PADDLEDOC_PUBLIC_API_URL?.trim();
  if (!raw) return null;
  try {
    return new URL(raw).origin;
  } catch {
    return null;
  }
}

/**
 * Mirrors resolveApiBaseUrl()'s browser fallback in src/lib/api-base.ts: with
 * PADDLEDOC_PUBLIC_API_URL unset the client calls `<protocol>//<hostname>:8000`
 * derived from window.location. connect-src has to name that same origin or the
 * browser blocks every API call before it leaves the page -- which is what
 * docker-compose's empty default used to do. Keep the two in sync.
 *
 * new URL() doubles as validation here: a malformed Host header cannot inject
 * anything into the policy, it just yields null and leaves connect-src at 'self'.
 */
function requestApiOrigin(request: NextRequest, isHttps: boolean): string | null {
  const forwarded = request.headers.get('x-forwarded-host');
  const host = (forwarded ? forwarded.split(',')[0] : request.headers.get('host'))?.trim();
  if (!host) return null;
  // window.location.hostname carries no port, and IPv6 literals keep their brackets.
  const hostname = host.startsWith('[') ? host.slice(0, host.indexOf(']') + 1) : host.split(':')[0];
  if (!hostname) return null;
  try {
    return new URL(`${isHttps ? 'https:' : 'http:'}//${hostname}:8000`).origin;
  } catch {
    return null;
  }
}

export function middleware(request: NextRequest): NextResponse {
  const response = NextResponse.next();
  const headers = response.headers;

  const forwardedProto = request.headers.get('x-forwarded-proto');
  const isHttps = forwardedProto
    ? forwardedProto.split(',')[0].trim() === 'https'
    : request.nextUrl.protocol === 'https:';

  const apiOrigin = configuredApiOrigin() ?? requestApiOrigin(request, isHttps);
  headers.set('Content-Security-Policy', contentSecurityPolicy(apiOrigin));
  headers.set('X-Frame-Options', 'DENY');
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('Referrer-Policy', 'same-origin');
  headers.set('Permissions-Policy', 'geolocation=(), camera=(), microphone=()');

  // HSTS only over TLS: behind a plain-HTTP reverse proxy or in local dev the
  // header would pin browsers to a scheme that is not being served.
  if (isHttps) {
    headers.set('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
  }

  return response;
}

export const config = {
  // Everything except Next's own build output and the favicon; /runtime-env.js
  // is deliberately included so it carries the headers too.
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
