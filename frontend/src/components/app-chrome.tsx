'use client';

import { usePathname } from 'next/navigation';
import { AuthProvider, useAuth } from '@/lib/auth-context';
import { SidebarNav } from '@/components/sidebar-nav';

const AUTH_PAGES = ['/login', '/setup'];

function isAuthPage(pathname: string): boolean {
  return AUTH_PAGES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** Sidebar + auth-gated content for protected routes. */
function ProtectedShell({ children }: { children: React.ReactNode }) {
  const { loading } = useAuth();

  if (loading) {
    return (
      <div className="flex min-h-screen flex-1 items-center justify-center">
        <div
          role="status"
          aria-label="Loading"
          className="h-8 w-8 animate-spin rounded-full border-2 border-slate-200 border-t-emerald-600"
        />
      </div>
    );
  }

  return (
    <>
      <SidebarNav />
      <div className="lg:pl-64">{children}</div>
    </>
  );
}

/**
 * Client-side app shell. Auth pages (/login, /setup) render bare —
 * no sidebar, no loading gate; everything else gets the sidebar and
 * waits for the initial session check before showing content.
 * AuthProvider wraps both branches (it skips redirects on auth pages
 * itself), so useAuth() works everywhere.
 */
export function AppChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <AuthProvider>
      {isAuthPage(pathname) ? children : <ProtectedShell>{children}</ProtectedShell>}
    </AuthProvider>
  );
}
