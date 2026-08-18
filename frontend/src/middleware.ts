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
function apiOrigin(): string | null {
  const raw = process.env.PADDLEDOC_PUBLIC_API_URL?.trim();
  if (!raw) return null;
  try {
    return new URL(raw).origin;
  } catch {
    return null;
  }
}

export function middleware(request: NextRequest): NextResponse {
  const response = NextResponse.next();
  const headers = response.headers;

  headers.set('Content-Security-Policy', contentSecurityPolicy(apiOrigin()));
  headers.set('X-Frame-Options', 'DENY');
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('Referrer-Policy', 'same-origin');
  headers.set('Permissions-Policy', 'geolocation=(), camera=(), microphone=()');

  // HSTS only over TLS: behind a plain-HTTP reverse proxy or in local dev the
  // header would pin browsers to a scheme that is not being served.
  const forwardedProto = request.headers.get('x-forwarded-proto');
  const isHttps = forwardedProto
    ? forwardedProto.split(',')[0].trim() === 'https'
    : request.nextUrl.protocol === 'https:';
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
