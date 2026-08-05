'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ChevronDown, ChevronRight, LoaderCircle, Pencil, Plus, RefreshCcw, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import { formatBytes } from '@/components/dashboard/shared';
import {
  type ImportRun,
  type ImportRunListResponse,
  type ImportSource,
  type ImportSourceListResponse,
  runStatusChip,
  runTitle,
} from '@/lib/imports';

export default function ImportsPage() {
  const [runs, setRuns] = useState<ImportRun[]>([]);
  const [sources, setSources] = useState<ImportSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [sourcesMessage, setSourcesMessage] = useState<string | null>(null);
  const [renamingSourceId, setRenamingSourceId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [busySourceId, setBusySourceId] = useState<string | null>(null);

  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [runsPayload, sourcesPayload] = await Promise.all([
          apiJson<ImportRunListResponse>('/api/v1/import/runs', { cache: 'no-store' }),
          apiJson<ImportSourceListResponse>('/api/v1/import/sources', { cache: 'no-store' }),
        ]);
        if (cancelled) return;
        setRuns(runsPayload.items);
        setSources(sourcesPayload.items);
        setLoadError(null);
      } catch (error) {
        if (!cancelled) {
          setLoadError(error instanceof ApiError ? error.detail : 'Failed to load imports.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const loadAll = () => {
    setLoading(true);
    setReloadNonce((nonce) => nonce + 1);
  };

  const startRename = (source: ImportSource) => {
    setRenamingSourceId(source.id);
    setRenameValue(source.name);
    setSourcesMessage(null);
  };

  const saveRename = async (sourceId: string) => {
    const name = renameValue.trim();
    if (!name) {
      setSourcesMessage('Name cannot be empty.');
      return;
    }
    setBusySourceId(sourceId);
    try {
      const updated = await apiJson<ImportSource>(`/api/v1/import/sources/${sourceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      setSources((current) => current.map((entry) => (entry.id === sourceId ? updated : entry)));
      setRenamingSourceId(null);
      setSourcesMessage(null);
    } catch (error) {
      setSourcesMessage(error instanceof ApiError ? error.detail : 'Failed to rename source.');
    } finally {
      setBusySourceId(null);
    }
  };

  const deleteSource = async (source: ImportSource) => {
    if (!window.confirm(`Delete source "${source.name}"? Past runs keep their history.`)) {
      return;
    }
    setBusySourceId(source.id);
    try {
      await apiJson<{ status: string }>(`/api/v1/import/sources/${source.id}`, { method: 'DELETE' });
      setSources((current) => current.filter((entry) => entry.id !== source.id));
      setSourcesMessage(null);
    } catch (error) {
      setSourcesMessage(error instanceof ApiError ? error.detail : 'Failed to delete source.');
    } finally {
      setBusySourceId(null);
    }
  };

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Confluence Imports</h1>
            <p className="mt-2 text-slate-600">
              Import runs pull Confluence pages into PaddleDoc as markdown jobs.
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => void loadAll()} disabled={loading}>
              <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
            </Button>
            <Link href="/imports/new">
              <Button>
                <Plus className="mr-2 h-4 w-4" /> New import
              </Button>
            </Link>
          </div>
        </section>

        {loadError && (
          <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</p>
        )}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Runs</h2>
            <p className="text-sm text-slate-500">{runs.length} run(s)</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full table-auto text-left text-xs sm:text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 font-medium">Import</th>
                  <th className="pb-2 font-medium">Status</th>
                  <th className="pb-2 font-medium">Pages</th>
                  <th className="hidden pb-2 font-medium sm:table-cell">Attachments</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Size</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="border-t border-slate-100">
                    <td className="py-3">
                      <Link href={`/imports/${run.id}`} className="line-clamp-2 font-medium text-slate-950 hover:text-emerald-700">
                        {runTitle(run)}
                      </Link>
                      <p className="mt-1 text-xs text-slate-500">
                        {run.scope_type === 'space' ? `Space key: ${run.scope_value}` : `Page id: ${run.scope_value}`}
                        {run.owner ? ` · ${run.owner.username}` : ''}
                      </p>
                    </td>
                    <td className="py-3">
                      <span className={`rounded px-2 py-1 text-xs ${runStatusChip[run.status]}`}>{run.status}</span>
                    </td>
                    <td className="py-3 text-slate-700">
                      {run.pages_imported} / {run.pages_discovered}
                      {run.pages_failed > 0 && <span className="ml-1 text-xs text-red-600">({run.pages_failed} failed)</span>}
                    </td>
                    <td className="hidden py-3 text-slate-700 sm:table-cell">{run.attachments_saved}</td>
                    <td className="hidden py-3 text-slate-700 md:table-cell">
                      {formatBytes(run.artifact_bytes + run.content_bytes)}
                    </td>
                    <td className="hidden py-3 text-slate-700 md:table-cell">{new Date(run.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {runs.length === 0 && !loading && (
              <p className="py-6 text-sm text-slate-600">
                No import runs yet. Start one with the New import button.
              </p>
            )}
            {loading && (
              <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
                <LoaderCircle className="h-4 w-4 animate-spin" /> Loading imports...
              </div>
            )}
          </div>
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <button
            type="button"
            onClick={() => setSourcesOpen((value) => !value)}
            aria-expanded={sourcesOpen}
            className="flex w-full items-center justify-between text-left"
          >
            <span className="flex items-center gap-2 text-lg font-semibold">
              {sourcesOpen ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
              Sources
            </span>
            <span className="text-sm text-slate-500">{sources.length} source(s)</span>
          </button>

          {sourcesOpen && (
            <div className="mt-4 space-y-2">
              <p className="text-sm text-slate-600">
                Connections are private to you. Credentials are write-only and never shown; create new sources from the
                import wizard.
              </p>
              {sourcesMessage && <p className="text-sm text-red-600">{sourcesMessage}</p>}
              {sources.map((source) => (
                <div
                  key={source.id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3"
                >
                  <div className="min-w-0">
                    {renamingSourceId === source.id ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <input
                          value={renameValue}
                          onChange={(event) => setRenameValue(event.target.value)}
                          aria-label="Source name"
                          onKeyDown={(event) => {
                            if (event.key === 'Enter') void saveRename(source.id);
                            if (event.key === 'Escape') setRenamingSourceId(null);
                          }}
                          className="rounded border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-950"
                          autoFocus
                        />
                        <Button size="sm" onClick={() => void saveRename(source.id)} disabled={busySourceId === source.id}>
                          Save
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setRenamingSourceId(null)}>
                          Cancel
                        </Button>
                      </div>
                    ) : (
                      <p className="truncate text-sm font-semibold text-slate-950">{source.name}</p>
                    )}
                    <p className="mt-0.5 truncate text-xs text-slate-500">
                      {source.base_url} · {source.auth_type === 'cloud_basic' ? 'Cloud (email + API token)' : 'Personal access token'}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`rounded px-2 py-1 text-xs ${
                        source.server_kind ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-600'
                      }`}
                    >
                      {source.server_kind === 'cloud'
                        ? 'Cloud'
                        : source.server_kind === 'datacenter'
                          ? 'Server/DC'
                          : 'untested'}
                    </span>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="h-8 w-8 px-0"
                      disabled={busySourceId === source.id}
                      onClick={() => startRename(source)}
                      aria-label={`Rename source ${source.name}`}
                    >
                      <Pencil className="h-4 w-4 text-slate-700" aria-hidden="true" />
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="h-8 w-8 px-0"
                      disabled={busySourceId === source.id}
                      onClick={() => void deleteSource(source)}
                      aria-label={`Delete source ${source.name}`}
                    >
                      <Trash2 className="h-4 w-4 text-red-600" aria-hidden="true" />
                    </Button>
                  </div>
                </div>
              ))}
              {sources.length === 0 && !loading && (
                <p className="py-2 text-sm text-slate-600">No sources yet. The import wizard creates one on first use.</p>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
