'use client';

import { memo, startTransition, useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
import Link from 'next/link';

import { Button } from '@/components/ui/button';
import {
  API,
  type ContainerState,
  type DashboardStats,
  type Job,
  type PaddleIndicator,
  type PaddleStatusResponse,
  type RuntimeCapabilityInfo,
  type UIState,
  formatBytes,
} from './shared';

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
};

const HeroPanel = memo(function HeroPanel({
  uiState,
  service,
}: {
  uiState: UIState;
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
              Upload files, process them automated and access cool markdown outputs. PaddleDock is your dashboard for document processing, powered by the open-source PaddleOCR engine.
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <Link href="/jobs">
                <Button>Go to jobs</Button>
              </Link>
              <Link href="/processing#upload-flow">
                <Button variant="outline">Go to upload flow</Button>
              </Link>
            </div>
          </div>
        </div>
        <div className="border-t border-slate-100 bg-gradient-to-br from-emerald-700 via-emerald-800 to-slate-900 p-6 text-white lg:border-l lg:border-t-0 lg:p-8">
          <div className="flex h-full flex-col justify-between gap-6">
            <div>
              <p className="text-sm uppercase tracking-[0.2em] text-emerald-100/80">PaddleDock</p>
              <h2 className="mt-3 text-2xl font-semibold">Document processing</h2>
              <p className="mt-3 text-sm leading-6 text-emerald-50/85">
                Status: {uiState}
              </p>
              <p className="mt-2 text-sm leading-6 text-emerald-50/85">
                Paddle Service: {paddleStatus}
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
              {paddleStatusDetail && (
                <p className="mt-2 text-xs leading-5 text-emerald-100/70">{paddleStatusDetail}</p>
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

const StatsGrid = memo(function StatsGrid({ stats }: { stats: DashboardStats | null }) {
  const cards = [
    { label: 'Processed documents', value: stats?.processed_documents ?? '...', hint: 'Finished jobs' },
    { label: 'Processed pages', value: stats?.processed_pages ?? '...', hint: 'Total page count' },
    { label: 'Errors', value: stats?.errors ?? '...', hint: 'Jobs with status FAILED' },
    { label: 'Database size', value: formatBytes(stats?.database_size_bytes ?? null), hint: 'Current storage usage' },
  ];

  return (
    <section className="mb-8 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
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
    </section>
  );
});

export function HomeDashboard() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [service, setService] = useState<ServiceSnapshot>(initialSnapshot);

  useEffect(() => {
    const refreshJobs = async () => {
      const response = await fetch(`${API}/api/v1/jobs`, { cache: 'no-store' });
      if (!response.ok) return;
      const payload = await response.json();
      const items = (payload.items ?? []) as Job[];
      startTransition(() => setJobs(items));
    };

    const refreshStats = async () => {
      const response = await fetch(`${API}/api/v1/stats`, { cache: 'no-store' });
      if (!response.ok) return;
      const payload = (await response.json()) as DashboardStats;
      startTransition(() => setStats(payload));
    };

    const refreshPaddleStatus = async () => {
      const response = await fetch(`${API}/api/v1/paddle/status`, { cache: 'no-store' });
      if (!response.ok) {
        startTransition(() => setService(initialSnapshot));
        return;
      }
      const payload = (await response.json()) as PaddleStatusResponse;
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
      startTransition(() => {
        setService({
          paddleStatus: payload.status ?? 'failed',
          paddleStatusDetail: payload.detail ?? null,
          pendingJobs: payload.pending_jobs ?? 0,
          runningJobs: payload.running_jobs ?? 0,
          queueTotal: payload.queue_total ?? 0,
          runningWorkers: payload.running_workers ?? 0,
          workerNodes: payload.worker_nodes ?? [],
          containerStates,
          runtimeCapability: payload.runtime ?? null,
        });
      });
    };

    queueMicrotask(() => {
      void refreshJobs();
      void refreshStats();
      void refreshPaddleStatus();
    });

    const jobsInterval = setInterval(() => void refreshJobs(), 15000);
    const statsInterval = setInterval(() => void refreshStats(), 30000);
    const statusInterval = setInterval(() => void refreshPaddleStatus(), 30000);
    return () => {
      clearInterval(jobsInterval);
      clearInterval(statsInterval);
      clearInterval(statusInterval);
    };
  }, []);

  const uiState = deriveUiState(jobs);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 text-slate-950 sm:px-6 lg:px-8">
      <HeroPanel uiState={uiState} service={service} />
      <StatsGrid stats={stats} />
    </div>
  );
}
