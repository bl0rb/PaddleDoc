'use client';

import { useState } from 'react';
import { Briefcase, LoaderCircle, Pencil, Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { apiJson } from '@/lib/api';
import type {
  AdminUserCreateRequest,
  AdminUserUpdateRequest,
  AuthUser,
  ClaimOwnerlessRequest,
  ClaimOwnerlessResponse,
  Team,
  UserRole,
} from '@/lib/auth-types';
import {
  apiSend,
  Badge,
  ConfirmDialog,
  ErrorNotice,
  errorMessage,
  Field,
  inputClass,
  LoadingState,
  Modal,
  SectionCard,
  Toggle,
  useAdminList,
} from '@/components/admin/admin-shared';

const NO_TEAM = '';

export function UsersTab() {
  const users = useAdminList<AuthUser>('/api/v1/auth/admin/users');
  const teams = useAdminList<Team>('/api/v1/auth/admin/teams');

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AuthUser | null>(null);
  const [deleting, setDeleting] = useState<AuthUser | null>(null);

  const teamName = (id: string | null) =>
    id === null ? '—' : (teams.items.find((t) => t.id === id)?.name ?? `#${id}`);

  return (
    <div className="space-y-6">
      <SectionCard
        title="Users"
        description="Manage accounts, roles, and team membership."
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" />
            Create user
          </Button>
        }
      >
        <ErrorNotice message={users.error} />
        {users.loading ? (
          <LoadingState label="Loading users…" />
        ) : users.items.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">No users yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full table-auto text-left text-xs sm:text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 pr-3 font-medium">Username</th>
                  <th className="hidden pb-2 pr-3 font-medium md:table-cell">Email</th>
                  <th className="pb-2 pr-3 font-medium">Role</th>
                  <th className="hidden pb-2 pr-3 font-medium lg:table-cell">Team</th>
                  <th className="pb-2 pr-3 font-medium">Active</th>
                  <th className="hidden pb-2 pr-3 font-medium sm:table-cell">SSO</th>
                  <th className="hidden pb-2 pr-3 font-medium xl:table-cell">Created</th>
                  <th className="pb-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {users.items.map((u) => (
                  <tr key={u.id} className="border-t border-slate-100">
                    <td className="py-3 pr-3 font-medium text-slate-950">{u.username}</td>
                    <td className="hidden py-3 pr-3 text-slate-700 md:table-cell">{u.email}</td>
                    <td className="py-3 pr-3">
                      <Badge tone={u.role === 'admin' ? 'emerald' : 'slate'}>{u.role}</Badge>
                    </td>
                    <td className="hidden py-3 pr-3 text-slate-700 lg:table-cell">
                      {teamName(u.team_id)}
                    </td>
                    <td className="py-3 pr-3">
                      <Badge tone={u.is_active ? 'emerald' : 'red'}>
                        {u.is_active ? 'Active' : 'Inactive'}
                      </Badge>
                    </td>
                    <td className="hidden py-3 pr-3 sm:table-cell">
                      {u.oidc_provider_id !== null ? (
                        <Badge tone="emerald">SSO</Badge>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                    <td className="hidden py-3 pr-3 text-slate-700 xl:table-cell">
                      {new Date(u.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3">
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => setEditing(u)}
                          aria-label={`Edit ${u.username}`}
                          title="Edit"
                          className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                        >
                          <Pencil className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => setDeleting(u)}
                          aria-label={`Delete ${u.username}`}
                          title="Delete"
                          className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      <ClaimOwnerlessCard users={users.items} />

      {creating && (
        <CreateUserModal
          teams={teams.items}
          onClose={() => setCreating(false)}
          onSaved={async () => {
            setCreating(false);
            await users.reload();
          }}
        />
      )}

      {editing && (
        <EditUserModal
          user={editing}
          teams={teams.items}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await users.reload();
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete user"
          body={
            <p>
              Delete <span className="font-semibold text-slate-950">{deleting.username}</span>?
              This cannot be undone.
            </p>
          }
          confirmLabel="Delete user"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await apiSend(`/api/v1/auth/admin/users/${deleting.id}`, { method: 'DELETE' });
            setDeleting(null);
            await users.reload();
          }}
        />
      )}
    </div>
  );
}

function CreateUserModal({
  teams,
  onClose,
  onSaved,
}: {
  teams: Team[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<UserRole>('user');
  const [teamId, setTeamId] = useState<string>(NO_TEAM);
  const [isActive, setIsActive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body: AdminUserCreateRequest = {
      username: username.trim(),
      email: email.trim(),
      role,
      is_active: isActive,
      ...(password ? { password } : {}),
      ...(teamId !== NO_TEAM ? { team_id: teamId } : {}),
    };
    try {
      await apiJson<AuthUser>('/api/v1/auth/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      await onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal title="Create user" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Username">
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className={inputClass}
            required
            autoFocus
          />
        </Field>
        <Field label="Email">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputClass}
            required
          />
        </Field>
        <Field label="Password" hint="Leave empty for SSO-only account.">
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
            minLength={8}
            autoComplete="new-password"
          />
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Role">
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
              className={inputClass}
            >
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </Field>
          <Field label="Team">
            <select
              value={teamId}
              onChange={(e) => setTeamId(e.target.value)}
              className={inputClass}
            >
              <option value={NO_TEAM}>No team</option>
              {teams.map((t) => (
                <option key={t.id} value={String(t.id)}>
                  {t.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Toggle checked={isActive} onChange={setIsActive} label="Active" />
        <ErrorNotice message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}
            Create user
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EditUserModal({
  user,
  teams,
  onClose,
  onSaved,
}: {
  user: AuthUser;
  teams: Team[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [email, setEmail] = useState(user.email);
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<UserRole>(user.role);
  const [teamId, setTeamId] = useState<string>(user.team_id === null ? NO_TEAM : String(user.team_id));
  const [isActive, setIsActive] = useState(user.is_active);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body: AdminUserUpdateRequest = {
      email: email.trim(),
      role,
      is_active: isActive,
      ...(password ? { password } : {}),
      ...(teamId === NO_TEAM ? { clear_team: true } : { team_id: teamId }),
    };
    try {
      await apiJson<AuthUser>(`/api/v1/auth/admin/users/${user.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      await onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal title={`Edit ${user.username}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Email">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputClass}
            required
          />
        </Field>
        <Field label="New password" hint="Leave empty to keep the current password.">
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
            minLength={8}
            autoComplete="new-password"
          />
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Role">
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
              className={inputClass}
            >
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </Field>
          <Field label="Team">
            <select
              value={teamId}
              onChange={(e) => setTeamId(e.target.value)}
              className={inputClass}
            >
              <option value={NO_TEAM}>No team</option>
              {teams.map((t) => (
                <option key={t.id} value={String(t.id)}>
                  {t.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Toggle checked={isActive} onChange={setIsActive} label="Active" />
        <ErrorNotice message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}
            Save changes
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ClaimOwnerlessCard({ users }: { users: AuthUser[] }) {
  const [ownerId, setOwnerId] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function claim() {
    setBusy(true);
    setResult(null);
    setError(null);
    try {
      const res = await apiJson<ClaimOwnerlessResponse>('/api/v1/auth/admin/jobs/claim-ownerless', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ owner_id: ownerId } satisfies ClaimOwnerlessRequest),
      });
      setResult(`${res.claimed} jobs assigned`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SectionCard
      title="Assign ownerless jobs"
      description="Jobs created before authentication was enabled have no owner. Assign all of them to a user so they show up in that user's job list."
    >
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-full max-w-xs">
          <Field label="Assign to">
            <select
              value={ownerId}
              onChange={(e) => setOwnerId(e.target.value)}
              className={inputClass}
            >
              <option value="">Select a user…</option>
              {users.map((u) => (
                <option key={u.id} value={String(u.id)}>
                  {u.username}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Button size="sm" className="h-[38px]" onClick={claim} disabled={busy || ownerId === ''}>
          {busy ? (
            <LoaderCircle className="h-4 w-4 animate-spin" />
          ) : (
            <Briefcase className="h-4 w-4" />
          )}
          Assign jobs
        </Button>
      </div>
      {result && <p className="mt-3 text-sm font-medium text-emerald-700">{result}</p>}
      {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
    </SectionCard>
  );
}
