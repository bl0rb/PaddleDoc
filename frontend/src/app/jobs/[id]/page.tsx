'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import { useParams } from 'next/navigation';

import { Button } from '@/components/ui/button';
import type { JobArtifact } from '@/components/markdown/markdown-view';
import { apiFetch, redirectIfSessionExpired, type JobVersionEntry, type JobVersionsResponse } from '@/lib/api';
import { API_BASE_URL } from '@/lib/api-base';
import { peekCached, setCached } from '@/lib/data-cache';

// react-markdown + remark-gfm + rehype-sanitize are only needed for the
// "Rendered" tab (the default tab is "Raw" for everything but Confluence
// imports) — loading them eagerly would tax every job-detail page visit
// with a parser bundle most visits never use. Deferred + client-only: the
// server-rendered shell never waits on it, and the chunk is fetched on
// first actual need.
const MarkdownView = dynamic(() => import('@/components/markdown/markdown-view').then((mod) => mod.MarkdownView), {
  ssr: false,
  loading: () => (
    <div className="animate-pulse space-y-3" role="status" aria-label="Loading rendered preview">
      <div className="h-4 w-3/4 rounded bg-slate-100" />
      <div className="h-4 w-full rounded bg-slate-100" />
      <div className="h-4 w-5/6 rounded bg-slate-100" />
      <div className="h-4 w-2/3 rounded bg-slate-100" />
    </div>
  ),
});

const LOWER_PROFILE_RETRY_MAP: Record<string, string> = {
  ppocrv6_medium_structurev3: 'ppocrv6_small_structurev3',
  ppocrv6_small_structurev3: 'ppocrv6_tiny_structurev3',
  ppocrv6_medium: 'ppocrv6_tiny',
  ppocrv6_small: 'ppocrv6_tiny',
};

const VERSION_STATUS_BADGE: Record<string, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-emerald-100 text-emerald-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-600/20 text-red-300',
};

type Job = {
  id: string;
  original_filename: string;
  status: 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';
  tags?: string[];
  processing_info?: {
    settings?: Record<string, unknown>;
    execution?: Record<string, unknown>;
  } | null;
  created_at: string;
  content_sha256?: string | null;
  document_version?: number;
  previous_job_id?: string | null;
};

const API = API_BASE_URL;

export default function JobDetailsPage() {
  const params = useParams<{ id: string }>();
  if (!params.id) {
    return null;
  }
  // The Versions table links between jobs on this same dynamic route. Keying
  // the details component by id remounts it on every id change, so all
  // per-job state (markdown, artifacts, password gate, edit mode) starts
  // fresh instead of leaking from the previously viewed version.
  return <JobDetails key={params.id} jobId={params.id} />;
}

