'use client';

import { useCallback, useEffect, useState } from 'react';
import { CircleCheck, CircleX, LoaderCircle, Pencil, PlugZap, Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import type { ListResponse } from '@/lib/auth-types';
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
} from '@/components/admin/admin-shared';

/**
 * TypeScript mirrors of the backend VL-connection admin schemas
 * (backend/app/api/v1/auth/admin — /api/v1/auth/admin/vl-connections).
 * Kept local to this file rather than in lib/auth-types.ts per this
 * round's file-ownership split.
 */

/** VlConnectionAdminResponse — one item from /api/v1/auth/admin/vl-connections. */
interface AdminVlConnection {
  id: string;
  name: string;
  base_url: string;
  model: string;
  /** Write-only key: responses only report whether one is stored. */
  has_api_key: boolean;
  system_prompt: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

/** POST /api/v1/auth/admin/vl-connections */
interface VlConnectionCreateRequest {
  name: string;
  base_url: string;
  model: string;
  api_key: string;
  system_prompt?: string;
  enabled?: boolean;
}

/** PUT /api/v1/auth/admin/vl-connections/{id} — api_key is write-only; omit to keep the stored key. */
interface VlConnectionUpdateRequest {
  name?: string;
  base_url?: string;
  model?: string;
  api_key?: string;
  system_prompt?: string;
  enabled?: boolean;
}

/** POST /api/v1/auth/admin/vl-connections/{id}/test */
interface VlConnectionTestResponse {
  ok: boolean;
  detail?: string | null;
  latency_ms?: number | null;
}

const BASE = '/api/v1/auth/admin/vl-connections';

export function VlConnectionsTab() {
  const [connections, setConnections] = useState<AdminVlConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure — rendered as a distinct, non-alarming notice (mirrors LogsTab).
  const [unavailable, setUnavailable] = useState(false);

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminVlConnection | null>(null);
  const [deleting, setDeleting] = useState<AdminVlConnection | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, VlConnectionTestResponse>>({});

  const reload = useCallback(async () => {
    try {
      const data = await apiJson<ListResponse<AdminVlConnection>>(BASE);
      setConnections(data.items);
      setListError(null);
      setUnavailable(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUnavailable(true);
        setListError(null);
      } else {
        setListError(errorMessage(err));
        setUnavailable(false);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function testConnection(id: string) {
    setTesting(id);
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      const res = await apiJson<VlConnectionTestResponse>(`${BASE}/${id}/test`, { method: 'POST' });
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
        title="VL connections"
        description="Vision-language model connections used for document processing. Stored API keys are never displayed."
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" />
            Add connection
          </Button>
        }
      >
        <ErrorNotice message={listError} />
        {unavailable && (
          <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            VL connections are not available on this backend yet. This tab starts showing data
            automatically once the endpoint is deployed.
          </div>
        )}
        {loading ? (
          <LoadingState label="Loading VL connections…" />
        ) : unavailable ? null : connections.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">
            No VL connections configured. Add one to enable vision-language processing.
          </p>
        ) : (
          <ul className="space-y-4">
            {connections.map((c) => (
              <li key={c.id} className="rounded-xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold text-slate-950">{c.name}</span>
                      <Badge tone={c.enabled ? 'emerald' : 'slate'}>
                        {c.enabled ? 'Enabled' : 'Disabled'}
                      </Badge>
                      <Badge tone={c.has_api_key ? 'emerald' : 'amber'}>
                        {c.has_api_key ? 'API key set' : 'No API key'}
                      </Badge>
                    </div>
                    <dl className="mt-2 space-y-1 text-xs text-slate-500">
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Base URL</dt>
                        <dd className="break-all">{c.base_url}</dd>
                      </div>
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Model</dt>
                        <dd className="break-all font-mono">{c.model}</dd>
                      </div>
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Created</dt>
                        <dd>{new Date(c.created_at).toLocaleDateString()}</dd>
                      </div>
                    </dl>
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-1">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => testConnection(c.id)}
                      disabled={testing !== null}
                    >
                      {testing === c.id ? (
                        <LoaderCircle className="h-4 w-4 animate-spin" />
                      ) : (
                        <PlugZap className="h-4 w-4" />
                      )}
                      Test
                    </Button>
                    <button
                      onClick={() => setEditing(c)}
                      aria-label={`Edit ${c.name}`}
                      title="Edit"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => setDeleting(c)}
                      aria-label={`Delete ${c.name}`}
                      title="Delete"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {testResults[c.id] && <VlTestResult result={testResults[c.id]} />}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      {creating && (
        <VlConnectionModal
          onClose={() => setCreating(false)}
          onSaved={async () => {
            setCreating(false);
            await reload();
          }}
        />
      )}

      {editing && (
        <VlConnectionModal
          connection={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await reload();
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete VL connection"
          body={
            <p>
              Delete <span className="font-semibold text-slate-950">{deleting.name}</span>? Jobs
              configured to use it for processing may fail until reconfigured.
            </p>
          }
          confirmLabel="Delete connection"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await apiSend(`${BASE}/${deleting.id}`, { method: 'DELETE' });
            setDeleting(null);
            await reload();
          }}
        />
      )}
    </div>
  );
}

function VlTestResult({ result }: { result: VlConnectionTestResponse }) {
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
      {result.ok && result.latency_ms != null && (
        <p className="mt-1 text-xs">{result.latency_ms} ms</p>
      )}
    </div>
  );
}

/** Create (no `connection`) or edit (with `connection`) a VL connection. */
function VlConnectionModal({
  connection,
  onClose,
  onSaved,
}: {
  connection?: AdminVlConnection;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const isEdit = connection !== undefined;

  const [name, setName] = useState(connection?.name ?? '');
  const [baseUrl, setBaseUrl] = useState(connection?.base_url ?? '');
  const [model, setModel] = useState(connection?.model ?? '');
  const [apiKey, setApiKey] = useState('');
  const [systemPrompt, setSystemPrompt] = useState(connection?.system_prompt ?? '');
  const [enabled, setEnabled] = useState(connection?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (isEdit) {
        const body: VlConnectionUpdateRequest = {
          name: name.trim(),
          base_url: baseUrl.trim(),
          model: model.trim(),
          system_prompt: systemPrompt.trim(),
          enabled,
          ...(apiKey ? { api_key: apiKey } : {}),
        };
        await apiJson<AdminVlConnection>(`${BASE}/${connection.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } else {
        const body: VlConnectionCreateRequest = {
          name: name.trim(),
          base_url: baseUrl.trim(),
          model: model.trim(),
          api_key: apiKey,
          system_prompt: systemPrompt.trim(),
          enabled,
        };
        await apiJson<AdminVlConnection>(BASE, {
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
    <Modal title={isEdit ? `Edit ${connection.name}` : 'Add VL connection'} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
            required
            autoFocus
          />
        </Field>
        <Field label="Base URL" hint="The connection appends /v1/chat/completions — do not include it here.">
          <input
            type="url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className={inputClass}
            required
            placeholder="e.g. https://api.example.com"
          />
        </Field>
        <Field label="Model">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className={inputClass}
            required
            placeholder="e.g. qwen2-vl-7b-instruct"
          />
        </Field>
        <Field label="API key" hint={isEdit ? 'Leave blank to keep the stored key.' : undefined}>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            className={inputClass}
            required={!isEdit}
            placeholder={isEdit ? 'unchanged unless filled' : undefined}
            autoComplete="new-password"
          />
        </Field>
        <Field label="System prompt" hint="Optional. Sent as the system message for every page.">
          <textarea
            rows={4}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            className={inputClass}
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
            {isEdit ? 'Save changes' : 'Add connection'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
