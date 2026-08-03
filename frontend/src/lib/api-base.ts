declare global {
  interface Window {
    /**
     * Runtime-injected config, set by a synchronous <script src="/runtime-env.js">
     * in the root layout's <head> (see src/app/runtime-env.js/route.ts) before
     * any client bundle executes. Lets the backend API URL be configured at
     * container start (e.g. via the Helm chart's `frontend.apiUrl`) instead of
     * being statically inlined at build time.
     */
    __PADDLEDOC_ENV__?: {
      apiUrl?: string | null;
    };
  }
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '');
}

export function resolveApiBaseUrl(): string {
  // Browser: prefer the runtime-injected value (read live from the
  // server's current env via /runtime-env.js on every page load), then
  // fall back to the window.location-based default.
  //
  // There is deliberately no "build-time inlined" fallback here: that
  // would require next.config.ts to list PADDLEDOC_PUBLIC_API_URL in its
  // `env` block, which makes Next.js replace every process.env reference
  // to it (client AND server bundles alike) with a frozen literal captured
  // at `next build` time — defeating the live /runtime-env.js mechanism
  // this file depends on. See next.config.ts for details.
  if (typeof window !== 'undefined') {
    const runtimeConfigured = window.__PADDLEDOC_ENV__?.apiUrl?.trim();
    if (runtimeConfigured) {
      return trimTrailingSlash(runtimeConfigured);
    }

    const protocol = window.location.protocol === 'https:' ? 'https:' : 'http:';
    const hostname = window.location.hostname;
    return `${protocol}//${hostname}:8000`;
  }

  // Server: read process.env live (safe as long as next.config.ts never
  // inlines this var — see above), then fall back to the existing default.
  const configured = process.env.PADDLEDOC_PUBLIC_API_URL?.trim();
  if (configured) {
    return trimTrailingSlash(configured);
  }

  return 'http://localhost:8000';
}

export const API_BASE_URL = resolveApiBaseUrl();
