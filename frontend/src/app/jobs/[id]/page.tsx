'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';

import { Button } from '@/components/ui/button';

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
};

const API = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export default function JobDetails() {
  const params = useParams<{ id: string }>();
  const [job, setJob] = useState<Job | null>(null);
  const [markdown, setMarkdown] = useState('');
  const [draftMarkdown, setDraftMarkdown] = useState('');
  const [isEditing, setIsEditing] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [requirePassword, setRequirePassword] = useState(false);
  const [password, setPassword] = useState('');
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    const run = async () => {
      const id = params.id;
      if (!id) {
        return;
      }
      const jobResp = await fetch(`${API}/api/v1/jobs/${id}`, { cache: 'no-store' });
      if (!jobResp.ok) {
        setLoadError('Failed to load job');
        return;
      }
      const jobData = await jobResp.json();
      setJob(jobData);
      if (jobData.status === 'FINISHED') {
        const previewResp = await fetch(`${API}/api/v1/jobs/${id}/preview`, { cache: 'no-store' });
        if (previewResp.status === 401) {
          setRequirePassword(true);
          return;
        }
        if (previewResp.ok) {
          const text = await previewResp.text();
          setMarkdown(text);
          setDraftMarkdown(text);
        }
      }
    };
    run();
  }, [params]);

  const loadMarkdownWithPassword = async () => {
    const id = params.id;
    if (!id) return;
    
    const url = new URL(`${API}/api/v1/jobs/${id}/preview`);
    if (password) {
      url.searchParams.set('password', password);
    }
    
    const previewResp = await fetch(url.toString(), { cache: 'no-store' });
    if (previewResp.status === 401) {
      setLoadError('Invalid password');
      return;
    }
    if (previewResp.ok) {
      const text = await previewResp.text();
      setMarkdown(text);
      setDraftMarkdown(text);
      setRequirePassword(false);
      setLoadError(null);
    }
  };

  if (!job) {
    return <main className="min-h-screen bg-white p-8 text-slate-950">Loading job...</main>;
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
    const response = await fetch(url.toString(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown: draftMarkdown }),
    });
    if (!response.ok) {
      setSaveMessage('Save failed. Ensure YAML frontmatter remains intact.');
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
    <main className="min-h-screen bg-white p-8 text-slate-950">
      <div className="mx-auto max-w-4xl space-y-4 rounded-3xl border border-slate-200 bg-white p-6 shadow-[0_24px_70px_rgba(15,23,42,0.08)]">
        <h1 className="font-serif text-2xl font-semibold">Job Details</h1>
        <p>Filename: {job.original_filename}</p>
        {job.tags && job.tags.length > 0 && <p>Tags: {job.tags.join(', ')}</p>}
        <p>Status: {job.status}</p>
        <p>Created: {new Date(job.created_at).toLocaleString()}</p>
        {selectedProfileId && <p>Profile: {selectedProfileId}</p>}
        {selectedProfileLabel && <p>Profile name: {selectedProfileLabel}</p>}
        {converter && <p>Converter: {converter}</p>}
        {pageCount !== null && blockCount !== null && <p>Structure: {pageCount} pages, {blockCount} blocks</p>}
        {qualityGrade && (
          <p>
            Quality gate: {qualityGrade}
            {qualityScore !== null ? ` (${qualityScore.toFixed(3)})` : ''}
            {qualityRecommendation ? ` - ${qualityRecommendation}` : ''}
          </p>
        )}
        <section>
          <h2 className="mb-2 text-lg font-semibold">Processing Info</h2>
          <pre className="overflow-x-auto rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800">
            {JSON.stringify(job.processing_info ?? {}, null, 2)}
          </pre>
        </section>
        {job.status === 'FINISHED' && (
          <a href={`${API}/api/v1/jobs/${job.id}/download${password ? `?password=${encodeURIComponent(password)}` : ''}`}>
            <Button>Download Markdown</Button>
          </a>
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
          ) : (
            <pre className="overflow-x-auto rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800">{markdown}</pre>
          )}
        </section>
      </div>
    </main>
  );
}
