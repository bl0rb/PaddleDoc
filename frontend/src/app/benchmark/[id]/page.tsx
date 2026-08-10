'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import { useParams, useRouter } from 'next/navigation';
import { LoaderCircle } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { apiSend, ConfirmDialog } from '@/components/admin/admin-shared';
import { API } from '@/components/dashboard/shared';
import {
  ApiError,
  apiFetch,
  apiJson,
  benchmarkStatusChip,
  benchmarkVariantStatusChip,
  qualityGradeChip,
  type BenchmarkReport,
  type BenchmarkRunDetail,
  type BenchmarkVariantKind,
  type BenchmarkVariantStatus,
} from '@/lib/api';

// react-markdown + remark-gfm + rehype-sanitize are only needed for the
// "Rendered" tab — deferred + client-only, same rationale as jobs/[id].
const MarkdownView = dynamic(() => import('@/components/markdown/markdown-view').then((mod) => mod.MarkdownView), {
  ssr: false,
  loading: () => (
    <div className="animate-pulse space-y-3" role="status" aria-label="Loading rendered preview">
      <div className="h-4 w-3/4 rounded bg-slate-100" />
      <div className="h-4 w-full rounded bg-slate-100" />
      <div className="h-4 w-5/6 rounded bg-slate-100" />
    </div>
  ),
});

const POLL_INTERVAL_MS = 3000;

/** Unified row shape for the metrics table: full data once `report` has loaded, `—` placeholders before that. */
type VariantRow = {
  job_id: string;
  label: string;
  kind: BenchmarkVariantKind;
  status: BenchmarkVariantStatus;
  error: string | null;
  duration_seconds: number | null;
  page_count: number | null;
  output_chars: number | null;
  quality_grade: 'A' | 'B' | 'C' | null;
  used_fallback: boolean | null;
};

