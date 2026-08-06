'use client';

import { useEffect, useState } from 'react';
import { LoaderCircle, RefreshCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson, type LogLevel, type WorkerLogEntry, type WorkerLogsResponse } from '@/lib/api';
import { useVisiblePolling } from '@/lib/data-cache';
import {
  ErrorNotice,
  errorMessage,
  Field,
  inputClass,
  LoadingState,
  SectionCard,
  Toggle,
} from '@/components/admin/admin-shared';

/** Rows per fetch — the backend allows 1-500; 200 matches its own default. */
const LIMIT = 200;
/** Live-tail cadence while auto-refresh is on. */
const AUTO_REFRESH_MS = 5000;

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: 'text-slate-500',
  INFO: 'text-emerald-400',
  WARNING: 'text-amber-400',
  ERROR: 'text-red-400',
  CRITICAL: 'text-red-300',
};

/**
 * Deterministic color per worker (pod/container name) so distinct
 * replicas stay visually distinguishable in a multi-worker deployment.
 * Sky/cyan/teal/fuchsia are otherwise unused in this app, so a worker
 * chip can never be mistaken for a status color.
 */
const WORKER_COLORS = ['text-sky-400', 'text-cyan-400', 'text-teal-400', 'text-fuchsia-400'];
function workerColor(name: string): string {
  let sum = 0;
  for (let i = 0; i < name.length; i += 1) sum += name.charCodeAt(i);
  return WORKER_COLORS[sum % WORKER_COLORS.length];
}

