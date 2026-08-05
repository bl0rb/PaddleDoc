'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ChevronDown, ChevronRight, LoaderCircle } from 'lucide-react';
import { useParams } from 'next/navigation';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import { formatBytes } from '@/components/dashboard/shared';
import {
  type ImportRunCancelResponse,
  type ImportRunDetail,
  importJobStatusChip,
  isRunActive,
  runStatusChip,
  runTitle,
} from '@/lib/imports';

const POLL_INTERVAL_MS = 2500;

type JobDetailForFolder = {
  processing_info?: {
    settings?: {
      folder?: string | null;
      subfolder?: string | null;
    } | null;
  } | null;
};

export default function ImportRunPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;

  const [run, setRun] = useState<ImportRunDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [errorsOpen, setErrorsOpen] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelMessage, setCancelMessage] = useState<string | null>(null);
  const [jobsHref, setJobsHref] = useState('/jobs');

  // Poll the run detail every 2.5 s while pending/running; the timeout chain
  // ends on terminal status and is cleared on unmount.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const detail = await apiJson<ImportRunDetail>(`/api/v1/import/runs/${runId}`, { cache: 'no-store' });
        if (cancelled) return;
        setRun(detail);
        setLoadError(null);
        if (isRunActive(detail.status)) {
          timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setNotFound(true);
          return;
        }
        setLoadError(error instanceof ApiError ? error.detail : 'Failed to load the import run.');
        // Transient failure: keep polling so a recovering backend resumes updates.
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [runId]);

  // "View in Jobs" filtered to the run's folder: the run payload does not
  // carry its options, so read folder/subfolder off the first created job.
  useEffect(() => {
    if (!run || isRunActive(run.status) || run.jobs.length === 0) return;
    let cancelled = false;
    const resolveFolder = async () => {
      try {
        const job = await apiJson<JobDetailForFolder>(`/api/v1/jobs/${run.jobs[0].id}`, { cache: 'no-store' });
        if (cancelled) return;
        const settings = job.processing_info?.settings;
        const folder = (settings?.folder ?? '').trim();
        const subfolder = (settings?.subfolder ?? '').trim();
        const path = [folder, subfolder].filter(Boolean).join('/');
        if (path) setJobsHref(`/jobs?folder=${encodeURIComponent(path)}`);
      } catch {
        // Fall back to the unfiltered jobs page.
      }
    };
    void resolveFolder();
    return () => {
      cancelled = true;
    };
  }, [run]);

  const cancelRun = async () => {
    if (!run) return;
    if (!window.confirm('Cancel this import run? Pages imported so far are kept.')) return;
    setCancelBusy(true);
    setCancelMessage(null);
    try {
      const result = await apiJson<ImportRunCancelResponse>(`/api/v1/import/runs/${run.id}/cancel`, {
        method: 'POST',
      });
      setRun((current) =>
        current ? { ...current, status: result.status, cancel_requested: result.cancel_requested } : current,
      );
      if (result.status === 'running' && result.cancel_requested) {
        setCancelMessage('Cancellation requested — the worker stops after the current page.');
      }
    } catch (error) {
      setCancelMessage(error instanceof ApiError ? error.detail : 'Failed to cancel the run.');
    } finally {
      setCancelBusy(false);
    }
  };

  if (notFound) {
    return (
      <main className="min-h-screen">
        <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
          <h1 className="text-2xl font-semibold">Import run not found</h1>
          <p className="mt-2 text-sm text-slate-600">The run does not exist or is not visible to you.</p>
          <Link href="/imports" className="mt-4 inline-block text-sm text-emerald-700 hover:text-emerald-800">
            Back to imports
          </Link>
        </div>
      </main>
    );
  }

  if (!run) {
    return (
      <main className="min-h-screen">
        <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
          <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
            <LoaderCircle className="h-4 w-4 animate-spin" /> Loading import run...
          </div>
          {loadError && <p className="text-sm text-red-600">{loadError}</p>}
        </div>
      </main>
    );
  }

  const active = isRunActive(run.status);
  // The API caps discovery at the run's max_pages, so pages_discovered is
  // already min(discovered, max_pages); it is the poll-time denominator.
  const denominator = Math.max(run.pages_discovered, 1);
  const progressPct = Math.min(100, Math.round((run.pages_imported / denominator) * 100));
  const totalBytes = run.artifact_bytes + run.content_bytes;

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.16em] text-slate-500">Confluence import</p>
            <h1 className="mt-1 truncate text-2xl font-semibold">{runTitle(run)}</h1>
            <p className="mt-1 text-sm text-slate-600">
              {run.scope_type === 'space' ? `Space key: ${run.scope_value}` : `Page id: ${run.scope_value}`}
              {run.owner ? ` · Started by ${run.owner.username}` : ''}
              {` · ${new Date(run.created_at).toLocaleString()}`}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className={`rounded px-2 py-1 text-xs ${runStatusChip[run.status]}`}>{run.status}</span>
            {active && (
              <Button variant="outline" size="sm" onClick={() => void cancelRun()} disabled={cancelBusy || run.cancel_requested}>
                {cancelBusy ? 'Cancelling...' : run.cancel_requested ? 'Cancelling' : 'Cancel'}
              </Button>
            )}
          </div>
        </section>

        {loadError && <p className="mb-4 text-sm text-amber-700">{loadError} Retrying...</p>}
        {cancelMessage && <p className="mb-4 text-sm text-slate-600">{cancelMessage}</p>}
        {run.error_message && (
          <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {run.error_message}
          </p>
        )}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm font-semibold text-slate-950">
              {run.pages_imported} of {run.pages_discovered} discovered page(s) imported
            </p>
            <p className="text-xs font-semibold text-slate-600">{progressPct}%</p>
          </div>
          <div
            className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100"
            role="progressbar"
            aria-label="Pages imported"
            aria-valuemin={0}
            aria-valuemax={run.pages_discovered}
            aria-valuenow={run.pages_imported}
          >
            <div className="h-full rounded-full bg-emerald-500 transition-all" style={{ width: `${progressPct}%` }} />
          </div>
          {active && run.current_page_title && (
            <p className="mt-2 flex items-center gap-2 text-xs text-slate-500">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> Current page: {run.current_page_title}
            </p>
          )}
          <div className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
              <p className="text-xs text-slate-500">Pages</p>
              <p className="font-semibold text-slate-950">
                {run.pages_imported} / {run.pages_discovered}
              </p>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
              <p className="text-xs text-slate-500">Failed pages</p>
              <p className={`font-semibold ${run.pages_failed > 0 ? 'text-red-600' : 'text-slate-950'}`}>
                {run.pages_failed}
              </p>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
              <p className="text-xs text-slate-500">Attachments</p>
              <p className="font-semibold text-slate-950">{run.attachments_saved}</p>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
              <p className="text-xs text-slate-500">Stored bytes</p>
              <p className="font-semibold text-slate-950">{formatBytes(totalBytes)}</p>
            </div>
          </div>
          {run.status === 'finished' && (
            <div className="mt-4">
              <Link href={jobsHref}>
                <Button>View in Jobs</Button>
              </Link>
            </div>
          )}
        </section>

        {run.errors.length > 0 && (
          <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
            <button
              type="button"
              onClick={() => setErrorsOpen((value) => !value)}
              aria-expanded={errorsOpen}
              className="flex w-full items-center justify-between text-left"
            >
              <span className="flex items-center gap-2 text-sm font-semibold text-slate-950">
                {errorsOpen ? (
                  <ChevronDown className="h-4 w-4 text-slate-500" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-slate-500" />
                )}
                Page errors and skips
              </span>
              <span className="text-xs text-slate-500">{run.errors.length} entr{run.errors.length === 1 ? 'y' : 'ies'}</span>
            </button>
            {errorsOpen && (
              <ul className="mt-3 space-y-2">
                {run.errors.map((entry, index) => (
                  <li
                    key={`${entry.page_id}-${index}`}
                    className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
                  >
                    <p className="font-semibold">{entry.title || (entry.page_id ? `Page ${entry.page_id}` : 'Run note')}</p>
                    <p className="mt-0.5">{entry.error}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Imported jobs</h2>
            <p className="text-sm text-slate-500">{run.jobs.length} job(s)</p>
          </div>
          {run.jobs.length === 0 ? (
            <p className="py-4 text-sm text-slate-600">
              {active ? 'Jobs appear here as pages are imported.' : 'This run created no jobs.'}
            </p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {run.jobs.map((job) => (
                <li key={job.id} className="flex items-center justify-between gap-3 py-2.5">
                  <Link
                    href={`/jobs/${job.id}`}
                    className="min-w-0 flex-1 truncate text-sm font-medium text-slate-950 hover:text-emerald-700"
                  >
                    {job.title}
                  </Link>
                  <span className={`shrink-0 rounded px-2 py-1 text-xs ${importJobStatusChip[job.status]}`}>
                    {job.status}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <p className="mt-6">
          <Link href="/imports" className="text-sm text-emerald-700 hover:text-emerald-800">
            Back to imports
          </Link>
        </p>
      </div>
    </main>
  );
}
