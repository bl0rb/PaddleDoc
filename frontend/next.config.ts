import type { NextConfig } from "next";

// Intentionally no `env` block here: Next.js's compiler replaces every
// `process.env.<KEY>` reference project-wide (client AND server bundles
// alike) with a frozen literal captured at `next build` time for any key
// listed in `env`. PADDLEDOC_PUBLIC_API_URL must stay readable live at
// container start (it's set via the Helm chart's `frontend.apiUrl` /
// docker-compose's environment block, never at image build time), so it
// must NOT be listed here — see src/app/runtime-env.js/route.ts and
// src/lib/api-base.ts for the live-read mechanism this enables.
const nextConfig: NextConfig = {};

export default nextConfig;
