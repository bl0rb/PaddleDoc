'use client';

import { memo, useCallback, useMemo } from 'react';
import { Sparkles } from 'lucide-react';
import Link from 'next/link';

import { Button } from '@/components/ui/button';
import { apiFetch } from '@/lib/api';
import { useCachedResource, useVisiblePolling } from '@/lib/data-cache';
import {
  type ContainerState,
  type DashboardStats,
  type Job,
  type PaddleIndicator,
  type PaddleStatusResponse,
  type RuntimeCapabilityInfo,
  type UIState,
  formatBytes,
} from './shared';

const JOBS_KEY = '/api/v1/jobs';
const STATS_KEY = '/api/v1/stats';
const PADDLE_STATUS_KEY = '/api/v1/paddle/status';

function deriveUiState(jobs: Job[]): UIState {
  if (jobs.some((job) => job.status === 'RUNNING' || job.status === 'PENDING')) {
    return 'Processing';
  }
  if (jobs.some((job) => job.status === 'FINISHED')) {
    return 'Finished';
  }
  return 'Idle';
}

type ServiceSnapshot = {
  paddleStatus: PaddleIndicator;
  paddleStatusDetail: string | null;
  pendingJobs: number;
  runningJobs: number;
  queueTotal: number;
  runningWorkers: number;
  workerNodes: string[];
  containerStates: ContainerState[];
  runtimeCapability: RuntimeCapabilityInfo | null;
  /** True when the latest paddle-status revalidate failed — the fields above are last-known-good, not confirmed current. */
  isUnreachable: boolean;
};

const initialSnapshot: ServiceSnapshot = {
  paddleStatus: 'stopped',
  paddleStatusDetail: null,
  pendingJobs: 0,
  runningJobs: 0,
  queueTotal: 0,
  runningWorkers: 0,
  workerNodes: [],
  containerStates: [],
  runtimeCapability: null,
  isUnreachable: false,
};

const HeroPanel = memo(function HeroPanel({
  uiState,
  jobsStale,
  service,
}: {
  uiState: UIState;
  jobsStale: boolean;
  service: ServiceSnapshot;
}) {
  const {
    paddleStatus,
    paddleStatusDetail,
    pendingJobs,
    runningJobs,
    queueTotal,
    runningWorkers,
    workerNodes,
    containerStates,
    runtimeCapability,
    isUnreachable,
  } = service;

  return (
    <section className="mb-8 overflow-hidden rounded-[2rem] border border-emerald-100 bg-white shadow-[0_24px_70px_rgba(15,23,42,0.08)]">
      <div className="grid gap-0 lg:grid-cols-[1.35fr_0.65fr]">
        <div className="relative p-6 sm:p-8 lg:p-10">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_right,rgba(14,116,144,0.08),transparent_28%),radial-gradient(circle_at_bottom_left,rgba(249,115,22,0.08),transparent_24%)]" />
          <div className="relative max-w-2xl">
            <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-emerald-100 bg-emerald-50 px-3 py-1 text-xs font-semibold uppercase tracking-[0.24em] text-emerald-800">
              <Sparkles className="h-3.5 w-3.5" />
              Document Magic
            </div>
            <h1 className="font-serif text-4xl font-semibold tracking-tight text-slate-950 sm:text-5xl">
              Your documents, supercharged with PaddleOCR
            </h1>
            <p className="mt-4 max-w-xl text-base leading-7 text-slate-600 sm:text-lg">
              Upload files, process them automated and access cool markdown outputs. PaddleDoc is your dashboard for document processing, powered by the open-source PaddleOCR engine.
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <Link href="/jobs">
                <Button>Tasks</Button>
              </Link>
            </div>
          </div>
        </div>
        <div className="border-t border-slate-100 bg-gradient-to-br from-emerald-700 via-emerald-800 to-slate-900 p-6 text-white lg:border-l lg:border-t-0 lg:p-8">
          <div className="flex h-full flex-col justify-between gap-6">
            <div>
              <p className="text-sm uppercase tracking-[0.2em] text-emerald-100/80">PaddleDoc</p>
              <h2 className="mt-3 text-2xl font-semibold">Document processing</h2>
              {isUnreachable && (
                <div className="mt-3 inline-flex items-center gap-2 rounded-full border border-red-400/40 bg-red-500/15 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-red-100">
                  <span className="h-1.5 w-1.5 rounded-full bg-red-400" />
                  Connection lost — showing last known status
                </div>
              )}
              <p className="mt-3 text-sm leading-6 text-emerald-50/85">
                Status: {uiState}
                {jobsStale && <span className="ml-2 text-amber-200">(last known — reconnecting)</span>}
              </p>
              <p className="mt-2 text-sm leading-6 text-emerald-50/85">
                Paddle Service: {isUnreachable ? 'unreachable' : paddleStatus}
              </p>
              <p className="mt-2 text-sm leading-6 text-emerald-50/85">
                Queue remaining: {queueTotal} (pending {pendingJobs}, running {runningJobs})
              </p>
              <p className="mt-2 text-sm leading-6 text-emerald-50/85">
                Running containers: {runningWorkers}
              </p>
              {containerStates.length > 0 && (
                <div className="mt-2 space-y-1 text-xs leading-5 text-emerald-100/80">
                  {containerStates.map((entry) => (
                    <p key={entry.name}>
                      {entry.name}: {entry.state}
                      {entry.detail ? ` (${entry.detail})` : ''}
                    </p>
                  ))}
                </div>
              )}
              {workerNodes.length > 0 && (
                <p className="mt-1 text-xs leading-5 text-emerald-100/70">
                  {workerNodes.join(', ')}
                </p>
              )}
              {isUnreachable ? (
                <p className="mt-2 text-xs leading-5 text-red-200">
                  The backend did not respond to the last health check. Queue, container and worker
                  figures above reflect the last successful check, not the current state.
                </p>
              ) : (
                paddleStatusDetail && (
                  <p className="mt-2 text-xs leading-5 text-emerald-100/70">{paddleStatusDetail}</p>
                )
              )}
              <p className="mt-2 text-xs leading-5 text-emerald-100/70">
                {runtimeCapability?.cuda_available
                  ? 'GPU available for accelerated processing'
                  : 'PaddleOCR runtime is configured for CPU execution in this deployment'}
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
});