function JobDetails({ jobId }: { jobId: string }) {
  // Job metadata (unlike the markdown preview) isn't gated behind the
  // document password, so it's safe to reuse: re-opening a job you already
  // viewed this session (e.g. the browser back button) paints its header
  // and status instantly instead of the loading skeleton every time.
  const [job, setJob] = useState<Job | null>(
    () => peekCached<Job>(`/api/v1/jobs/${jobId}`) ?? null
  );
  const [markdown, setMarkdown] = useState('');
  const [draftMarkdown, setDraftMarkdown] = useState('');
  const [isEditing, setIsEditing] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [requirePassword, setRequirePassword] = useState(false);
  const [password, setPassword] = useState('');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isRetryingLower, setIsRetryingLower] = useState(false);
  const [viewTab, setViewTab] = useState<'rendered' | 'raw'>('raw');
  // null = artifact list not fetched yet (images render skeletons); [] after
  // a completed fetch that found nothing or failed (broken-image placeholder).
  const [artifacts, setArtifacts] = useState<JobArtifact[] | null>(null);
  // null = not fetched yet, or the backend doesn't support this endpoint yet
  // (404) — the Versions section stays hidden in both cases.
  const [versions, setVersions] = useState<JobVersionEntry[] | null>(null);

  // `isActive` lets the load effect discard results that finish after the
  // user has navigated to another job id on this same dynamic route (the
  // component does not remount, so a late response would otherwise clobber
  // the newer job's state). Direct user actions pass the always-true default.
  const loadVersions = async (id: string, isActive: () => boolean = () => true) => {
    try {
      const resp = await apiFetch(`/api/v1/jobs/${id}/versions`, { cache: 'no-store', skipAuthRedirect: true });
      if (!isActive()) {
        return;
      }
      if (!resp.ok) {
        setVersions(null);
        return;
      }
      const payload = (await resp.json()) as JobVersionsResponse;
      if (!isActive()) {
        return;
      }
      setVersions(Array.isArray(payload?.items) ? payload.items : null);
    } catch {
      // Non-fatal: an older backend without this endpoint (or any transient
      // error) simply hides the Versions section.
      if (isActive()) {
        setVersions(null);
      }
    }
  };

  const loadArtifacts = async (id: string, pw: string, isActive: () => boolean = () => true) => {
    try {
      const url = new URL(`${API}/api/v1/jobs/${id}/artifacts`);
      if (pw) {
        url.searchParams.set('password', pw);
      }
      // skipAuthRedirect: a 401 here means the document password, same as
      // the sibling /preview fetches — never bounce to /login for it.
      const resp = await apiFetch(url.toString(), { cache: 'no-store', skipAuthRedirect: true });
      if (!isActive()) {
        return;
      }
      if (resp.ok) {
        const payload = await resp.json();
        if (!isActive()) {
          return;
        }
        setArtifacts(Array.isArray(payload?.items) ? payload.items : []);
      } else {
        // Completed but failed: resolve to "no artifacts" so images show the
        // error placeholder instead of a never-ending skeleton.
        setArtifacts([]);
      }
    } catch {
      // Non-fatal: artifact images fall back to their placeholder.
      if (isActive()) {
        setArtifacts([]);
      }
    }
  };

  useEffect(() => {
    // Per-job state resets are handled by the key={id} remount in
    // JobDetailsPage; this effect only fetches.
    const id = jobId;
    let active = true;
    const run = async () => {
      const jobResp = await apiFetch(`/api/v1/jobs/${id}`, { cache: 'no-store' });
      if (!active) {
        return;
      }
      if (!jobResp.ok) {
        setLoadError('Failed to load job');
        return;
      }
      const jobData = await jobResp.json();
      if (!active) {
        return;
      }
      setJob(jobData);
      setCached(`/api/v1/jobs/${id}`, jobData);
      void loadVersions(id, () => active);
      const jobSettings = jobData.processing_info?.settings as Record<string, unknown> | undefined;
      setViewTab(jobSettings?.mode === 'import' ? 'rendered' : 'raw');
      if (jobData.status === 'FINISHED') {
        const previewResp = await apiFetch(`/api/v1/jobs/${id}/preview`, { cache: 'no-store', skipAuthRedirect: true });
        if (!active) {
          return;
        }
        if (previewResp.status === 401) {
          // Could also be an expired session — redirect instead of
          // showing a password prompt that can never succeed.
          if (await redirectIfSessionExpired()) {
            return;
          }
          if (!active) {
            return;
          }
          setRequirePassword(true);
          return;
        }
        if (previewResp.ok) {
          const text = await previewResp.text();
          if (!active) {
            return;
          }
          setMarkdown(text);
          setDraftMarkdown(text);
          void loadArtifacts(id, '', () => active);
        }
      }
    };
    void run();
    return () => {
      active = false;
    };
  }, [jobId]);

  const loadMarkdownWithPassword = async () => {
    const id = jobId;
    
    const url = new URL(`${API}/api/v1/jobs/${id}/preview`);
    if (password) {
      url.searchParams.set('password', password);
    }
    
    const previewResp = await apiFetch(url.toString(), { cache: 'no-store', skipAuthRedirect: true });
    if (previewResp.status === 401) {
      if (await redirectIfSessionExpired()) {
        return;
      }
      setLoadError('Invalid password');
      return;
    }
    if (previewResp.ok) {
      const text = await previewResp.text();
      setMarkdown(text);
      setDraftMarkdown(text);
      setRequirePassword(false);
      setLoadError(null);
      void loadArtifacts(id, password);
    }
  };

  if (!job) {
    return (
      <main className="min-h-screen bg-white px-4 py-6 text-slate-950 sm:px-6 lg:px-8">
        <div
          className="mx-auto w-full max-w-6xl animate-pulse space-y-4 rounded-3xl border border-slate-200 bg-white p-4 shadow-[0_24px_70px_rgba(15,23,42,0.08)] sm:p-6 lg:p-8"
          role="status"
          aria-label="Loading job"
        >
          <div className="h-9 w-32 rounded-md bg-slate-100" />
          <div className="h-7 w-48 rounded bg-slate-100" />
          <div className="space-y-2">
            <div className="h-4 w-2/3 rounded bg-slate-100" />
            <div className="h-4 w-1/3 rounded bg-slate-100" />
            <div className="h-4 w-1/4 rounded bg-slate-100" />
          </div>
          <div className="h-64 rounded-md border border-slate-100 bg-slate-50" />
        </div>
      </main>
    );
  }

  if (requirePassword) {
    return (
      <main className="min-h-screen bg-white p-8 text-slate-950">
        <div className="mx-auto max-w-md space-y-4 rounded-3xl border border-slate-200 bg-white p-6 shadow-[0_24px_70px_rgba(15,23,42,0.08)]">
          <h1 className="text-2xl font-semibold">Password Required</h1>
          <p className="text-slate-600">This job is password protected.</p>
          {loadError && <p className="text-sm text-red-600">{loadError}</p>}
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void loadMarkdownWithPassword()}
            placeholder="Enter password"
            className="w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
          />
          <div className="flex gap-2">
            <Button onClick={loadMarkdownWithPassword}>Unlock</Button>
            <Link href="/jobs">
              <Button variant="outline">Back</Button>
            </Link>
          </div>
        </div>
      </main>
    );
  }

  const settings = job.processing_info?.settings as Record<string, unknown> | undefined;
  const execution = job.processing_info?.execution as Record<string, unknown> | undefined;
  const selectedProfileId = typeof settings?.profile_id === 'string' ? settings.profile_id : null;
  const selectedProfileLabel = typeof execution?.profile_label === 'string' ? execution.profile_label : null;
  const converter = typeof execution?.converter === 'string' ? execution.converter : null;
  const structure = execution?.structure as Record<string, unknown> | undefined;
  const blockCount = typeof structure?.block_count === 'number' ? structure.block_count : null;
  const pageCount = typeof structure?.page_count === 'number' ? structure.page_count : null;
  const warning = typeof execution?.warning === 'string' ? execution.warning : null;
  // Requeue paths (restart, retry-lower) keep the previous run's execution
  // fields until a worker claims the job, so both banners must only reflect
  // a finished run — otherwise they show stale/contradictory data while
  // the job is PENDING/RUNNING.
  const usedFallback = job.status === 'FINISHED' && execution?.used_fallback === true;
  const fallbackReason = typeof execution?.fallback_reason === 'string' ? execution.fallback_reason : null;
  const engine = typeof execution?.engine === 'string' ? execution.engine : null;
  const resolvedProfileId = typeof execution?.profile_id === 'string' ? execution.profile_id : null;
  const profileMismatch = Boolean(
    job.status === 'FINISHED' && selectedProfileId && resolvedProfileId && selectedProfileId !== resolvedProfileId
  );
  const suggestedLowerProfile =
    (typeof execution?.suggested_profile_id === 'string' ? execution.suggested_profile_id : null) ||
    (typeof settings?.profile_id === 'string' ? LOWER_PROFILE_RETRY_MAP[settings.profile_id] ?? null : null);

  const retryWithLowerProfile = async () => {
    if (!job?.id) {
      return;
    }
    setIsRetryingLower(true);
    setLoadError(null);
    try {
      const response = await apiFetch(`/api/v1/jobs/${job.id}/retry-lower-profile`, { method: 'POST' });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to retry with lower profile.';
        setLoadError(detail);
        return;
      }
      const refreshed = await apiFetch(`/api/v1/jobs/${job.id}`, { cache: 'no-store' });
      if (refreshed.ok) {
        const jobData = await refreshed.json();
        setJob(jobData);
        setCached(`/api/v1/jobs/${job.id}`, jobData);
      }
    } finally {
      setIsRetryingLower(false);
    }
  };
  const qualityGate = execution?.quality_gate as Record<string, unknown> | undefined;
  const qualityGrade = typeof qualityGate?.grade === 'string' ? qualityGate.grade : null;
  const qualityScore = typeof qualityGate?.score === 'number' ? qualityGate.score : null;
  const qualityRecommendation = typeof qualityGate?.recommendation === 'string' ? qualityGate.recommendation : null;

  const saveMarkdown = async () => {
    setIsSaving(true);
    setSaveMessage(null);
    const url = new URL(`${API}/api/v1/jobs/${job?.id || ''}/save`);
    if (password) {
      url.searchParams.set('password', password);
    }
    const response = await apiFetch(url.toString(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown: draftMarkdown }),
      skipAuthRedirect: true,
    });
    if (!response.ok) {
      if (response.status === 401 && (await redirectIfSessionExpired())) {
        return;
      }
      setSaveMessage(
        response.status === 401
          ? 'Save failed: wrong document password.'
          : 'Save failed. Ensure YAML frontmatter remains intact.'
      );
      setIsSaving(false);
      return;
    }
    const payload = await response.json();
    setMarkdown(draftMarkdown);
    setSaveMessage(`Saved as version ${payload.version}.`);
    setIsEditing(false);
    setIsSaving(false);
  };

  return (
    <main className="min-h-screen bg-white px-4 py-6 text-slate-950 sm:px-6 lg:px-8">
      <div className="mx-auto w-full max-w-6xl space-y-4 rounded-3xl border border-slate-200 bg-white p-4 shadow-[0_24px_70px_rgba(15,23,42,0.08)] sm:p-6 lg:p-8">
        <div className="flex justify-start">
          <Link href="/jobs">
            <Button variant="outline">Back to jobs</Button>
          </Link>
        </div>
        <h1 className="font-serif text-2xl font-semibold">Job Details</h1>
        <p>Filename: {job.original_filename}</p>
        {job.tags && job.tags.length > 0 && <p>Tags: {job.tags.join(', ')}</p>}
        <p className="flex items-center gap-2">
          Status: {job.status}
          {typeof job.document_version === 'number' && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-600">
              v{job.document_version}
            </span>
          )}
        </p>
        <p>Created: {new Date(job.created_at).toLocaleString()}</p>
        {selectedProfileId && <p>Profile: {selectedProfileId}</p>}
        {selectedProfileLabel && <p>Profile name: {selectedProfileLabel}</p>}
        {converter && <p>Converter: {converter}</p>}
        {pageCount !== null && blockCount !== null && <p>Structure: {pageCount} pages, {blockCount} blocks</p>}
        {usedFallback && (
          <div className="rounded-md border border-red-300 bg-red-50 p-3 text-red-900">
            <p className="text-sm font-semibold">
              OCR did not run — this result came from the {engine ?? 'plain-text'} extraction fallback.
            </p>
            <p className="mt-1 text-sm">
              The selected profile{selectedProfileLabel ? ` (${selectedProfileLabel})` : ''} had no effect on this
              output. Fix the worker (see reason below), then restart this job from the jobs list to run real OCR.
            </p>
            {fallbackReason && <p className="mt-1 break-words text-sm">Reason: {fallbackReason}</p>}
          </div>
        )}
        {!usedFallback && profileMismatch && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900">
            <p className="text-sm">
              Requested profile {selectedProfileId} is unknown to the worker; the job ran with {resolvedProfileId}{' '}
              instead.
            </p>
          </div>
        )}
        {job.status === 'FAILED' && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900">
            <p className="text-sm">
              {warning || 'Processing stopped for this document. Retry manually with a lower profile.'}
            </p>
            {suggestedLowerProfile && (
              <div className="mt-2">
                <Button size="sm" variant="outline" disabled={isRetryingLower} onClick={retryWithLowerProfile}>
                  {isRetryingLower ? 'Retrying...' : `Retry with ${suggestedLowerProfile}`}
                </Button>
              </div>
            )}
          </div>
        )}
        {qualityGrade && (
          <p>
            Quality gate: {qualityGrade}
            {qualityScore !== null ? ` (${qualityScore.toFixed(3)})` : ''}
            {qualityRecommendation ? ` - ${qualityRecommendation}` : ''}
          </p>
        )}
        {versions && versions.length > 1 && (
          <section>
            <h2 className="mb-2 text-lg font-semibold">Versions</h2>
            <div className="overflow-x-auto rounded-md border border-slate-200">
              <table className="w-full table-auto text-left text-sm">
                <thead className="bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-3 py-2 font-medium">Version</th>
                    <th className="px-3 py-2 font-medium">SHA</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Created</th>
                    <th className="px-3 py-2 font-medium">Uploaded by</th>
                  </tr>
                </thead>
                <tbody>
                  {versions.map((entry) => (
                    <tr key={entry.job_id} className="border-t border-slate-100">
                      <td className="px-3 py-2 text-slate-950">
                        {entry.job_id === job.id ? (
                          <span className="font-medium">v{entry.document_version}</span>
                        ) : (
                          <Link href={`/jobs/${entry.job_id}`} className="font-medium text-emerald-700 hover:underline">
                            v{entry.document_version}
                          </Link>
                        )}
                        {entry.is_current && <span className="ml-2 text-xs font-medium text-emerald-700">(current)</span>}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-600" title={entry.content_sha256 ?? ''}>
                        {entry.content_sha256 ? entry.content_sha256.slice(0, 12) : '-'}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`rounded px-2 py-1 text-xs ${VERSION_STATUS_BADGE[entry.status] ?? 'bg-slate-100 text-slate-700'}`}>
                          {entry.status}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-700">{new Date(entry.created_at).toLocaleString()}</td>
                      <td className="px-3 py-2 text-slate-700">{entry.uploaded_by ?? '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
        <section>
          <h2 className="mb-2 text-lg font-semibold">Processing Info</h2>
          <pre className="overflow-x-auto rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800">
            {JSON.stringify(job.processing_info ?? {}, null, 2)}
          </pre>
        </section>
        {job.status === 'FINISHED' && (
          <div className="flex flex-wrap gap-2">
            <a href={`${API}/api/v1/jobs/${job.id}/download${password ? `?password=${encodeURIComponent(password)}` : ''}`}>
              <Button>Download Markdown</Button>
            </a>
            <a href={`${API}/api/v1/jobs/${job.id}/export.json${password ? `?password=${encodeURIComponent(password)}` : ''}`}>
              <Button variant="outline">Download JSON</Button>
            </a>
          </div>
        )}
        <section>
          <h2 className="mb-2 text-lg font-semibold">Markdown Preview</h2>
          <div className="mb-2 flex items-center gap-2">
            <Button size="sm" variant={isEditing ? 'outline' : 'default'} onClick={() => setIsEditing(false)}>
              Preview
            </Button>
            <Button size="sm" variant={isEditing ? 'default' : 'outline'} onClick={() => setIsEditing(true)}>
              Edit
            </Button>
          </div>
          {!isEditing && (
            // Plain toggle buttons (aria-pressed), not an ARIA tabs widget:
            // the full tablist pattern needs panel wiring + arrow-key focus,
            // which these two view switchers do not implement.
            <div className="mb-2 flex items-center gap-2">
              <Button
                size="sm"
                aria-pressed={viewTab === 'rendered'}
                variant={viewTab === 'rendered' ? 'default' : 'outline'}
                onClick={() => setViewTab('rendered')}
              >
                Rendered
              </Button>
              <Button
                size="sm"
                aria-pressed={viewTab === 'raw'}
                variant={viewTab === 'raw' ? 'default' : 'outline'}
                onClick={() => setViewTab('raw')}
              >
                Raw
              </Button>
            </div>
          )}
          {isEditing ? (
            <div className="space-y-2">
              <textarea
                className="min-h-[380px] w-full rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800"
                value={draftMarkdown}
                onChange={(event) => setDraftMarkdown(event.target.value)}
              />
              <div className="flex items-center gap-2">
                <Button onClick={saveMarkdown} disabled={isSaving}>
                  {isSaving ? 'Saving...' : 'Save as new version'}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    setDraftMarkdown(markdown);
                    setIsEditing(false);
                    setSaveMessage(null);
                  }}
                >
                  Cancel
                </Button>
              </div>
              {saveMessage && <p className="text-sm text-slate-600">{saveMessage}</p>}
            </div>
          ) : viewTab === 'rendered' ? (
            <div className="rounded-md border border-slate-200 bg-white p-4">
              <MarkdownView markdown={markdown} jobId={job.id} password={password || undefined} artifacts={artifacts} />
            </div>
          ) : (
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800">{markdown}</pre>
          )}
        </section>
      </div>
    </main>
  );
}
