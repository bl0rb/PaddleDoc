'use client';

import { useState } from 'react';
import { CircleCheck, CircleX, LoaderCircle, Pencil, PlugZap, Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { apiJson } from '@/lib/api';
import type {
  AdminProvider,
  ProviderCreateRequest,
  ProviderTestResponse,
  ProviderUpdateRequest,
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

const DEFAULT_SCOPES = 'openid profile email';

export function ProvidersTab() {
  const providers = useAdminList<AdminProvider>('/api/v1/auth/admin/providers');

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminProvider | null>(null);
  const [deleting, setDeleting] = useState<AdminProvider | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ProviderTestResponse>>({});

  async function testProvider(id: string) {
    setTesting(id);
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      const res = await apiJson<ProviderTestResponse>(`/api/v1/auth/admin/providers/${id}/test`, {
        method: 'POST',
      });
      setTestResults((prev) => ({ ...prev, [id]: res }));
    } catch (err) {
      setTestResults((prev) => ({ ...prev, [id]: { ok: false, detail: errorMessage(err) } }));
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="space-y-6">
      <SectionCard
        title="Identity providers"
        description="OIDC providers users can sign in with. Stored client secrets are never displayed."
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" />
            Add provider
          </Button>
        }
      >
        <ErrorNotice message={providers.error} />
        {providers.loading ? (
          <LoadingState label="Loading providers…" />
        ) : providers.items.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">
            No identity providers configured. Add one to enable SSO login.
          </p>
        ) : (
          <ul className="space-y-4">
            {providers.items.map((p) => (
              <li key={p.id} className="rounded-xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold text-slate-950">{p.display_name}</span>
                      <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-600">
                        {p.slug}
                      </span>
                      <Badge tone={p.enabled ? 'emerald' : 'slate'}>
                        {p.enabled ? 'Enabled' : 'Disabled'}
                      </Badge>
                      <Badge tone={p.client_secret_set ? 'emerald' : 'amber'}>
                        {p.client_secret_set ? 'Secret set' : 'No secret'}
                      </Badge>
                    </div>
                    <dl className="mt-2 space-y-1 text-xs text-slate-500">
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Issuer</dt>
                        <dd className="break-all">{p.issuer_url}</dd>
                      </div>
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Client ID</dt>
                        <dd className="break-all font-mono">{p.client_id}</dd>
                      </div>
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Scopes</dt>
                        <dd className="break-all">{p.scopes}</dd>
                      </div>
                    </dl>
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-1">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => testProvider(p.id)}
                      disabled={testing !== null}
                    >
                      {testing === p.id ? (
                        <LoaderCircle className="h-4 w-4 animate-spin" />
                      ) : (
                        <PlugZap className="h-4 w-4" />
                      )}
                      Test connection
                    </Button>
                    <button
                      onClick={() => setEditing(p)}
                      aria-label={`Edit ${p.display_name}`}
                      title="Edit"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => setDeleting(p)}
                      aria-label={`Delete ${p.display_name}`}
                      title="Delete"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {testResults[p.id] && <TestResult result={testResults[p.id]} />}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      {creating && (
        <ProviderModal
          onClose={() => setCreating(false)}
          onSaved={async () => {
            setCreating(false);
            await providers.reload();
          }}
        />
      )}

      {editing && (
        <ProviderModal
          provider={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await providers.reload();
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete provider"
          body={
            <p>
              Delete <span className="font-semibold text-slate-950">{deleting.display_name}</span>?
              Users who signed in through this provider will no longer be able to log in with it.
            </p>
          }
          confirmLabel="Delete provider"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await apiSend(`/api/v1/auth/admin/providers/${deleting.id}`, { method: 'DELETE' });
            setDeleting(null);
            await providers.reload();
          }}
        />
      )}
    </div>
  );
}

function TestResult({ result }: { result: ProviderTestResponse }) {
  return (
    <div
      className={`mt-3 rounded-xl border px-4 py-3 text-sm ${
        result.ok
          ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
          : 'border-red-200 bg-red-50 text-red-700'
      }`}
    >
      <div className="flex items-center gap-2 font-medium">
        {result.ok ? (
          <CircleCheck className="h-4 w-4 flex-shrink-0" />
        ) : (
          <CircleX className="h-4 w-4 flex-shrink-0" />
        )}
        {result.ok ? 'Connection successful' : 'Connection failed'}
      </div>
      {result.detail && <p className="mt-1 text-xs">{result.detail}</p>}
      {(result.issuer || result.authorization_endpoint || result.token_endpoint) && (
        <dl className="mt-2 space-y-1 text-xs">
          {result.issuer && (
            <div className="flex gap-2">
              <dt className="w-24 flex-shrink-0 font-medium">Issuer</dt>
              <dd className="break-all">{result.issuer}</dd>
            </div>
          )}
          {result.authorization_endpoint && (
            <div className="flex gap-2">
              <dt className="w-24 flex-shrink-0 font-medium">Authorization</dt>
              <dd className="break-all">{result.authorization_endpoint}</dd>
            </div>
          )}
          {result.token_endpoint && (
            <div className="flex gap-2">
              <dt className="w-24 flex-shrink-0 font-medium">Token</dt>
              <dd className="break-all">{result.token_endpoint}</dd>
            </div>
          )}
        </dl>
      )}
    </div>
  );
}

/** Create (no `provider`) or edit (with `provider`) an OIDC provider. */
function ProviderModal({
  provider,
  onClose,
  onSaved,
}: {
  provider?: AdminProvider;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const isEdit = provider !== undefined;

  const [slug, setSlug] = useState(provider?.slug ?? '');
  const [displayName, setDisplayName] = useState(provider?.display_name ?? '');
  const [issuerUrl, setIssuerUrl] = useState(provider?.issuer_url ?? '');
  const [clientId, setClientId] = useState(provider?.client_id ?? '');
  const [clientSecret, setClientSecret] = useState('');
  const [scopes, setScopes] = useState(provider?.scopes ?? DEFAULT_SCOPES);
  const [enabled, setEnabled] = useState(provider?.enabled ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (isEdit) {
        const body: ProviderUpdateRequest = {
          display_name: displayName.trim(),
          issuer_url: issuerUrl.trim(),
          client_id: clientId.trim(),
          scopes: scopes.trim(),
          enabled,
          ...(clientSecret ? { client_secret: clientSecret } : {}),
        };
        await apiJson<AdminProvider>(`/api/v1/auth/admin/providers/${provider.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } else {
        const body: ProviderCreateRequest = {
          slug: slug.trim(),
          display_name: displayName.trim(),
          issuer_url: issuerUrl.trim(),
          client_id: clientId.trim(),
          client_secret: clientSecret,
          scopes: scopes.trim(),
          enabled,
        };
        await apiJson<AdminProvider>('/api/v1/auth/admin/providers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      }
      await onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal title={isEdit ? `Edit ${provider.display_name}` : 'Add identity provider'} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        {!isEdit && (
          <Field label="Slug" hint="URL-safe identifier, e.g. entra or keycloak. Cannot be changed later.">
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className={inputClass}
              required
              autoFocus
              pattern="[a-z0-9-]+"
              title="Lowercase letters, digits, and hyphens"
            />
          </Field>
        )}
        <Field label="Display name" hint="Shown on the login button.">
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className={inputClass}
            required
          />
        </Field>
        <Field label="Issuer URL">
          <input
            type="url"
            value={issuerUrl}
            onChange={(e) => setIssuerUrl(e.target.value)}
            className={inputClass}
            required
            placeholder="https://login.example.com/realms/main"
          />
        </Field>
        <Field label="Client ID">
          <input
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            className={inputClass}
            required
          />
        </Field>
        <Field
          label="Client secret"
          hint={isEdit ? 'Unchanged unless filled.' : undefined}
        >
          <input
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            className={inputClass}
            required={!isEdit}
            placeholder={isEdit ? 'unchanged unless filled' : undefined}
            autoComplete="new-password"
          />
        </Field>
        <Field label="Scopes">
          <input
            value={scopes}
            onChange={(e) => setScopes(e.target.value)}
            className={inputClass}
            required
            placeholder={DEFAULT_SCOPES}
          />
        </Field>
        <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
        <ErrorNotice message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}
            {isEdit ? 'Save changes' : 'Add provider'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
