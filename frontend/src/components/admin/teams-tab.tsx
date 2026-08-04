'use client';

import { useState } from 'react';
import { Check, LoaderCircle, Pencil, Plus, Trash2, X } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { apiJson } from '@/lib/api';
import type { Team, TeamRequest } from '@/lib/auth-types';
import {
  apiSend,
  ConfirmDialog,
  ErrorNotice,
  errorMessage,
  inputClass,
  LoadingState,
  SectionCard,
  useAdminList,
} from '@/components/admin/admin-shared';

export function TeamsTab() {
  const teams = useAdminList<Team>('/api/v1/auth/admin/teams');

  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);

  const [deleting, setDeleting] = useState<Team | null>(null);

  async function createTeam(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      const body: TeamRequest = { name: newName.trim() };
      await apiJson<Team>('/api/v1/auth/admin/teams', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setNewName('');
      await teams.reload();
    } catch (err) {
      setCreateError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  }

  function startRename(team: Team) {
    setRenamingId(team.id);
    setRenameValue(team.name);
    setRenameError(null);
  }

  async function saveRename(id: string) {
    setRenameBusy(true);
    setRenameError(null);
    try {
      const body: TeamRequest = { name: renameValue.trim() };
      await apiJson<Team>(`/api/v1/auth/admin/teams/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setRenamingId(null);
      await teams.reload();
    } catch (err) {
      setRenameError(errorMessage(err));
    } finally {
      setRenameBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <SectionCard title="Create team" description="Teams scope job visibility for their members.">
        <form onSubmit={createTeam} className="flex flex-wrap items-center gap-3">
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Team name"
            required
            className={`${inputClass} mt-0 w-full max-w-xs`}
          />
          <Button type="submit" size="sm" className="h-[38px]" disabled={creating || !newName.trim()}>
            {creating ? (
              <LoaderCircle className="h-4 w-4 animate-spin" />
            ) : (
              <Plus className="h-4 w-4" />
            )}
            Create team
          </Button>
        </form>
        {createError && <p className="mt-3 text-sm text-red-700">{createError}</p>}
      </SectionCard>

      <SectionCard title="Teams" description="Rename or remove existing teams.">
        <ErrorNotice message={teams.error} />
        {teams.loading ? (
          <LoadingState label="Loading teams…" />
        ) : teams.items.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">
            No teams yet. Create one above to start grouping users.
          </p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {teams.items.map((team) => (
              <li key={team.id} className="flex items-center justify-between gap-3 py-3">
                {renamingId === team.id ? (
                  <div className="flex flex-1 flex-wrap items-center gap-2">
                    <input
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      className={`${inputClass} mt-0 w-full max-w-xs`}
                      autoFocus
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          void saveRename(team.id);
                        }
                        if (e.key === 'Escape') setRenamingId(null);
                      }}
                    />
                    <button
                      onClick={() => saveRename(team.id)}
                      disabled={renameBusy || !renameValue.trim()}
                      aria-label="Save team name"
                      title="Save"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-emerald-600 transition hover:bg-emerald-50 disabled:opacity-50"
                    >
                      {renameBusy ? (
                        <LoaderCircle className="h-4 w-4 animate-spin" />
                      ) : (
                        <Check className="h-4 w-4" />
                      )}
                    </button>
                    <button
                      onClick={() => setRenamingId(null)}
                      disabled={renameBusy}
                      aria-label="Cancel rename"
                      title="Cancel"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                    >
                      <X className="h-4 w-4" />
                    </button>
                    {renameError && <p className="w-full text-sm text-red-700">{renameError}</p>}
                  </div>
                ) : (
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-950">{team.name}</p>
                    <p className="text-xs text-slate-400">
                      Created {new Date(team.created_at).toLocaleDateString()}
                    </p>
                  </div>
                )}
                {renamingId !== team.id && (
                  <div className="flex flex-shrink-0 gap-1">
                    <button
                      onClick={() => startRename(team)}
                      aria-label={`Rename ${team.name}`}
                      title="Rename"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => setDeleting(team)}
                      aria-label={`Delete ${team.name}`}
                      title="Delete"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      {deleting && (
        <ConfirmDialog
          title="Delete team"
          body={
            <p>
              Delete <span className="font-semibold text-slate-950">{deleting.name}</span>? Members
              keep working normally — they only lose the team scope.
            </p>
          }
          confirmLabel="Delete team"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await apiSend(`/api/v1/auth/admin/teams/${deleting.id}`, { method: 'DELETE' });
            setDeleting(null);
            await teams.reload();
          }}
        />
      )}
    </div>
  );
}