export default function BenchmarkRunPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;
  const router = useRouter();

  // Header extras (owner, content hash) — fetched once; the report poll below
  // carries everything the metrics table and status badge need on its own.
  const [run, setRun] = useState<BenchmarkRunDetail | null>(null);
  const [report, setReport] = useState<BenchmarkReport | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const [activeVariantJobId, setActiveVariantJobId] = useState<string | null>(null);
  const [viewTab, setViewTab] = useState<'rendered' | 'raw'>('raw');
  const [markdownByJob, setMarkdownByJob] = useState<Record<string, string>>({});
  const [markdownError, setMarkdownError] = useState<Record<string, string>>({});
  const [markdownLoading, setMarkdownLoading] = useState<Record<string, boolean>>({});

  // Header metadata (owner) — one-shot, not polled.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    apiJson<BenchmarkRunDetail>(`/api/v1/benchmarks/${runId}`, { cache: 'no-store' })
      .then((detail) => {
        if (!cancelled) setRun(detail);
      })
      .catch(() => {
        // Non-fatal: the header owner/hash line just stays blank; report
        // polling below still drives the rest of the page.
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // Report is designed to be polled directly (it is always 200, with fields
  // filling in as variants finish, and `all_terminal` marking "done") — so
  // it alone drives both the status badge and the progressive metrics table.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const payload = await apiJson<BenchmarkReport>(`/api/v1/benchmarks/${runId}/report`, { cache: 'no-store' });
        if (cancelled) return;
        setReport(payload);
        setLoadError(null);
        if (!payload.all_terminal) {
          timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setNotFound(true);
          return;
        }
        setLoadError(error instanceof ApiError ? error.detail : 'Failed to load the benchmark report.');
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

  // Default the markdown tab to the first finished variant once the report
  // loads. Adjusted during render (matching processing-flow.tsx's
  // lastSettingsData idiom) rather than in an effect: this derives initial
  // state from `report`, it does not synchronize with an external system.
  const [lastReportForTab, setLastReportForTab] = useState<BenchmarkReport | null>(null);
  if (report !== lastReportForTab) {
    setLastReportForTab(report);
    if (report && !activeVariantJobId) {
      const finished = report.variants.find((variant) => variant.status === 'FINISHED');
      setActiveVariantJobId((finished ?? report.variants[0])?.job_id ?? null);
    }
  }

  // Lazily fetch the selected variant's markdown via the job's own preview
  // endpoint — the report itself never embeds markdown (that would make it
  // too heavy to poll), each variant is a real, individually-fetchable Job.
  useEffect(() => {
    if (!activeVariantJobId) return;
    if (
      markdownByJob[activeVariantJobId] !== undefined ||
      markdownError[activeVariantJobId] ||
      markdownLoading[activeVariantJobId]
    ) {
      return;
    }
    let cancelled = false;
    const load = async () => {
      setMarkdownLoading((current) => ({ ...current, [activeVariantJobId]: true }));
      try {
        const resp = await apiFetch(`/api/v1/jobs/${activeVariantJobId}/preview`, {
          cache: 'no-store',
          skipAuthRedirect: true,
        });
        if (cancelled) return;
        if (resp.status === 401) {
          setMarkdownError((current) => ({
            ...current,
            [activeVariantJobId]: 'Password protected — open the job directly to view.',
          }));
          return;
        }
        if (!resp.ok) {
          setMarkdownError((current) => ({
            ...current,
            [activeVariantJobId]: 'Failed to load markdown for this variant.',
          }));
          return;
        }
        const text = await resp.text();
        if (cancelled) return;
        setMarkdownByJob((current) => ({ ...current, [activeVariantJobId]: text }));
      } catch {
        if (!cancelled) {
          setMarkdownError((current) => ({
            ...current,
            [activeVariantJobId]: 'Failed to load markdown for this variant.',
          }));
        }
      } finally {
        if (!cancelled) {
          setMarkdownLoading((current) => ({ ...current, [activeVariantJobId]: false }));
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // Deliberately keyed only on the active tab: markdownByJob/markdownError/
    // markdownLoading are read for their current value as a guard, not to
    // retrigger the fetch when they change (mirrors data-cache.ts's ttlMs gate).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVariantJobId]);

  if (notFound) {
    return (
      <main className="min-h-screen">
        <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
          <h1 className="text-2xl font-semibold">Benchmark run not found</h1>
          <p className="mt-2 text-sm text-slate-600">The run does not exist or is not visible to you.</p>
          <Link href="/benchmark" className="mt-4 inline-block text-sm text-emerald-700 hover:text-emerald-800">
            Back to benchmarks
          </Link>
        </div>
      </main>
    );
  }

  if (!report) {
    return (
      <main className="min-h-screen">
        <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
          <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
            <LoaderCircle className="h-4 w-4 animate-spin" /> Loading benchmark run...
          </div>
          {loadError && <p className="text-sm text-red-600">{loadError}</p>}
        </div>
      </main>
    );
  }

  const rows: VariantRow[] = report.variants.map((variant) => ({
    job_id: variant.job_id,
    label: variant.label,
    kind: variant.kind,
    status: variant.status,
    error: variant.error,
    duration_seconds: variant.duration_seconds,
    page_count: variant.page_count,
    output_chars: variant.output_chars,
    quality_grade: variant.quality_grade,
    used_fallback: variant.used_fallback,
  }));

  const activeVariant = report.variants.find((variant) => variant.job_id === activeVariantJobId) ?? null;
  const filename = report.original_filename;

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <p className="mb-3">
          <Link href="/benchmark" className="text-sm text-emerald-700 hover:text-emerald-800">
            Back to benchmarks
          </Link>
        </p>

        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-semibold">{filename}</h1>
            <p className="mt-1 text-sm text-slate-600">
              {run?.owner ? `Started by ${run.owner.username} · ` : ''}
              {new Date(report.created_at).toLocaleString()}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <span className={`rounded px-2 py-1 text-xs ${benchmarkStatusChip[report.status]}`}>{report.status}</span>
            {report.all_terminal && (
              <a href={`${API}/api/v1/benchmarks/${runId}/export.json`}>
                <Button variant="outline" size="sm">
                  Download JSON
                </Button>
              </a>
            )}
            <Button variant="danger" size="sm" onClick={() => setConfirmingDelete(true)}>
              Delete run
            </Button>
          </div>
        </section>

        {loadError && <p className="mb-4 text-sm text-amber-700">{loadError} Retrying...</p>}

        <section className="mb-6 overflow-x-auto rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <h2 className="mb-3 text-lg font-semibold">Variants</h2>
          <table className="w-full table-auto text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2 pr-4 font-medium">Variant</th>
                <th className="pb-2 pr-4 font-medium">Status</th>
                <th className="pb-2 pr-4 font-medium">Duration</th>
                <th className="pb-2 pr-4 font-medium">Pages</th>
                <th className="pb-2 pr-4 font-medium">Output</th>
                <th className="pb-2 pr-4 font-medium">Quality</th>
                <th className="pb-2 pr-4 font-medium">Fallback</th>
                <th className="pb-2 pr-4 font-medium">Error</th>
                <th className="pb-2 font-medium">Job</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.job_id} className="border-t border-slate-100">
                  <td className="py-3 pr-4">
                    <p className="font-medium text-slate-950">{row.label}</p>
                    <p className="text-xs text-slate-500">{row.kind === 'vl' ? 'VL connection' : 'OCR profile'}</p>
                  </td>
                  <td className="py-3 pr-4">
                    <span className={`rounded px-2 py-1 text-xs ${benchmarkVariantStatusChip[row.status]}`}>
                      {row.status}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-slate-700">
                    {row.duration_seconds !== null ? `${row.duration_seconds.toFixed(1)}s` : '—'}
                  </td>
                  <td className="py-3 pr-4 text-slate-700">{row.page_count ?? '—'}</td>
                  <td className="py-3 pr-4 text-slate-700">
                    {row.output_chars !== null ? `${row.output_chars.toLocaleString()} chars` : '—'}
                  </td>
                  <td className="py-3 pr-4">
                    {row.quality_grade ? (
                      <span className={`rounded px-2 py-1 text-xs ${qualityGradeChip[row.quality_grade]}`}>
                        {row.quality_grade}
                      </span>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="py-3 pr-4 text-slate-700">
                    {row.used_fallback === null ? '—' : row.used_fallback ? (
                      <span className="text-amber-700">Yes</span>
                    ) : (
                      'No'
                    )}
                  </td>
                  <td className="max-w-[220px] truncate py-3 pr-4 text-red-600" title={row.error ?? ''}>
                    {row.error ?? '—'}
                  </td>
                  <td className="py-3">
                    <Link href={`/jobs/${row.job_id}`} className="text-emerald-700 hover:text-emerald-800">
                      Open
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(report.summary.fastest_variant_job_id || report.summary.highest_quality_variant_job_id) && (
            <p className="mt-3 text-xs text-slate-500">
              {report.summary.fastest_variant_job_id && (
                <>Fastest: {rows.find((row) => row.job_id === report.summary.fastest_variant_job_id)?.label ?? '—'}</>
              )}
              {report.summary.fastest_variant_job_id && report.summary.highest_quality_variant_job_id && ' · '}
              {report.summary.highest_quality_variant_job_id && (
                <>
                  Highest quality:{' '}
                  {rows.find((row) => row.job_id === report.summary.highest_quality_variant_job_id)?.label ?? '—'}
                </>
              )}
            </p>
          )}
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <h2 className="mb-3 text-lg font-semibold">Markdown preview</h2>
          {!report.all_terminal ? (
            <p className="text-sm text-slate-600">Markdown becomes available once the run finishes.</p>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                {report.variants.map((variant) => (
                  <Button
                    key={variant.job_id}
                    size="sm"
                    aria-pressed={activeVariantJobId === variant.job_id}
                    variant={activeVariantJobId === variant.job_id ? 'default' : 'outline'}
                    onClick={() => setActiveVariantJobId(variant.job_id)}
                  >
                    {variant.label}
                  </Button>
                ))}
              </div>
              {activeVariant &&
                (activeVariant.status !== 'FINISHED' ? (
                  <p className="text-sm text-slate-600">
                    {activeVariant.status === 'FAILED'
                      ? `This variant failed${activeVariant.error ? `: ${activeVariant.error}` : '.'}`
                      : 'This variant has not finished yet.'}
                  </p>
                ) : markdownLoading[activeVariant.job_id] ? (
                  <div className="flex items-center gap-2 py-4 text-sm text-slate-600">
                    <LoaderCircle className="h-4 w-4 animate-spin" /> Loading markdown...
                  </div>
                ) : markdownError[activeVariant.job_id] ? (
                  <p className="text-sm text-red-600">{markdownError[activeVariant.job_id]}</p>
                ) : (
                  markdownByJob[activeVariant.job_id] !== undefined && (
                    <>
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
                        <Link
                          href={`/jobs/${activeVariant.job_id}`}
                          className="ml-auto text-sm text-emerald-700 hover:text-emerald-800"
                        >
                          Open job
                        </Link>
                      </div>
                      {viewTab === 'rendered' ? (
                        <div className="rounded-md border border-slate-200 bg-white p-4">
                          <MarkdownView
                            markdown={markdownByJob[activeVariant.job_id]}
                            jobId={activeVariant.job_id}
                            artifacts={[]}
                          />
                        </div>
                      ) : (
                        <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800">
                          {markdownByJob[activeVariant.job_id]}
                        </pre>
                      )}
                    </>
                  )
                ))}
            </>
          )}
        </section>

        {confirmingDelete && (
          <ConfirmDialog
            title="Delete benchmark run"
            body={
              <p>
                Delete <span className="font-semibold text-slate-950">{filename}</span>? This removes every variant
                job and its markdown history.
              </p>
            }
            confirmLabel="Delete run"
            onClose={() => setConfirmingDelete(false)}
            onConfirm={async () => {
              await apiSend(`/api/v1/benchmarks/${runId}`, { method: 'DELETE' });
              router.push('/benchmark');
            }}
          />
        )}
      </div>
    </main>
  );
}
