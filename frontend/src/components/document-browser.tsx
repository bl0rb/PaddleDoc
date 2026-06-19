'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { Download, LoaderCircle, RefreshCcw, RotateCcw, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';

type JobStatus = 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';

type Job = {
  id: string;
  original_filename: string;
  status: JobStatus;
  tags: string[];
  error_message?: string | null;
  processing_info?: {
    settings?: Record<string, unknown>;
    execution?: Record<string, unknown>;
    editor?: Record<string, unknown>;
  } | null;
  created_at: string;
  updated_at?: string;
};

type SortKey = 'document' | 'status' | 'profile' | 'pages' | 'created';
type SortDirection = 'asc' | 'desc';

type DocumentBrowserProps = {
  title: string;
  description: string;
  endpoint: 'jobs' | 'search';
  allowDelete?: boolean;
  includeDateFilters?: boolean;
  compact?: boolean;
  hideHeader?: boolean;
};

const API = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

const statusBadge: Record<JobStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-emerald-100 text-emerald-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-600/20 text-red-300',
};

const LOWER_PROFILE_RETRY_MAP: Record<string, string> = {
  ppocrv6_medium_structurev3: 'ppocrv6_small_structurev3',
  ppocrv6_small_structurev3: 'ppocrv6_tiny_structurev3',
  ppocrv6_medium: 'ppocrv6_tiny',
  ppocrv6_small: 'ppocrv6_tiny',
};

function pageCountForJob(job: Job): string {
  const execution = job.processing_info?.execution;
  const direct = execution?.page_count;
  if (typeof direct === 'number') {
    return String(direct);
  }
  const structure = execution?.structure;
  const nested = typeof structure === 'object' && structure !== null ? (structure as Record<string, unknown>).page_count : null;
  if (typeof nested === 'number') {
    return String(nested);
  }
  return '-';
}

function jobFolderPath(job: Job): string {
  const settings = job.processing_info?.settings;
  const folder = typeof settings?.folder === 'string' ? settings.folder.trim() : '';
  const subfolder = typeof settings?.subfolder === 'string' ? settings.subfolder.trim() : '';
  if (folder || subfolder) {
    return [folder, subfolder].filter(Boolean).join('/');
  }

  const storageFolder = typeof settings?.storage_folder === 'string' ? settings.storage_folder.trim() : '';
  if (!storageFolder) {
    return 'inbox';
  }
  const parts = storageFolder.split('/').filter(Boolean);
  if (parts.length <= 1) {
    return 'inbox';
  }
  return parts.slice(0, -1).join('/');
}

function profileForJob(job: Job): string {
  const settings = job.processing_info?.settings;
  const execution = job.processing_info?.execution;
  const executionProfile = typeof execution?.profile_id === 'string' ? execution.profile_id : '';
  if (executionProfile) {
    return executionProfile;
  }
  const configuredProfile = typeof settings?.profile_id === 'string' ? settings.profile_id : '';
  if (configuredProfile) {
    return configuredProfile;
  }
  const requestedProfile = typeof settings?.requested_profile_id === 'string' ? settings.requested_profile_id : '';
  return requestedProfile || '-';
}

