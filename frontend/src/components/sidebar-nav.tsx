'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Home, Menu, X, Cpu, FolderOpen, Gauge, Mail, Settings, Shield, LogOut } from 'lucide-react';
import { useAuth } from '@/lib/auth-context';

const links = [
  { href: '/', label: 'Home', icon: Home },
  { href: '/processing', label: 'Processing', icon: Cpu, description: 'Upload and process documents' },
  { href: '/jobs', label: 'Jobs', icon: FolderOpen, description: 'View processing jobs' },
  { href: '/mail', label: 'Mail', icon: Mail, description: 'API-ingested messages' },
  { href: '/benchmark', label: 'Benchmark', icon: Gauge },
];

export function SidebarNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const drawerRef = useRef<HTMLDivElement>(null);
  const { user, logout } = useAuth();

  // Close on outside click
  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      if (open && drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  return (
    <>
      {/* Burger button — fixed top-left */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? 'Close navigation' : 'Open navigation'}
        className="fixed left-4 top-4 z-50 flex h-9 w-9 items-center justify-center rounded-xl border border-slate-200 bg-white shadow-md transition hover:bg-slate-50 lg:hidden"
      >
        {open ? <X className="h-4 w-4 text-slate-700" /> : <Menu className="h-4 w-4 text-slate-700" />}
      </button>

      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-sm lg:hidden"
          aria-hidden="true"
        />
      )}

      {/* Drawer */}
      <div
        ref={drawerRef}
        className={`fixed left-0 top-0 z-40 flex h-full w-64 flex-col bg-white shadow-2xl transition-transform duration-200 lg:translate-x-0 lg:border-r lg:border-slate-100 lg:shadow-none ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-center gap-3 border-b border-slate-100 px-5 py-4">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-600">
            <Cpu className="h-4 w-4 text-white" />
          </div>
          <span className="text-base font-semibold text-slate-950">PaddleDoc</span>
        </div>

        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 py-4">
          {links.map(({ href, label, icon: Icon, description }) => {
            const active = pathname === href || (href !== '/' && pathname.startsWith(href));
            return (
              <Link
                key={href}
                href={href}
                onClick={() => setOpen(false)}
                title={description}
                className={`flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                  active
                    ? 'bg-emerald-50 text-emerald-800'
                    : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                }`}
              >
                <Icon className={`h-4 w-4 flex-shrink-0 ${active ? 'text-emerald-700' : 'text-slate-400'}`} />
                {label}
              </Link>
            );
          })}
        </nav>

        <div className="border-t border-slate-100 px-3 py-3">
          {user ? (
            <>
              <Link
                href="/settings"
                onClick={() => setOpen(false)}
                className={`mb-2 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                  pathname === '/settings' || pathname.startsWith('/settings/')
                    ? 'bg-emerald-50 text-emerald-800'
                    : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                }`}
              >
                <Settings
                  className={`h-4 w-4 flex-shrink-0 ${
                    pathname === '/settings' || pathname.startsWith('/settings/')
                      ? 'text-emerald-700'
                      : 'text-slate-400'
                  }`}
                />
                Settings
              </Link>
              {user.role === 'admin' && (
                <Link
                  href="/admin"
                  onClick={() => setOpen(false)}
                  className={`mb-2 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                    pathname === '/admin' || pathname.startsWith('/admin/')
                      ? 'bg-emerald-50 text-emerald-800'
                      : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                  }`}
                >
                  <Shield
                    className={`h-4 w-4 flex-shrink-0 ${
                      pathname === '/admin' || pathname.startsWith('/admin/')
                        ? 'text-emerald-700'
                        : 'text-slate-400'
                    }`}
                  />
                  Admin
                </Link>
              )}
              <div className="flex items-center justify-between gap-2 rounded-xl px-3 py-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-950">{user.username}</p>
                  <span
                    className={`mt-0.5 inline-block rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                      user.role === 'admin'
                        ? 'bg-emerald-50 text-emerald-700'
                        : 'bg-slate-100 text-slate-600'
                    }`}
                  >
                    {user.role}
                  </span>
                </div>
                <button
                  onClick={() => logout()}
                  aria-label="Log out"
                  title="Log out"
                  className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                >
                  <LogOut className="h-4 w-4" />
                </button>
              </div>
            </>
          ) : (
            <p className="px-3 py-1 text-xs text-slate-400">PaddleOCR document pipeline</p>
          )}
        </div>
      </div>
    </>
  );
}
