'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { motion } from 'framer-motion';
import { LoaderCircle, RefreshCcw, UploadCloud, X } from 'lucide-react';
import { useRouter } from 'next/navigation';

import { Button } from '@/components/ui/button';
import {
  API,
  UploadError,
  formatBytes,
  sendFormDataWithProgress,
  type PaddleCapabilities,
  type UploadProgress,
} from '@/components/dashboard/shared';
import { useVisiblePolling } from '@/lib/data-cache';
import {
  ApiError,
  apiJson,
  benchmarkStatusChip,
  isBenchmarkRunActive,
  MAX_BENCHMARK_VARIANTS,
  MAX_VL_CONNECTIONS,
  MIN_BENCHMARK_VARIANTS,
  type BenchmarkRun,
  type BenchmarkRunDetail,
  type BenchmarkRunListResponse,
  type VLConnection,
  type VLConnectionListResponse,
} from '@/lib/api';

const RUNS_POLL_INTERVAL_MS = 4000;

export default function BenchmarkPage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- VL connections + OCR capabilities (loaded once) ---
  const [connections, setConnections] = useState<VLConnection[]>([]);
  const [connectionsLoaded, setConnectionsLoaded] = useState(false);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<PaddleCapabilities>({ profiles: [] });

  useEffect(() => {
    let cancelled = false;
    apiJson<VLConnectionListResponse>('/api/v1/vl-connections', { cache: 'no-store' })
      .then((payload) => {
        if (!cancelled) setConnections(payload.items);
      })
      .catch((error) => {
        if (!cancelled) {
          setConnectionsError(
            error instanceof ApiError ? error.detail : 'Failed to load VL connections.',
          );
        }
      })
      .finally(() => {
        if (!cancelled) setConnectionsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    apiJson<PaddleCapabilities>('/api/v1/paddle/capabilities', { cache: 'no-store' })
      .then((payload) => {
        if (!cancelled) setCapabilities({ profiles: payload.profiles ?? [] });
      })
      .catch(() => {
        // Profile picker just stays empty — "no profile" remains selectable.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // --- Start form ---
  const [file, setFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [selectedConnectionIds, setSelectedConnectionIds] = useState<string[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [busy, setBusy] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const totalVariants = selectedConnectionIds.length + (selectedProfileId ? 1 : 0);
  const variantsValid = totalVariants >= MIN_BENCHMARK_VARIANTS && totalVariants <= MAX_BENCHMARK_VARIANTS;

  const toggleConnection = (id: string) => {
    setSelectedConnectionIds((current) => {
      if (current.includes(id)) return current.filter((entry) => entry !== id);
      if (current.length >= MAX_VL_CONNECTIONS) return current;
      return [...current, id];
    });
  };

  const onDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  };

  const startBenchmark = async () => {
    if (!file) return;
    if (!variantsValid) {
      setStartError(
        `Select ${MIN_BENCHMARK_VARIANTS} to ${MAX_BENCHMARK_VARIANTS} combined connections and profile.`,
      );
      return;
    }
    setBusy(true);
    setStartError(null);
    setUploadProgress({
      phase: 'single',
      currentFile: file.name,
      filesCompleted: 0,
      filesTotal: 1,
      bytesLoaded: 0,
      bytesTotal: file.size || 1,
    });
    try {
      const formData = new FormData();
      formData.append('file', file);
      for (const id of selectedConnectionIds) {
        formData.append('vl_connection_ids', id);
      }
      if (selectedProfileId) {
        formData.append('profile_id', selectedProfileId);
      }
      const result = (await sendFormDataWithProgress(`${API}/api/v1/benchmarks`, formData, (loaded, total) => {
        setUploadProgress({
          phase: 'single',
          currentFile: file.name,
          filesCompleted: 0,
          filesTotal: 1,
          bytesLoaded: loaded,
          bytesTotal: total || file.size || 1,
        });
      })) as BenchmarkRunDetail;
      router.push(`/benchmark/${result.id}`);
    } catch (error) {
      setStartError(error instanceof UploadError ? error.message : 'Failed to start the benchmark.');
    } finally {
      setUploadProgress(null);
      setBusy(false);
    }
  };

  // --- Runs list ---
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);

  const [runsReloadNonce, setRunsReloadNonce] = useState(0);

  // The load function is defined locally inside the effect (rather than as a
  // hoisted helper called from it) so every state update it makes is
  // reachable only through this effect's own `cancelled` cleanup guard.
  // Polling and the manual Refresh button re-run it by bumping the nonce
  // dependency instead of holding a callable reference to it.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await apiJson<BenchmarkRunListResponse>('/api/v1/benchmarks', { cache: 'no-store' });
        if (cancelled) return;
        setRuns(payload.items);
        setRunsError(null);
      } catch (error) {
        if (cancelled) return;
        setRunsError(error instanceof ApiError ? error.detail : 'Failed to load benchmark runs.');
      } finally {
        if (!cancelled) setRunsLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [runsReloadNonce]);

  const reloadRuns = () => setRunsReloadNonce((nonce) => nonce + 1);
  const refreshRuns = () => {
    setRunsLoading(true);
    reloadRuns();
  };

  useVisiblePolling(
    reloadRuns,
    runs.some((run) => isBenchmarkRunActive(run.status)) ? RUNS_POLL_INTERVAL_MS : null,
  );

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Benchmark</h1>
            <p className="mt-2 text-slate-600">
              Run the same document through multiple VL connections and compare output quality.
            </p>
          </div>
          <Button variant="outline" onClick={refreshRuns} disabled={runsLoading}>
            <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
          </Button>
        </section>

        <section className="mb-8 rounded-xl border border-slate-200 bg-gradient-to-br from-emerald-50 to-white p-5">
          <h2 className="mb-3 text-lg font-semibold">Start a benchmark</h2>

          {connectionsError && (
            <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {connectionsError} Benchmarks need at least one enabled VL connection.
            </p>
          )}

          <div className="space-y-4">
            {file ? (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-950">{file.name}</p>
                  <p className="text-xs text-slate-500">{formatBytes(file.size)}</p>
                </div>
                <button
                  type="button"
                  onClick={() => setFile(null)}
                  aria-label="Remove selected file"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ) : (
              <motion.div
                onDrop={onDrop}
                onDragOver={(event) => event.preventDefault()}
                onDragEnter={() => setDragActive(true)}
                onDragLeave={() => setDragActive(false)}
                className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-10 text-center"
                animate={{ borderColor: dragActive ? '#6ee7b7' : '#10b981' }}
              >
                <UploadCloud className="mx-auto mb-4 h-10 w-10 text-slate-600" />
                <p className="mb-2 text-lg font-medium">Drag and drop a file here</p>
                <p className="mb-4 text-sm text-slate-600">PDF, DOCX, PPTX, XLSX, XLS, PNG, JPG, JPEG</p>
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  accept=".pdf,.docx,.pptx,.xlsx,.xls,.png,.jpg,.jpeg"
                  onChange={(event) => {
                    const selected = event.currentTarget.files?.[0];
                    if (selected) setFile(selected);
                    event.currentTarget.value = '';
                  }}
                />
                <Button variant="outline" onClick={() => fileInputRef.current?.click()}>
                  Select file
                </Button>
              </motion.div>
            )}

            <div>
              <div className="mb-2 flex items-center justify-between gap-4">
                <p className="text-sm font-medium text-slate-950">VL connections</p>
                <p className="text-xs text-slate-500">
                  {selectedConnectionIds.length >= MAX_VL_CONNECTIONS
                    ? `Limit reached (${MAX_VL_CONNECTIONS}/${MAX_VL_CONNECTIONS}).`
                    : `Up to ${MAX_VL_CONNECTIONS} connections per run.`}
                </p>
              </div>
              {!connectionsLoaded ? (
                <div className="flex items-center gap-2 py-4 text-sm text-slate-600">
                  <LoaderCircle className="h-4 w-4 animate-spin" /> Loading connections...
                </div>
              ) : connections.length === 0 ? (
                <p className="text-sm text-slate-600">
                  No enabled VL connections yet. Ask an admin to add one — a benchmark needs at least{' '}
                  {MIN_BENCHMARK_VARIANTS} variants.
                </p>
              ) : (
                <div className="grid gap-3 md:grid-cols-2">
                  {connections.map((connection) => {
                    const checked = selectedConnectionIds.includes(connection.id);
                    const capped = !checked && selectedConnectionIds.length >= MAX_VL_CONNECTIONS;
                    return (
                      <label
                        key={connection.id}
                        className={`flex items-start gap-3 rounded-xl border p-4 text-left transition ${
                          checked ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                        } ${capped ? 'opacity-50' : ''}`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={capped}
                          onChange={() => toggleConnection(connection.id)}
                          className="mt-0.5 h-4 w-4 accent-emerald-600"
                        />
                        <span className="min-w-0">
                          <span className="block truncate text-sm font-semibold text-slate-950">
                            {connection.name}
                          </span>
                          <span className="block truncate text-xs text-slate-600">{connection.model}</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
            </div>

            <label className="block text-sm text-slate-600">
              OCR profile (optional baseline)
              <select
                value={selectedProfileId}
                onChange={(event) => setSelectedProfileId(event.target.value)}
                className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
              >
                <option value="">No profile (VL connections only)</option>
                {capabilities.profiles.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>

            <p className={`text-xs ${variantsValid ? 'text-slate-500' : 'text-amber-700'}`}>
              {totalVariants} of {MIN_BENCHMARK_VARIANTS}-{MAX_BENCHMARK_VARIANTS} variants selected
              {!variantsValid &&
                ` — select at least ${MIN_BENCHMARK_VARIANTS} combined connections and profile.`}
            </p>

            {startError && (
              <p className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                {startError}
              </p>
            )}

            {uploadProgress && (
              <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p className="font-medium text-slate-950">Uploading file</p>
                    <p className="text-xs text-slate-500">{uploadProgress.currentFile}</p>
                  </div>
                </div>
                <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full rounded-full bg-emerald-500 transition-all"
                    style={{
                      width: `${Math.min(100, Math.round((uploadProgress.bytesLoaded / uploadProgress.bytesTotal) * 100))}%`,
                    }}
                  />
                </div>
                <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
                  <span>
                    {Math.min(100, Math.round((uploadProgress.bytesLoaded / uploadProgress.bytesTotal) * 100))}%
                  </span>
                  <span>
                    {formatBytes(uploadProgress.bytesLoaded)} / {formatBytes(uploadProgress.bytesTotal)}
                  </span>
                </div>
              </div>
            )}

            <div className="flex justify-end">
              <Button onClick={() => void startBenchmark()} disabled={!file || !variantsValid || busy}>
                {busy ? 'Starting...' : 'Start benchmark'}
              </Button>
            </div>
          </div>
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Runs</h2>
            <p className="text-sm text-slate-500">{runs.length} run(s)</p>
          </div>
          {runsError && <p className="mb-3 text-sm text-red-600">{runsError}</p>}
          <div className="overflow-x-auto">
            <table className="w-full table-auto text-left text-xs sm:text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 font-medium">Filename</th>
                  <th className="pb-2 font-medium">Status</th>
                  <th className="pb-2 font-medium">Variants</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="border-t border-slate-100">
                    <td className="py-3">
                      <Link
                        href={`/benchmark/${run.id}`}
                        className="line-clamp-2 font-medium text-slate-950 hover:text-emerald-700"
                      >
                        {run.original_filename}
                      </Link>
                      {run.owner && <p className="mt-1 text-xs text-slate-500">{run.owner.username}</p>}
                    </td>
                    <td className="py-3">
                      <span className={`rounded px-2 py-1 text-xs ${benchmarkStatusChip[run.status]}`}>
                        {run.status}
                      </span>
                    </td>
                    <td className="py-3 text-slate-700">{run.variant_count}</td>
                    <td className="hidden py-3 text-slate-700 md:table-cell">
                      {new Date(run.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {runs.length === 0 && !runsLoading && (
              <p className="py-6 text-sm text-slate-600">No benchmark runs yet. Start one above.</p>
            )}
            {runsLoading && (
              <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
                <LoaderCircle className="h-4 w-4 animate-spin" /> Loading benchmark runs...
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
