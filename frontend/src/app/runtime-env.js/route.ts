// Serves the frontend's runtime configuration as a plain JavaScript file.
//
// Next.js statically inlines `process.env.*` values referenced via
// `next.config.ts`'s `env` block at BUILD time, so setting the env var when
// the container starts (e.g. via the Helm chart's `frontend.apiUrl`) has no
// effect on the already-built browser bundle. Route handlers, in contrast,
// execute per-request under `next start`, so reading `process.env` here
// picks up the value that was set at container start.
//
// The root layout loads this route via a synchronous <script src="/runtime-env.js">
// in <head>, which sets `window.__PADDLEDOC_ENV__` before any client bundle
// runs. `src/lib/api-base.ts` then prefers that value over the build-time env.

// Force dynamic (per-request) execution so this route is never statically
// optimized/prerendered at build time with a baked-in value.
export const dynamic = 'force-dynamic';

export async function GET() {
  const apiUrl = process.env.PADDLEDOC_PUBLIC_API_URL?.trim() || null;

  const body = `window.__PADDLEDOC_ENV__ = ${JSON.stringify({ apiUrl })};`;

  return new Response(body, {
    status: 200,
    headers: {
      'Content-Type': 'application/javascript',
      'Cache-Control': 'no-store',
    },
  });
}