export function LogsTab() {
  const [entries, setEntries] = useState<WorkerLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure — rendered as a distinct, non-alarming notice.
  const [unavailable, setUnavailable] = useState(false);
  const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const [level, setLevel] = useState<LogLevel | ''>('');
  const [worker, setWorker] = useState('');
  const [query, setQuery] = useState('');
  const hasFilters = level !== '' || worker.trim() !== '' || query.trim() !== '';

  // No setState before the first `await` here on purpose — callers flip
  // the relevant spinner flag themselves before calling `load`, so this
  // is safe to invoke directly from the mount effect below. `filters`
  // lets a caller fetch with values other than this render's state
  // (needed by resetFilters, whose setters only land on the next render).
  async function load(
    offset: number,
    filters?: { level: LogLevel | ''; worker: string; query: string },
  ): Promise<void> {
    const active = filters ?? { level, worker, query };
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (active.level) params.set('level', active.level);
    if (active.worker.trim()) params.set('worker', active.worker.trim());
    if (active.query.trim()) params.set('q', active.query.trim());
    try {
      const res = await apiJson<WorkerLogsResponse>(
        `/api/v1/auth/admin/worker-logs?${params.toString()}`,
        { cache: 'no-store' },
      );
      setEntries((prev) => {
        if (offset === 0) return res.items;
        // Offset-based "load older" against a newest-first, continuously
        // growing table: rows inserted between the page-0 fetch and this
        // fetch shift the window, so the older page can re-include rows
        // already rendered. Drop those by id to avoid duplicate rows and
        // duplicate React keys.
        const existingIds = new Set(prev.map((e) => e.id));
        return [...prev, ...res.items.filter((e) => !existingIds.has(e.id))];
      });
      setTotal(res.total);
      setError(null);
      setUnavailable(false);
      setLastFetchedAt(new Date());
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUnavailable(true);
        setError(null);
      } else {
        setError(errorMessage(err));
        setUnavailable(false);
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
      setLoadingMore(false);
    }
  }

  // Initial load only — filters apply on demand (Apply filters / Enter),
  // matching the rest of the app's filter forms (see document-browser.tsx).
  useEffect(() => {
    const run = async () => {
      await load(0);
    };
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-refresh always re-pulls the newest page (offset 0) and replaces
  // what's visible — any "Load older" pages fetched below get dropped on
  // the next tick, same as the tail-with-refresh pattern the endpoint
  // recommends. Pauses while the tab is hidden. No spinner here — that's
  // reserved for user-triggered fetches so the tail doesn't flicker.
  useVisiblePolling(() => void load(0), autoRefresh ? AUTO_REFRESH_MS : null);

  function refresh() {
    setRefreshing(true);
    void load(0);
  }

  function loadMore() {
    setLoadingMore(true);
    void load(entries.length);
  }

  function resetFilters() {
    setLevel('');
    setWorker('');
    setQuery('');
    // The setters above only land on the next render, and this closure's
    // `load` still sees the old state — pass the cleared values explicitly.
    setRefreshing(true);
    void load(0, { level: '', worker: '', query: '' });
  }

  function onEnterApply(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault();
      refresh();
    }
  }

  const hasMore = entries.length < total;

  return (
    <SectionCard
      title="Worker logs"
      description="Recent output from the processing worker containers."
      actions={
        <>
          <Toggle checked={autoRefresh} onChange={setAutoRefresh} label="Auto-refresh" />
          <Button variant="outline" size="sm" onClick={refresh} disabled={refreshing}>
            {refreshing ? (
              <LoaderCircle className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCcw className="h-4 w-4" />
            )}
            Refresh
          </Button>
        </>
      }
    >
      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-xl border border-slate-100 bg-slate-50/60 p-3">
        <div className="w-36">
          <Field label="Level">
            <select
              value={level}
              onChange={(e) => setLevel(e.target.value as LogLevel | '')}
              className={inputClass}
            >
              <option value="">All levels</option>
              <option value="CRITICAL">Critical</option>
              <option value="ERROR">Error</option>
              <option value="WARNING">Warning</option>
              <option value="INFO">Info</option>
              <option value="DEBUG">Debug</option>
            </select>
          </Field>
        </div>
        <div className="w-48">
          <Field label="Worker">
            <input
              value={worker}
              onChange={(e) => setWorker(e.target.value)}
              onKeyDown={onEnterApply}
              placeholder="Pod / container name"
              className={inputClass}
            />
          </Field>
        </div>
        <div className="min-w-[12rem] flex-1">
          <Field label="Search">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onEnterApply}
              placeholder="Filter message text…"
              className={inputClass}
            />
          </Field>
        </div>
        <div className="flex gap-2">
          <Button size="sm" onClick={refresh}>
            Apply filters
          </Button>
          <Button variant="outline" size="sm" onClick={resetFilters} disabled={!hasFilters}>
            Reset
          </Button>
        </div>
      </div>

      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
        <span>{total > 0 ? `Showing ${entries.length} of ${total} entries` : ''}</span>
        <span>{lastFetchedAt ? `Updated ${lastFetchedAt.toLocaleTimeString()}` : ''}</span>
      </div>

      <ErrorNotice message={error} />
      {unavailable && (
        <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          Worker logs are not available on this backend yet. This tab starts showing data
          automatically once the endpoint is deployed.
        </div>
      )}

      {loading ? (
        <LoadingState label="Loading logs…" />
      ) : unavailable ? null : entries.length === 0 ? (
        <p className="py-8 text-center text-sm text-slate-500">
          {hasFilters ? 'No log entries match the current filters.' : 'No log entries yet.'}
        </p>
      ) : (
        <>
          <div className="max-h-[32rem] overflow-y-auto overflow-x-hidden rounded-2xl border border-slate-800 bg-slate-950 p-4 font-mono text-xs">
            {entries.map((entry) => (
              <LogRow key={entry.id} entry={entry} />
            ))}
          </div>
          {hasMore && (
            <div className="mt-3 flex justify-center">
              <Button variant="outline" size="sm" onClick={loadMore} disabled={loadingMore}>
                {loadingMore && <LoaderCircle className="h-4 w-4 animate-spin" />}
                Load older
              </Button>
            </div>
          )}
        </>
      )}
    </SectionCard>
  );
}

function LogRow({ entry }: { entry: WorkerLogEntry }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="border-b border-slate-900/60 py-1.5 last:border-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <span className="shrink-0 text-slate-500">
          {new Date(entry.created_at).toLocaleTimeString()}
        </span>
        <span
          className={`shrink-0 w-20 font-semibold ${LEVEL_COLORS[entry.level] ?? 'text-slate-300'}`}
        >
          {entry.level}
        </span>
        <span
          className={`shrink-0 rounded px-1.5 ${workerColor(entry.worker_name)}`}
          title={entry.worker_name}
        >
          {entry.worker_name}
        </span>
        {entry.task_name && <span className="shrink-0 text-slate-500">[{entry.task_name}]</span>}
        <span className="whitespace-pre-wrap break-all text-slate-200">{entry.message}</span>
        {entry.exc_text && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="shrink-0 rounded px-1.5 text-[11px] font-semibold text-red-400 underline decoration-dotted hover:text-red-300"
          >
            {expanded ? 'Hide traceback' : 'Show traceback'}
          </button>
        )}
      </div>
      {expanded && entry.exc_text && (
        <pre className="mt-1 overflow-x-auto rounded-lg bg-slate-900 p-2 text-[11px] text-red-300">
          {entry.exc_text}
        </pre>
      )}
    </div>
  );
}
