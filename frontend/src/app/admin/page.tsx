'use client';

import { useState } from 'react';
import { KeyRound, ShieldAlert, Terminal, Users, UsersRound } from 'lucide-react';

import { useAuth } from '@/lib/auth-context';
import { UsersTab } from '@/components/admin/users-tab';
import { TeamsTab } from '@/components/admin/teams-tab';
import { ProvidersTab } from '@/components/admin/providers-tab';
import { LogsTab } from '@/components/admin/logs-tab';

type TabId = 'users' | 'teams' | 'providers' | 'logs';

const tabs: { id: TabId; label: string; icon: typeof Users }[] = [
  { id: 'users', label: 'Users', icon: Users },
  { id: 'teams', label: 'Teams', icon: UsersRound },
  { id: 'providers', label: 'Identity Providers', icon: KeyRound },
  { id: 'logs', label: 'Logs', icon: Terminal },
];

export default function AdminPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<TabId>('users');

  if (!user || user.role !== 'admin') {
    return (
      <main className="min-h-screen">
        <div className="mx-auto flex w-full max-w-7xl flex-col items-center px-4 py-24 sm:px-6 lg:px-8">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100">
            <ShieldAlert className="h-6 w-6 text-slate-400" />
          </div>
          <h1 className="mt-4 text-lg font-semibold text-slate-950">Admin privileges required</h1>
          <p className="mt-1 max-w-md text-center text-sm text-slate-500">
            This area is restricted to administrators. Ask an admin to grant you the admin role if
            you need access.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6">
          <h1 className="text-2xl font-semibold text-slate-950">Administration</h1>
          <p className="mt-1 text-sm text-slate-500">
            Manage users, teams, single sign-on identity providers, and worker logs.
          </p>
        </header>

        <div
          role="tablist"
          aria-label="Admin sections"
          className="mb-6 inline-flex flex-wrap gap-1 rounded-2xl border border-slate-200 bg-white p-1 shadow-sm"
        >
          {tabs.map(({ id, label, icon: Icon }) => {
            const active = tab === id;
            return (
              <button
                key={id}
                role="tab"
                aria-selected={active}
                onClick={() => setTab(id)}
                className={`flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition ${
                  active
                    ? 'bg-emerald-50 text-emerald-800'
                    : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                }`}
              >
                <Icon className={`h-4 w-4 ${active ? 'text-emerald-700' : 'text-slate-400'}`} />
                {label}
              </button>
            );
          })}
        </div>

        {tab === 'users' && <UsersTab />}
        {tab === 'teams' && <TeamsTab />}
        {tab === 'providers' && <ProvidersTab />}
        {tab === 'logs' && <LogsTab />}
      </div>
    </main>
  );
}
