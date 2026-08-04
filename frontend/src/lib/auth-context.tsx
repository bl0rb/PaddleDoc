'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { useRouter } from 'next/navigation';
import { apiFetch } from '@/lib/api';
import type { AuthUser, SetupStatusResponse } from '@/lib/auth-types';

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Pages that must never be redirected away from (redirect loop otherwise). */
function onAuthPage(): boolean {
  if (typeof window === 'undefined') return true;
  const { pathname } = window.location;
  return ['/login', '/setup'].some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

async function fetchMe(): Promise<AuthUser | null> {
  const res = await apiFetch('/api/v1/auth/me', { skipAuthRedirect: true });
  if (!res.ok) return null;
  return (await res.json()) as AuthUser;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  /** Re-fetch the current user (e.g. after a login/setup POST). */
  const refresh = useCallback(async () => {
    try {
      setUser(await fetchMe());
    } catch {
      setUser(null);
    }
  }, []);

  const logout = useCallback(async () => {
    // The session cookie is httpOnly, so only the backend can clear it —
    // never pretend to be logged out while the session may still be valid.
    let ok = false;
    try {
      const res = await apiFetch('/api/v1/auth/logout', { method: 'POST', skipAuthRedirect: true });
      // 401 means the session is already gone — that is a successful logout.
      ok = res.ok || res.status === 401;
    } catch {
      ok = false;
    }
    if (ok) {
      window.location.assign('/login');
    } else {
      window.alert('Logout failed — the server could not be reached. You are still signed in.');
    }
  }, []);

  // Initial session check: /me, then setup-status routing on 401.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      // When we redirect away, loading stays true so ProtectedShell keeps its
      // spinner up — otherwise the protected page mounts for a frame and fires
      // doomed API requests before the /login route commits.
      let redirected = false;
      try {
        const res = await apiFetch('/api/v1/auth/me', { skipAuthRedirect: true });

        if (res.ok) {
          const me = (await res.json()) as AuthUser;
          if (!cancelled) setUser(me);
        } else {
          if (!cancelled) setUser(null);

          if (res.status === 401 && !onAuthPage()) {
            let needsSetup = false;
            try {
              const status = await apiFetch('/api/v1/auth/setup-status', {
                skipAuthRedirect: true,
              });
              if (status.ok) {
                needsSetup = ((await status.json()) as SetupStatusResponse).needs_setup;
              }
            } catch {
              // Backend unreachable — fall through to /login.
            }
            if (!cancelled) {
              redirected = true;
              router.replace(needsSetup ? '/setup' : '/login');
            }
          }
        }
      } catch {
        if (!cancelled) setUser(null);
      }
      if (!cancelled && !redirected) setLoading(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [router]);

  const value = useMemo(
    () => ({ user, loading, refresh, logout }),
    [user, loading, refresh, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an <AuthProvider>');
  }
  return ctx;
}
