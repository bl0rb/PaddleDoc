'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ExternalLink } from 'lucide-react';
import { ApiError, apiJson } from '@/lib/api';
import { resolveApiBaseUrl } from '@/lib/api-base';
import type { ListResponse, PublicProvider, SetupStatusResponse } from '@/lib/auth-types';
import { Button } from '@/components/ui/button';
import { AuthField, AuthPageSpinner, AuthShell, FormError } from '@/components/auth/auth-card';

export default function LoginPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [providers, setProviders] = useState<PublicProvider[]>([]);

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  // First run must go through /setup instead; also load enabled OIDC providers.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const status = await apiJson<SetupStatusResponse>('/api/v1/auth/setup-status', {
          skipAuthRedirect: true,
        });
        if (cancelled) return;
        if (status.needs_setup) {
          router.replace('/setup');
          return;
        }
      } catch {
        // Backend unreachable — show the form; the POST will surface the error.
      }
      if (!cancelled) setChecking(false);

      try {
        const list = await apiJson<ListResponse<PublicProvider>>('/api/v1/auth/providers', {
          skipAuthRedirect: true,
        });
        if (!cancelled) setProviders(list.items);
      } catch {
        // Providers are optional — local login still works without them.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (pending) return;
    setError(null);
    setPending(true);

    try {
      await apiJson('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier, password }),
        skipAuthRedirect: true,
      });
      // Session cookie is set by the response — enter the app.
      window.location.assign('/');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('Invalid credentials');
      } else if (err instanceof ApiError && err.status === 429) {
        setError('Too many attempts — try again shortly');
      } else if (err instanceof ApiError) {
        setError(err.detail);
      } else {
        setError('Could not reach the server. Please try again.');
      }
      setPending(false);
    }
  }

  /** OIDC must be a full-page navigation so the IdP redirect chain works. */
  function loginWithProvider(slug: string) {
    window.location.assign(`${resolveApiBaseUrl()}/api/v1/auth/oidc/${slug}/authorize`);
  }

  if (checking) return <AuthPageSpinner />;

  return (
    <AuthShell title="Sign in" subtitle="Welcome back — sign in to your PaddleDoc workspace.">
      <form onSubmit={onSubmit} className="flex flex-col gap-4">
        <AuthField
          id="identifier"
          label="Username or email"
          name="username"
          autoComplete="username"
          required
          autoFocus
          value={identifier}
          onChange={(e) => setIdentifier(e.target.value)}
        />
        <AuthField
          id="password"
          label="Password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <FormError message={error} />

        <Button type="submit" disabled={pending} className="mt-1 w-full rounded-xl">
          {pending ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>

      {providers.length > 0 && (
        <div className="mt-6">
          <div className="flex items-center gap-3" aria-hidden="true">
            <div className="h-px flex-1 bg-slate-200" />
            <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
              or continue with
            </span>
            <div className="h-px flex-1 bg-slate-200" />
          </div>
          <div className="mt-4 flex flex-col gap-2">
            {providers.map((provider) => (
              <Button
                key={provider.slug}
                type="button"
                variant="outline"
                onClick={() => loginWithProvider(provider.slug)}
                className="w-full rounded-xl"
              >
                <ExternalLink className="h-4 w-4" aria-hidden="true" />
                {provider.display_name}
              </Button>
            ))}
          </div>
        </div>
      )}
    </AuthShell>
  );
}
