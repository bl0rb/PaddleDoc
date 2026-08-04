'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ApiError, apiJson } from '@/lib/api';
import type { SetupStatusResponse } from '@/lib/auth-types';
import { Button } from '@/components/ui/button';
import { AuthField, AuthPageSpinner, AuthShell, FormError } from '@/components/auth/auth-card';

export default function SetupPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  // If setup is already done, this page must not exist — go to /login.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const status = await apiJson<SetupStatusResponse>('/api/v1/auth/setup-status', {
          skipAuthRedirect: true,
        });
        if (cancelled) return;
        if (!status.needs_setup) {
          router.replace('/login');
          return;
        }
      } catch {
        // Backend unreachable — show the form; the POST will surface the error.
      }
      if (!cancelled) setChecking(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (pending) return;
    setError(null);

    if (password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }

    setPending(true);
    try {
      await apiJson('/api/v1/auth/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, email, password }),
        skipAuthRedirect: true,
      });
      // Session cookie is set by the response — enter the app.
      window.location.assign('/');
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Could not reach the server. Please try again.');
      setPending(false);
    }
  }

  if (checking) return <AuthPageSpinner />;

  return (
    <AuthShell
      title="Create the first admin account"
      subtitle="Set up the administrator that will manage users, teams, and sign-in providers."
    >
      <form onSubmit={onSubmit} className="flex flex-col gap-4">
        <AuthField
          id="username"
          label="Username"
          name="username"
          autoComplete="username"
          required
          autoFocus
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <AuthField
          id="email"
          label="Email"
          name="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <AuthField
          id="password"
          label="Password"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          minLength={8}
          placeholder="At least 8 characters"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <AuthField
          id="confirm-password"
          label="Confirm password"
          name="confirm-password"
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />

        <FormError message={error} />

        <Button type="submit" disabled={pending} className="mt-1 w-full rounded-xl">
          {pending ? 'Creating account…' : 'Create admin account'}
        </Button>
      </form>
    </AuthShell>
  );
}