export function DocumentBrowser({
  title,
  description,
  endpoint,
  allowDelete = false,
  includeDateFilters = true,
  compact = false,
  hideHeader = false,
}: DocumentBrowserProps) {
  const pageSize = 50;
  const [items, setItems] = useState<Job[]>([]);
  const [query, setQuery] = useState('');
  const [tag, setTag] = useState('');
  const [statusFilter, setStatusFilter] = useState<JobStatus | ''>('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [loading, setLoading] = useState(false);
  const [restartingPending, setRestartingPending] = useState(false);
  const [selectedFolder, setSelectedFolder] = useState<string>('all');
  const [deletingFolder, setDeletingFolder] = useState<string | null>(null);
  const [downloadingFolder, setDownloadingFolder] = useState<string | null>(null);
  const [restartingFolder, setRestartingFolder] = useState<string | null>(null);
  const [restartingJobId, setRestartingJobId] = useState<string | null>(null);
  const [retryingLowerJobId, setRetryingLowerJobId] = useState<string | null>(null);
  const [protectedJobId, setProtectedJobId] = useState<string | null>(null);
  const [protectedJobPassword, setProtectedJobPassword] = useState('');
  const [passwordAttempt, setPasswordAttempt] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('created');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [currentPage, setCurrentPage] = useState(1);

  const markJobsQueued = (predicate: (job: Job) => boolean) => {
    setItems((current) =>
      current.map((job) => {
        if (!predicate(job)) {
          return job;
        }

        const nextInfo = job.processing_info ? { ...job.processing_info } : {};
        const nextExecution =
          nextInfo.execution && typeof nextInfo.execution === 'object'
            ? { ...nextInfo.execution }
            : {};
        nextExecution.status = 'requeued';
        nextInfo.execution = nextExecution;

        return {
          ...job,
          status: 'PENDING',
          error_message: null,
          processing_info: nextInfo,
        };
      }),
    );
  };

  const loadItems = async () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (query.trim()) {
      params.set('q', query.trim());
    }
    if (tag.trim()) {
      params.set('tag', tag.trim());
    }
    if (statusFilter) {
      params.set('status', statusFilter);
    }
    if (includeDateFilters && fromDate) {
      params.set('from_date', fromDate);
    }
    if (includeDateFilters && toDate) {
      params.set('to_date', toDate);
    }

    const response = await fetch(`${API}/api/v1/${endpoint}${params.toString() ? `?${params.toString()}` : ''}`, {
      cache: 'no-store',
    });
    if (response.ok) {
      const payload = await response.json();
      setItems(payload.items ?? []);
    }
    setLoading(false);
  };

  useEffect(() => {
    const run = async () => {
      const response = await fetch(`${API}/api/v1/${endpoint}`, {
        cache: 'no-store',
      });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      setItems(payload.items ?? []);
    };
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const removeJob = async (id: string, password?: string) => {
    const url = new URL(`${API}/api/v1/jobs/${id}`);
    if (password) {
      url.searchParams.set('password', password);
    }
    const response = await fetch(url, { method: 'DELETE' });
    if (response.status === 401) {
      setProtectedJobId(id);
      setProtectedJobPassword('');
      return;
    }
    if (!response.ok) {
      alert('Failed to delete job');
      return;
    }
    setProtectedJobId(null);
    await loadItems();
  };

  const removeFolder = async (folderPath: string) => {
    setDeletingFolder(folderPath);
    const url = new URL(`${API}/api/v1/folders/${encodeURI(folderPath)}`);
    await fetch(url, { method: 'DELETE' });
    if (selectedFolder === folderPath || selectedFolder.startsWith(`${folderPath}/`)) {
      setSelectedFolder('all');
    }
    await loadItems();
    setDeletingFolder(null);
  };

  const downloadFolder = async (folderPath: string) => {
    setDownloadingFolder(folderPath);
    try {
      const response = await fetch(`${API}/api/v1/folders/${encodeURI(folderPath)}/download`);
      if (!response.ok) {
        alert('No downloadable markdown files found in this folder.');
        return;
      }

      const blob = await response.blob();
      const href = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = `${folderPath.replaceAll('/', '_')}-markdown.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(href);
    } finally {
      setDownloadingFolder(null);
    }
  };

  const restartFolder = async (folderPath: string) => {
    if (endpoint !== 'jobs') {
      return;
    }
    setRestartingFolder(folderPath);
    try {
      const response = await fetch(`${API}/api/v1/folders/${encodeURI(folderPath)}/restart`, { method: 'POST' });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to restart folder jobs.';
        alert(detail);
        return;
      }
      const payload = await response.json().catch(() => ({}));
      if (typeof payload?.restarted_jobs === 'number') {
        alert(`Restarted ${payload.restarted_jobs} job(s) in folder.`);
      }
      markJobsQueued((job) => {
        const path = jobFolderPath(job);
        return path === folderPath || path.startsWith(`${folderPath}/`);
      });
      await loadItems();
    } finally {
      setRestartingFolder(null);
    }
  };

  const handlePasswordSubmit = async () => {
    if (!protectedJobId) return;
    await removeJob(protectedJobId, protectedJobPassword);
  };

  const restartPendingJobs = async () => {
    if (endpoint !== 'jobs') {
      return;
    }
    setRestartingPending(true);
    try {
      const response = await fetch(`${API}/api/v1/jobs/restart-pending`, { method: 'POST' });
      if (!response.ok) {
        alert('Failed to restart pending jobs.');
        return;
      }
      const payload = await response.json();
      alert(`Restarted ${payload.queued_jobs ?? 0} pending job(s).`);
      await loadItems();
    } finally {
      setRestartingPending(false);
    }
  };

  const restartJob = async (jobId: string) => {
    if (endpoint !== 'jobs') {
      return;
    }
    setRestartingJobId(jobId);
    try {
      const response = await fetch(`${API}/api/v1/jobs/${jobId}/restart`, { method: 'POST' });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to restart job.';
        alert(detail);
        return;
      }
      markJobsQueued((job) => job.id === jobId);
      await loadItems();
    } finally {
      setRestartingJobId(null);
    }
  };

  const suggestedLowerProfile = (job: Job): string | null => {
    const execution = job.processing_info?.execution;
    const fromExecution = typeof execution?.suggested_profile_id === 'string' ? execution.suggested_profile_id : null;
    if (fromExecution) {
      return fromExecution;
    }
    const settings = job.processing_info?.settings;
    const currentProfile = typeof settings?.profile_id === 'string' ? settings.profile_id : null;
    if (!currentProfile) {
      return null;
    }
    return LOWER_PROFILE_RETRY_MAP[currentProfile] ?? null;
  };

  const retryJobLowerProfile = async (job: Job) => {
    if (endpoint !== 'jobs') {
      return;
    }
    const lowerProfile = suggestedLowerProfile(job);
    if (!lowerProfile) {
      alert('No lower profile available for this job.');
      return;
    }

    setRetryingLowerJobId(job.id);
    try {
      const response = await fetch(`${API}/api/v1/jobs/${job.id}/retry-lower-profile`, { method: 'POST' });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to retry with lower profile.';
        alert(detail);
        return;
      }
      markJobsQueued((item) => item.id === job.id);
      await loadItems();
    } finally {
      setRetryingLowerJobId(null);
    }
  };

  const folderItems = useMemo(() => {
    const counts = new Map<string, number>();
    for (const job of items) {
      const path = jobFolderPath(job);
      const parts = path.split('/').filter(Boolean);
      let current = '';
      for (const part of parts) {
        current = current ? `${current}/${part}` : part;
        counts.set(current, (counts.get(current) ?? 0) + 1);
      }
    }
    return Array.from(counts.entries())
      .map(([path, count]) => ({ path, count, depth: path.split('/').length - 1, name: path.split('/').pop() ?? path }))
      .sort((left, right) => left.path.localeCompare(right.path));
  }, [items]);

  const visibleItems = useMemo(() => {
    if (selectedFolder === 'all') {
      return items;
    }
    return items.filter((job) => {
      const folder = jobFolderPath(job);
      return folder === selectedFolder || folder.startsWith(`${selectedFolder}/`);
    });
  }, [items, selectedFolder]);

  const sortedItems = useMemo(() => {
    const sorted = [...visibleItems];
    sorted.sort((left, right) => {
      let comparison = 0;

      switch (sortKey) {
        case 'document':
          comparison = left.original_filename.localeCompare(right.original_filename, undefined, { sensitivity: 'base' });
          break;
        case 'status':
          comparison = left.status.localeCompare(right.status);
          break;
        case 'profile':
          comparison = profileForJob(left).localeCompare(profileForJob(right), undefined, { sensitivity: 'base' });
          break;
        case 'pages':
          comparison = Number(pageCountForJob(left)) - Number(pageCountForJob(right));
          break;
        case 'created':
          comparison = new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
          break;
      }

      if (comparison === 0) {
        comparison = left.original_filename.localeCompare(right.original_filename, undefined, { sensitivity: 'base' });
      }

      return sortDirection === 'asc' ? comparison : -comparison;
    });
    return sorted;
  }, [visibleItems, sortDirection, sortKey]);

  const totalPages = Math.max(1, Math.ceil(sortedItems.length / pageSize));
  const displayPage = Math.min(currentPage, totalPages);
  const paginatedItems = useMemo(() => {
    const start = (displayPage - 1) * pageSize;
    return sortedItems.slice(start, start + pageSize);
  }, [displayPage, pageSize, sortedItems]);

  const setSort = (nextKey: SortKey) => {
    setCurrentPage(1);
    if (sortKey === nextKey) {
      setSortDirection((current) => (current === 'asc' ? 'desc' : 'asc'));
      return;
    }
    setSortKey(nextKey);
    setSortDirection(nextKey === 'created' ? 'desc' : 'asc');
  };

  const sortIndicator = (key: SortKey) => {
    if (sortKey !== key) {
      return '';
    }
    return sortDirection === 'asc' ? ' ▲' : ' ▼';
  };

  return (
    <div className="w-full text-slate-900">

      <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="text-sm text-slate-700 xl:col-span-2">
            Search filename
            <input
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setCurrentPage(1);
              }}
              className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-300 focus:bg-white"
              placeholder="invoice, report, contract"
            />
          </label>
          <label className="text-sm text-slate-700">
            Tag filter
            <input
              value={tag}
              onChange={(event) => {
                setTag(event.target.value);
                setCurrentPage(1);
              }}
              className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-300 focus:bg-white"
              placeholder="finance"
            />
          </label>
          <label className="text-sm text-slate-700">
            Status
            <select
              value={statusFilter}
              onChange={(event) => {
                setStatusFilter(event.target.value as JobStatus | '');
                setCurrentPage(1);
              }}
              className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
            >
              <option value="">All</option>
              <option value="PENDING">PENDING</option>
              <option value="RUNNING">RUNNING</option>
              <option value="FINISHED">FINISHED</option>
              <option value="FAILED">FAILED</option>
            </select>
          </label>
          {includeDateFilters && (
            <>
              <label className="text-sm text-slate-700">
                From date
                <input
                  type="date"
                  value={fromDate}
                  onChange={(event) => {
                    setFromDate(event.target.value);
                    setCurrentPage(1);
                  }}
                  className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
                />
              </label>
              <label className="text-sm text-slate-700">
                To date
                <input
                  type="date"
                  value={toDate}
                  onChange={(event) => {
                    setToDate(event.target.value);
                    setCurrentPage(1);
                  }}
                  className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
                />
              </label>
            </>
          )}
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button onClick={loadItems}>Apply Filters</Button>
          <Button variant="outline" onClick={loadItems}>
            <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
          </Button>
          {endpoint === 'jobs' && (
            <Button variant="outline" onClick={restartPendingJobs} disabled={restartingPending}>
              {restartingPending ? 'Restarting...' : 'Restart pending jobs'}
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => {
              setQuery('');
              setTag('');
              setStatusFilter('');
              setFromDate('');
              setToDate('');
              window.setTimeout(() => void loadItems(), 0);
            }}
          >
            Reset
          </Button>
        </div>
      </section>

      <section className="grid gap-4 2xl:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="overflow-hidden rounded-3xl border border-slate-200 bg-white p-4 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-slate-700">Folders</h2>
          <div className="mt-3 space-y-1">
            <button
              type="button"
              onClick={() => {
                setSelectedFolder('all');
                setCurrentPage(1);
              }}
              className={`flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-sm ${
                selectedFolder === 'all' ? 'bg-emerald-50 text-emerald-900' : 'text-slate-700 hover:bg-slate-50'
              }`}
            >
              <span>All folders</span>
              <span className="text-xs text-slate-500">{items.length}</span>
            </button>
            {folderItems.map((folder) => (
              <div key={folder.path} className="flex flex-wrap items-center gap-1 sm:flex-nowrap">
                <button
                  type="button"
                  onClick={() => {
                    setSelectedFolder(folder.path);
                    setCurrentPage(1);
                  }}
                  className={`min-w-0 flex-1 rounded-lg px-2 py-1.5 text-left text-sm ${
                    selectedFolder === folder.path ? 'bg-emerald-50 text-emerald-900' : 'text-slate-700 hover:bg-slate-50'
                  }`}
                  style={{ paddingLeft: `${8 + folder.depth * 14}px` }}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate">{folder.name}</span>
                    <span className="shrink-0 text-xs text-slate-500">{folder.count}</span>
                  </span>
                </button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-8 w-8 shrink-0 px-0"
                  disabled={downloadingFolder === folder.path}
                  onClick={() => void downloadFolder(folder.path)}
                >
                  <Download className="h-4 w-4 text-emerald-700" />
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-8 w-8 shrink-0 px-0"
                  disabled={restartingFolder === folder.path}
                  onClick={() => void restartFolder(folder.path)}
                >
                  <RotateCcw className="h-4 w-4 text-slate-700" />
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-8 w-8 shrink-0 px-0"
                  disabled={deletingFolder === folder.path}
                  onClick={() => void removeFolder(folder.path)}
                >
                  <Trash2 className="h-4 w-4 text-red-600" />
                </Button>
              </div>
            ))}
          </div>
        </aside>

        <div className="min-w-0 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <h2 className="text-lg font-semibold">Results</h2>
            <p className="text-sm text-slate-500">
              {sortedItems.length} document(s) · Page {displayPage} / {totalPages}
            </p>
          </div>
          <div className="overflow-x-auto">
          <table className="w-full min-w-[700px] lg:min-w-[900px] text-left text-xs sm:text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">
                  <button type="button" className="font-medium hover:text-slate-800" onClick={() => setSort('document')}>
                    Document{sortIndicator('document')}
                  </button>
                </th>
                <th className="pb-2">
                  <button type="button" className="font-medium hover:text-slate-800" onClick={() => setSort('status')}>
                    Status{sortIndicator('status')}
                  </button>
                </th>
                <th className="pb-2">
                  <button type="button" className="font-medium hover:text-slate-800" onClick={() => setSort('profile')}>
                    Used Profile{sortIndicator('profile')}
                  </button>
                </th>
                <th className="pb-2">
                  <button type="button" className="font-medium hover:text-slate-800" onClick={() => setSort('pages')}>
                    Pages{sortIndicator('pages')}
                  </button>
                </th>
                <th className="hidden pb-2 md:table-cell">
                  <button type="button" className="font-medium hover:text-slate-800" onClick={() => setSort('created')}>
                    Created{sortIndicator('created')}
                  </button>
                </th>
                <th className="pb-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {paginatedItems.map((job) => (
                <tr key={job.id} className="border-t border-slate-100">
                  <td className="py-3">
                    <Link href={`/jobs/${job.id}`} className="font-medium text-slate-950 hover:text-emerald-700">
                      {job.original_filename}
                    </Link>
                    {job.status === 'FAILED' && (
                      <p className="mt-1 text-xs text-amber-700">
                        {typeof job.processing_info?.execution?.warning === 'string'
                          ? job.processing_info.execution.warning
                          : job.error_message || 'Processing stopped. Retry with a lower profile.'}
                      </p>
                    )}
                  </td>
                  <td className="py-3">
                    <span className={`rounded px-2 py-1 text-xs ${statusBadge[job.status]}`}>{job.status}</span>
                  </td>
                  <td className="py-3 text-slate-700">{profileForJob(job)}</td>
                  <td className="py-3 text-slate-700">{pageCountForJob(job)}</td>
                  <td className="hidden py-3 text-slate-700 md:table-cell">{new Date(job.created_at).toLocaleString()}</td>
                  <td className="py-3 text-right">
                    {endpoint === 'jobs' && (
                      <div className="flex flex-wrap justify-end gap-2">
                        {job.status === 'FAILED' && suggestedLowerProfile(job) && (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={retryingLowerJobId === job.id}
                            onClick={() => void retryJobLowerProfile(job)}
                          >
                            <RotateCcw className="mr-2 h-4 w-4" />
                            {retryingLowerJobId === job.id ? 'Retrying...' : 'Retry Lower'}
                          </Button>
                        )}
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={job.status === 'RUNNING' || restartingJobId === job.id}
                          onClick={() => void restartJob(job.id)}
                        >
                          <RotateCcw className="mr-2 h-4 w-4" />
                          {restartingJobId === job.id ? 'Restarting...' : 'Restart'}
                        </Button>
                        {allowDelete && (
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            disabled={job.status === 'RUNNING'}
                            onClick={() => void removeJob(job.id)}
                          >
                            <Trash2 className="h-4 w-4 text-red-600" />
                          </Button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {sortedItems.length > pageSize && (
            <div className="mt-4 flex items-center justify-between gap-3 text-sm text-slate-600">
              <p>
                Showing {(displayPage - 1) * pageSize + 1}-{Math.min(displayPage * pageSize, sortedItems.length)} of {sortedItems.length}
              </p>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" disabled={displayPage === 1} onClick={() => setCurrentPage((page) => page - 1)}>
                  Previous
                </Button>
                <Button variant="outline" size="sm" disabled={displayPage === totalPages} onClick={() => setCurrentPage((page) => page + 1)}>
                  Next
                </Button>
              </div>
            </div>
          )}
          {sortedItems.length === 0 && !loading && (
            <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
              <LoaderCircle className="h-4 w-4 animate-spin" /> No documents found.
            </div>
          )}
          {loading && (
            <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
              <LoaderCircle className="h-4 w-4 animate-spin" /> Loading documents...
            </div>
          )}
        </div>
        </div>
      </section>

      {protectedJobId && (
        <div className="fixed inset-0 flex items-center justify-center bg-slate-950/50 p-4">
          <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-lg">
            <h2 className="mb-3 text-lg font-semibold">Password Required</h2>
            <p className="mb-4 text-sm text-slate-600">This job is password protected.</p>
            <input
              type="password"
              value={protectedJobPassword}
              onChange={(e) => setProtectedJobPassword(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void handlePasswordSubmit()}
              placeholder="Enter password"
              className="mb-4 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
              autoFocus
            />
            <div className="flex gap-2">
              <Button onClick={handlePasswordSubmit}>Delete</Button>
              <Button variant="outline" onClick={() => setProtectedJobId(null)}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