const StatsGrid = memo(function StatsGrid({ stats, isStale }: { stats: DashboardStats | null; isStale: boolean }) {
  const cards = [
    { label: 'Processed documents', value: stats?.processed_documents ?? '...', hint: 'Finished jobs' },
    { label: 'Processed pages', value: stats?.processed_pages ?? '...', hint: 'Total page count' },
    { label: 'Errors', value: stats?.errors ?? '...', hint: 'Jobs with status FAILED' },
    { label: 'Database size', value: formatBytes(stats?.database_size_bytes ?? null), hint: 'Current storage usage' },
  ];

  return (
    <section className="mb-8">
      {isStale && (
        <div className="mb-3 flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />
          Showing last known stats — reconnecting to the backend.
        </div>
      )}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {cards.map((item) => (
          <div
            key={item.label}
            className="rounded-2xl border border-slate-200 bg-gradient-to-br from-emerald-50 to-white p-5 shadow-[0_18px_45px_rgba(15,23,42,0.06)]"
          >
            <p className="text-sm text-slate-600">{item.label}</p>
            <p className="mt-3 text-3xl font-semibold text-slate-950">{item.value}</p>
            <p className="mt-2 text-xs text-slate-500">{item.hint}</p>
          </div>
        ))}
      </div>
    </section>
  );
});

async function fetchJson<T>(path: string): Promise<T> {
  const response = await apiFetch(path, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

export function HomeDashboard() {
  const fetchJobs = useCallback(async () => {
    const payload = await fetchJson<{ items?: Job[] }>(JOBS_KEY);
    return (payload.items ?? []) as Job[];
  }, []);
  const fetchStats = useCallback(() => fetchJson<DashboardStats>(STATS_KEY), []);
  const fetchPaddleStatus = useCallback(() => fetchJson<PaddleStatusResponse>(PADDLE_STATUS_KEY), []);

  // ttlMs is short: these views are cheap to refetch and jobs/stats/status
  // can change under a running pipeline, so a mount should rarely trust a
  // value older than one polling tick — the cache's job here is instant
  // repaint on remount + shared in-flight dedupe, not long-lived staleness.
  const jobsResource = useCachedResource(JOBS_KEY, fetchJobs, { ttlMs: 5_000 });
  const statsResource = useCachedResource(STATS_KEY, fetchStats, { ttlMs: 10_000 });
  const paddleStatusResource = useCachedResource(PADDLE_STATUS_KEY, fetchPaddleStatus, { ttlMs: 10_000 });

  const jobs = jobsResource.data ?? [];
  const stats = statsResource.data ?? null;
  const uiState = deriveUiState(jobs);
  const isActive = uiState === 'Processing';

  // A failed revalidate leaves the cache (and therefore `.data`) holding the
  // last-known-good payload on purpose — that's the whole point of the
  // stale-while-revalidate cache. But this panel reports live service
  // health, so it must not silently keep presenting that stale payload as
  // current: `.error` tells us the most recent check actually failed, and
  // the panel below degrades honestly instead of freezing on the last good
  // reading.
  const paddleUnreachable = Boolean(paddleStatusResource.error);
  const jobsStale = Boolean(jobsResource.error);

  const service = useMemo<ServiceSnapshot>(() => {
    const payload = paddleStatusResource.data;
    if (!payload) {
      return { ...initialSnapshot, isUnreachable: paddleUnreachable };
    }
    const reportedContainers = payload.containers ?? [];
    const hasFrontend = reportedContainers.some((entry) => entry.name === 'frontend');
    const containerStates: ContainerState[] = hasFrontend
      ? reportedContainers.map((entry) =>
          entry.name === 'frontend'
            ? { ...entry, state: 'running', detail: 'Served in current browser session' }
            : entry,
        )
      : [
          { name: 'frontend', state: 'running', detail: 'Served in current browser session' },
          ...reportedContainers,
        ];
    return {
      paddleStatus: payload.status ?? 'failed',
      paddleStatusDetail: payload.detail ?? null,
      pendingJobs: payload.pending_jobs ?? 0,
      runningJobs: payload.running_jobs ?? 0,
      queueTotal: payload.queue_total ?? 0,
      runningWorkers: payload.running_workers ?? 0,
      workerNodes: payload.worker_nodes ?? [],
      containerStates,
      runtimeCapability: payload.runtime ?? null,
      isUnreachable: paddleUnreachable,
    };
  }, [paddleStatusResource.data, paddleUnreachable]);

  // Poll briskly while a job is actually queued/running, back off to a slow
  // heartbeat once idle, and pause outright while the tab is hidden — no
  // point re-rendering a dashboard nobody is looking at.
  useVisiblePolling(() => void jobsResource.revalidate(), isActive ? 5_000 : 30_000);
  useVisiblePolling(() => void statsResource.revalidate(), isActive ? 15_000 : 60_000);
  useVisiblePolling(() => void paddleStatusResource.revalidate(), isActive ? 10_000 : 45_000);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 text-slate-950 sm:px-6 lg:px-8">
      <HeroPanel uiState={uiState} jobsStale={jobsStale} service={service} />
      <StatsGrid stats={stats} isStale={statsResource.isStale} />
    </div>
  );
}
