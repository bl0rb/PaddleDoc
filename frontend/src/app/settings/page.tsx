'use client';

import { useEffect, useState } from 'react';
import { Check, Copy, Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  ApiError,
  apiJson,
  type ApiTokenCreateResponse,
  type ApiTokenListResponse,
  type ApiTokenSummary,
} from '@/lib/api';
import {
  apiSend,
  ConfirmDialog,
  ErrorNotice,
  errorMessage,
  Field,
  inputClass,
  LoadingState,
  SectionCard,
} from '@/components/admin/admin-shared';

const TOKENS_PATH = '/api/v1/auth/tokens';

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—';
}

export default function SettingsPage() {
  const [tokens, setTokens] = useState<ApiTokenSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState('');
  const [expiresInDays, setExpiresInDays] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<ApiTokenCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const [revoking, setRevoking] = useState<ApiTokenSummary | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiJson<ApiTokenListResponse>(TOKENS_PATH);
      setTokens(data.items);
      setUnavailable(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUnavailable(true);
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setLoading(false);
    }
  };

  // Initial load only — mirrors useAdminList's mount effect (admin-shared.tsx):
  // `load` above is for post-mutation refreshes triggered from event
  // handlers, not called here directly, since `loading` already starts
  // `true` and the effect body's first statement must be the `await` (not a
  // synchronous setState) for react-hooks/set-state-in-effect.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiJson<ApiTokenListResponse>(TOKENS_PATH);
        if (cancelled) return;
        setTokens(data.items);
        setUnavailable(false);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setUnavailable(true);
        } else {
          setError(errorMessage(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const createToken = async (event: React.FormEvent) => {
    event.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      const days = expiresInDays.trim();
      const body: { name: string; expires_in_days?: number } = { name: name.trim() };
      if (days) {
        body.expires_in_days = Number(days);
      }
      const created = await apiJson<ApiTokenCreateResponse>(TOKENS_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setCreatedToken(created);
      setCopied(false);
      setName('');
      setExpiresInDays('');
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUnavailable(true);
      } else {
        setCreateError(errorMessage(err));
      }
    } finally {
      setCreating(false);
    }
  };

  const copyToken = async () => {
    if (!createdToken) return;
    try {
      await navigator.clipboard.writeText(createdToken.token);
      setCopied(true);
    } catch {
      // Clipboard API unavailable — the token stays visible/selectable.
    }
  };

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6">
          <h1 className="text-2xl font-semibold text-slate-950">Settings</h1>
          <p className="mt-1 text-sm text-slate-500">Manage your personal account settings.</p>
        </header>

        <SectionCard
          title="API tokens"
          description="Create personal access tokens to use the API programmatically. Send them as an Authorization: Bearer header."
        >
          {unavailable ? (
            <p className="py-6 text-center text-sm text-slate-500">Not available on this backend yet.</p>
          ) : (
            <>
              <ErrorNotice message={error} />

              <form
                onSubmit={createToken}
                className="mb-6 flex flex-wrap items-end gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4"
              >
                <div className="w-full max-w-xs">
                  <Field label="Name">
                    <input
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      className={inputClass}
                      placeholder="CI pipeline"
                      required
                    />
                  </Field>
                </div>
                <div className="w-full max-w-[180px]">
                  <Field label="Expires in (days)" hint="Optional">
                    <input
                      type="number"
                      min={1}
                      value={expiresInDays}
                      onChange={(event) => setExpiresInDays(event.target.value)}
                      className={inputClass}
                      placeholder="Never"
                    />
                  </Field>
                </div>
                <Button type="submit" size="sm" disabled={creating}>
                  <Plus className="h-4 w-4" />
                  {creating ? 'Creating…' : 'Create token'}
                </Button>
              </form>
              <ErrorNotice message={createError} />

              {createdToken && (
                <div className="mb-6 rounded-xl border border-emerald-300 bg-emerald-50 p-4">
                  <p className="text-sm font-semibold text-emerald-900">Token created: {createdToken.name}</p>
                  <div className="mt-2 flex items-center gap-2">
                    <code className="flex-1 overflow-x-auto rounded-lg border border-emerald-200 bg-white px-3 py-2 text-xs text-slate-950">
                      {createdToken.token}
                    </code>
                    <Button type="button" size="sm" variant="outline" onClick={copyToken}>
                      {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                      {copied ? 'Copied' : 'Copy'}
                    </Button>
                  </div>
                  <p className="mt-2 text-xs font-medium text-emerald-800">This token is shown only once — store it now.</p>
                </div>
              )}

              {loading ? (
                <LoadingState label="Loading tokens…" />
              ) : tokens.length === 0 ? (
                <p className="py-8 text-center text-sm text-slate-500">No API tokens yet.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full table-auto text-left text-xs sm:text-sm">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="pb-2 pr-3 font-medium">Name</th>
                        <th className="pb-2 pr-3 font-medium">Token</th>
                        <th className="hidden pb-2 pr-3 font-medium md:table-cell">Created</th>
                        <th className="hidden pb-2 pr-3 font-medium lg:table-cell">Last used</th>
                        <th className="hidden pb-2 pr-3 font-medium sm:table-cell">Expires</th>
                        <th className="pb-2 font-medium" />
                      </tr>
                    </thead>
                    <tbody>
                      {tokens.map((token) => (
                        <tr key={token.id} className="border-t border-slate-100">
                          <td className="py-3 pr-3 font-medium text-slate-950">{token.name}</td>
                          <td className="py-3 pr-3 font-mono text-xs text-slate-600">{token.token_prefix}…</td>
                          <td className="hidden py-3 pr-3 text-slate-700 md:table-cell">{formatDate(token.created_at)}</td>
                          <td className="hidden py-3 pr-3 text-slate-700 lg:table-cell">{formatDate(token.last_used_at)}</td>
                          <td className="hidden py-3 pr-3 text-slate-700 sm:table-cell">{formatDate(token.expires_at)}</td>
                          <td className="py-3">
                            <div className="flex justify-end">
                              <button
                                onClick={() => setRevoking(token)}
                                aria-label={`Revoke ${token.name}`}
                                title="Revoke"
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
            </>
          )}
        </SectionCard>

        {revoking && (
          <ConfirmDialog
            title="Revoke token"
            body={
              <p>
                Revoke <span className="font-semibold text-slate-950">{revoking.name}</span>? Any integration using it
                will stop working immediately.
              </p>
            }
            confirmLabel="Revoke token"
            onClose={() => setRevoking(null)}
            onConfirm={async () => {
              await apiSend(`${TOKENS_PATH}/${revoking.id}`, { method: 'DELETE' });
              setRevoking(null);
              await load();
            }}
          />
        )}
      </div>
    </main>
  );
}
