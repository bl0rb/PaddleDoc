'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { AnimatePresence, motion } from 'framer-motion';
import { CheckCircle2, LoaderCircle, XCircle } from 'lucide-react';
import { useRouter } from 'next/navigation';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import {
  type FolderOptions,
  type Job,
  type PaddleCapabilities,
  buildFolderOptions,
} from '@/components/dashboard/shared';
import {
  type ImportAuthType,
  type ImportRun,
  type ImportSource,
  type ImportSourceListResponse,
  type ImportSourceTestResponse,
  TEST_COOLDOWN_FALLBACK_MS,
} from '@/lib/imports';

const WIZARD_STEPS = [
  { id: 1, title: 'Connection', description: 'Pick or create a Confluence connection and test it.' },
  { id: 2, title: 'Scope', description: 'Choose a space or a single page tree to import.' },
  { id: 3, title: 'Options + start', description: 'Limits, attachments, and where results are stored.' },
];

// Mirrors the backend defaults (import_max_pages / import_max_depth); the
// server clamps whatever the client sends, these are display hints only.
const SERVER_DEFAULT_MAX_PAGES = 200;
const SERVER_DEFAULT_MAX_DEPTH = 10;

export default function NewImportPage() {
  const router = useRouter();

  const [wizardStep, setWizardStep] = useState(1);

  // --- Step 1: connection ---
  const [sources, setSources] = useState<ImportSource[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [sourcesLoadFailed, setSourcesLoadFailed] = useState(false);
  const [loadNonce, setLoadNonce] = useState(0);
  const [selectedSourceId, setSelectedSourceId] = useState('');
  const [creatingSource, setCreatingSource] = useState(false);

  const [newName, setNewName] = useState('');
  const [newBaseUrl, setNewBaseUrl] = useState('');
  const [newAuthType, setNewAuthType] = useState<ImportAuthType>('cloud_basic');
  const [newEmail, setNewEmail] = useState('');
  // Write-only secrets: only ever hold what the user is typing right now;
  // stored credentials are never fetched or displayed.
  const [newCredential, setNewCredential] = useState('');
  const [createBusy, setCreateBusy] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState<string | null>(null);

  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<ImportSourceTestResponse | null>(null);
  // The server cooldown is per source (last_test_at column), so track one
  // client-side deadline per source id instead of a single global timer.
  const [testCooldowns, setTestCooldowns] = useState<Record<string, number>>({});
  // Remaining seconds for the SELECTED source's cooldown; updated from event
  // handlers and the interval below (never during render — purity rule).
  const [cooldownSeconds, setCooldownSeconds] = useState(0);

  // --- Step 2: scope ---
  const [scopeType, setScopeType] = useState<'space' | 'page'>('space');
  const [scopeValue, setScopeValue] = useState('');

  // --- Step 3: options + metadata ---
  const [maxPages, setMaxPages] = useState('');
  const [maxDepth, setMaxDepth] = useState('');
  const [includeAttachments, setIncludeAttachments] = useState(true);
  const [ocrAttachments, setOcrAttachments] = useState(false);
  const [capabilities, setCapabilities] = useState<PaddleCapabilities>({ profiles: [] });
  const [selectedProfileId, setSelectedProfileId] = useState('');

  const [folder, setFolder] = useState('');
  const [subfolder, setSubfolder] = useState('');
  const [folderOptions, setFolderOptions] = useState<FolderOptions>({});
  const [newFolderName, setNewFolderName] = useState('');
  const [newSubfolderName, setNewSubfolderName] = useState('');
  const [folderBusy, setFolderBusy] = useState(false);
  const [tags, setTags] = useState('');
  const [email, setEmail] = useState('');

  const [startBusy, setStartBusy] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const selectedSource = sources.find((entry) => entry.id === selectedSourceId) ?? null;
  const selectedSubfolderOptions = folder ? (folderOptions[folder] ?? []) : [];

  useEffect(() => {
    let cancelled = false;

    const loadInitialData = async () => {
      try {
        const [sourcesPayload, jobsPayload, capabilitiesPayload] = await Promise.all([
          apiJson<ImportSourceListResponse>('/api/v1/import/sources', { cache: 'no-store' }),
          apiJson<{ items: Job[] }>('/api/v1/jobs', { cache: 'no-store' }).catch(() => ({ items: [] as Job[] })),
          apiJson<PaddleCapabilities>('/api/v1/paddle/capabilities', { cache: 'no-store' }).catch(
            () => ({ profiles: [] }) as PaddleCapabilities,
          ),
        ]);
        if (cancelled) return;
        setSources(sourcesPayload.items);
        if (sourcesPayload.items.length > 0) {
          setSelectedSourceId(sourcesPayload.items[0].id);
        } else {
          setCreatingSource(true);
        }
        setFolderOptions((prev) => buildFolderOptions(prev, jobsPayload.items ?? []));
        setCapabilities({ profiles: capabilitiesPayload.profiles ?? [] });
        if ((capabilitiesPayload.profiles ?? []).length > 0) {
          setSelectedProfileId(capabilitiesPayload.profiles[0].value);
        }
      } catch (error) {
        if (!cancelled) {
          setSourcesLoadFailed(true);
          setConnectionMessage(error instanceof ApiError ? error.detail : 'Failed to load import sources.');
        }
      } finally {
        if (!cancelled) setSourcesLoaded(true);
      }
    };

    void loadInitialData();
    return () => {
      cancelled = true;
    };
  }, [loadNonce]);

  const retryLoadSources = () => {
    setSourcesLoaded(false);
    setSourcesLoadFailed(false);
    setConnectionMessage(null);
    setLoadNonce((nonce) => nonce + 1);
  };

  // Countdown for the selected source's /test cooldown window. Runs an
  // immediate async update (covers switching between sources with different
  // deadlines) plus a 250 ms tick while a deadline exists, and drops the
  // entry once it passes so the Test button re-enables.
  useEffect(() => {
    if (!selectedSourceId) return;
    const until = testCooldowns[selectedSourceId];
    const update = () => {
      if (until === undefined) {
        setCooldownSeconds(0);
        return;
      }
      const remaining = Math.ceil((until - Date.now()) / 1000);
      if (remaining <= 0) {
        setTestCooldowns((current) => {
          const next = { ...current };
          delete next[selectedSourceId];
          return next;
        });
        setCooldownSeconds(0);
      } else {
        setCooldownSeconds(remaining);
      }
    };
    const immediate = window.setTimeout(update, 0);
    const timer = until !== undefined ? window.setInterval(update, 250) : null;
    return () => {
      window.clearTimeout(immediate);
      if (timer !== null) window.clearInterval(timer);
    };
  }, [selectedSourceId, testCooldowns]);

  // The backend claims the per-source cooldown slot on EVERY completed test
  // attempt (last_test_at is set before probing, success or failure), so the
  // client arms its fallback window after each completed test as well as on
  // an explicit 429.
  const armTestCooldown = (sourceId: string) => {
    setTestCooldowns((current) => ({ ...current, [sourceId]: Date.now() + TEST_COOLDOWN_FALLBACK_MS }));
    setCooldownSeconds(Math.ceil(TEST_COOLDOWN_FALLBACK_MS / 1000));
  };

  const createSource = async () => {
    const name = newName.trim();
    const baseUrl = newBaseUrl.trim();
    const credential = newCredential.trim();
    const authUsername = newEmail.trim();
    if (!name || !baseUrl || !credential) {
      setConnectionMessage('Name, base URL, and the token are required.');
      return;
    }
    if (newAuthType === 'cloud_basic' && !authUsername) {
      setConnectionMessage('The Atlassian account email is required for Cloud connections.');
      return;
    }
    setCreateBusy(true);
    setConnectionMessage(null);
    try {
      const created = await apiJson<ImportSource>('/api/v1/import/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          base_url: baseUrl,
          auth_type: newAuthType,
          auth_username: newAuthType === 'cloud_basic' ? authUsername : '',
          credential,
        }),
      });
      setSources((current) => [created, ...current]);
      setSelectedSourceId(created.id);
      setCreatingSource(false);
      setTestResult(null);
      setNewName('');
      setNewBaseUrl('');
      setNewEmail('');
      setNewCredential('');
      setConnectionMessage('Source created. Test the connection before starting the import.');
    } catch (error) {
      setConnectionMessage(error instanceof ApiError ? error.detail : 'Failed to create source.');
    } finally {
      setCreateBusy(false);
    }
  };

  const testConnection = async () => {
    if (!selectedSourceId) return;
    setTestBusy(true);
    setTestResult(null);
    setConnectionMessage(null);
    try {
      const result = await apiJson<ImportSourceTestResponse>(`/api/v1/import/sources/${selectedSourceId}/test`, {
        method: 'POST',
      });
      setTestResult(result);
      armTestCooldown(selectedSourceId);
      if (result.ok && result.server_kind) {
        setSources((current) =>
          current.map((entry) =>
            entry.id === selectedSourceId
              ? { ...entry, server_kind: result.server_kind ?? entry.server_kind, last_validated_at: new Date().toISOString() }
              : entry,
          ),
        );
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 429) {
        // The server enforces a per-source cooldown but sends no Retry-After;
        // fall back to the documented default window.
        armTestCooldown(selectedSourceId);
        setConnectionMessage(error.detail);
      } else {
        setConnectionMessage(error instanceof ApiError ? error.detail : 'Connection test failed.');
      }
    } finally {
      setTestBusy(false);
    }
  };

  const createFolder = async () => {
    const folderValue = newFolderName.trim();
    const subfolderValue = newSubfolderName.trim();
    if (!folderValue && !subfolderValue) {
      setStartError('Enter a folder or subfolder name first.');
      return;
    }
    setFolderBusy(true);
    setStartError(null);
    try {
      const payload = await apiJson<{ path?: string }>('/api/v1/folders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: folderValue, subfolder: subfolderValue }),
      });
      const createdPath = String(payload.path ?? '').split('/').filter(Boolean);
      const createdFolder = createdPath[0] ?? '';
      const createdSubfolder = createdPath.length > 1 ? createdPath.slice(1).join('/') : '';
      if (createdFolder) {
        setFolderOptions((prev) => {
          const next = { ...prev };
          const current = new Set(next[createdFolder] ?? []);
          if (createdSubfolder) current.add(createdSubfolder);
          next[createdFolder] = Array.from(current).sort((a, b) => a.localeCompare(b));
          return next;
        });
        setFolder(createdFolder);
        setSubfolder(createdSubfolder);
      }
      setNewFolderName('');
      setNewSubfolderName('');
    } catch (error) {
      setStartError(error instanceof ApiError ? error.detail : 'Failed to create folder.');
    } finally {
      setFolderBusy(false);
    }
  };

  const startImport = async () => {
    if (!selectedSourceId) {
      setStartError('Pick a connection first.');
      setWizardStep(1);
      return;
    }
    if (!scopeValue.trim()) {
      setStartError(scopeType === 'space' ? 'Enter a space key.' : 'Enter a page URL or id.');
      setWizardStep(2);
      return;
    }
    const parsedMaxPages = maxPages.trim() ? Number.parseInt(maxPages, 10) : null;
    if (parsedMaxPages !== null && (!Number.isFinite(parsedMaxPages) || parsedMaxPages < 1)) {
      setStartError('Max pages must be a whole number of at least 1.');
      return;
    }
    const parsedMaxDepth = maxDepth.trim() ? Number.parseInt(maxDepth, 10) : null;
    if (parsedMaxDepth !== null && (!Number.isFinite(parsedMaxDepth) || parsedMaxDepth < 0)) {
      setStartError('Max depth must be a whole number of at least 0.');
      return;
    }

    setStartBusy(true);
    setStartError(null);
    try {
      const run = await apiJson<ImportRun>('/api/v1/import/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_id: selectedSourceId,
          scope: { type: scopeType, value: scopeValue.trim() },
          options: {
            max_pages: parsedMaxPages,
            max_depth: parsedMaxDepth,
            include_attachments: includeAttachments,
            ocr_attachments: includeAttachments && ocrAttachments,
            ocr_profile_id:
              includeAttachments && ocrAttachments && selectedProfileId ? selectedProfileId : null,
            folder: folder.trim(),
            subfolder: subfolder.trim(),
            tags: tags
              .split(',')
              .map((entry) => entry.trim())
              .filter(Boolean),
            email: email.trim(),
          },
        }),
      });
      router.push(`/imports/${run.id}`);
    } catch (error) {
      if (error instanceof ApiError) {
        // 409: untested source or an already-active run; 422: bad scope
        // value; 429: rate limited — the backend detail explains all three.
        setStartError(error.detail);
      } else {
        setStartError('Failed to start the import.');
      }
      setStartBusy(false);
    }
  };

  // Toggling the auth scheme clears the typed secret: the field is masked, so
  // a token entered for one scheme must not silently become the credential of
  // the other.
  const selectAuthType = (authType: ImportAuthType) => {
    if (authType !== newAuthType) {
      setNewCredential('');
    }
    setNewAuthType(authType);
  };

  const testDisabled = !selectedSourceId || testBusy || cooldownSeconds > 0 || !sourcesLoaded;

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">New Confluence Import</h1>
            <p className="mt-2 text-slate-600">Connect, choose what to import, and start the run.</p>
          </div>
          <Link href="/imports" className="text-sm text-emerald-700 hover:text-emerald-800">
            Back to imports
          </Link>
        </section>

        <section className="rounded-xl border border-slate-200 bg-gradient-to-br from-emerald-50 to-white p-5">
          <div className="mb-5 grid gap-3 md:grid-cols-3">
            {WIZARD_STEPS.map((step) => {
              const active = wizardStep === step.id;
              const completed = wizardStep > step.id;
              return (
                <button
                  key={step.id}
                  type="button"
                  onClick={() => {
                    if (step.id < wizardStep) setWizardStep(step.id);
                  }}
                  className={`rounded-lg border px-4 py-3 text-left transition ${
                    active
                      ? 'border-emerald-400 bg-emerald-50'
                      : completed
                        ? 'border-slate-200 bg-slate-50'
                        : 'border-slate-200 bg-white'
                  }`}
                >
                  <div className="flex items-center gap-2 text-sm font-semibold text-slate-950">
                    <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-emerald-100 text-xs text-emerald-800">
                      {step.id}
                    </span>
                    {step.title}
                  </div>
                  <p className="mt-2 text-xs text-slate-600">{step.description}</p>
                </button>
              );
            })}
          </div>

          <AnimatePresence mode="wait">
            {wizardStep === 1 && (
              <motion.div
                key="step-1"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -12 }}
                className="space-y-4"
              >
                {!sourcesLoaded ? (
                  <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
                    <LoaderCircle className="h-4 w-4 animate-spin" /> Loading connections...
                  </div>
                ) : (
                  <>
                    {sources.length > 0 && (
                      <div className="grid gap-3 md:grid-cols-2">
                        {sources.map((source) => {
                          const active = !creatingSource && source.id === selectedSourceId;
                          return (
                            <button
                              key={source.id}
                              type="button"
                              onClick={() => {
                                setSelectedSourceId(source.id);
                                setCreatingSource(false);
                                setTestResult(null);
                                setConnectionMessage(null);
                              }}
                              className={`rounded-xl border p-4 text-left ${
                                active ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                              }`}
                            >
                              <div className="flex items-center justify-between gap-2">
                                <p className="truncate text-sm font-semibold text-slate-950">{source.name}</p>
                                <span
                                  className={`shrink-0 rounded px-2 py-0.5 text-xs ${
                                    source.server_kind ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-600'
                                  }`}
                                >
                                  {source.server_kind === 'cloud'
                                    ? 'Cloud'
                                    : source.server_kind === 'datacenter'
                                      ? 'Server/DC'
                                      : 'untested'}
                                </span>
                              </div>
                              <p className="mt-1 truncate text-xs text-slate-600">{source.base_url}</p>
                              <p className="mt-1 text-xs text-slate-500">
                                {source.auth_type === 'cloud_basic'
                                  ? `Email + API token (${source.auth_username})`
                                  : 'Personal access token'}
                              </p>
                            </button>
                          );
                        })}
                        <button
                          type="button"
                          onClick={() => {
                            setCreatingSource(true);
                            setTestResult(null);
                            setConnectionMessage(null);
                          }}
                          className={`rounded-xl border border-dashed p-4 text-left ${
                            creatingSource ? 'border-emerald-300 bg-emerald-50' : 'border-slate-300 bg-white'
                          }`}
                        >
                          <p className="text-sm font-semibold text-slate-950">New connection</p>
                          <p className="mt-1 text-xs text-slate-600">Add another Confluence instance.</p>
                        </button>
                      </div>
                    )}

                    {creatingSource && (
                      <div className="rounded-xl border border-slate-200 bg-white p-4">
                        <p className="text-sm font-semibold text-slate-950">Create connection</p>
                        <div className="mt-3 grid gap-3 md:grid-cols-2">
                          <label className="text-sm text-slate-600">
                            Name
                            <input
                              value={newName}
                              onChange={(event) => setNewName(event.target.value)}
                              className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                              placeholder="ACME Confluence"
                            />
                          </label>
                          <label className="text-sm text-slate-600">
                            Base URL
                            <input
                              value={newBaseUrl}
                              onChange={(event) => setNewBaseUrl(event.target.value)}
                              className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                              placeholder="https://acme.atlassian.net"
                            />
                          </label>
                          <div className="md:col-span-2">
                            <p className="text-sm text-slate-600">Authentication</p>
                            <div className="mt-1 grid gap-3 md:grid-cols-2">
                              <button
                                type="button"
                                onClick={() => selectAuthType('cloud_basic')}
                                className={`rounded-xl border p-3 text-left ${
                                  newAuthType === 'cloud_basic' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                                }`}
                              >
                                <p className="text-sm font-semibold text-slate-950">Confluence Cloud</p>
                                <p className="mt-1 text-xs text-slate-600">Atlassian account email + API token.</p>
                              </button>
                              <button
                                type="button"
                                onClick={() => selectAuthType('pat_bearer')}
                                className={`rounded-xl border p-3 text-left ${
                                  newAuthType === 'pat_bearer' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                                }`}
                              >
                                <p className="text-sm font-semibold text-slate-950">Server / Data Center</p>
                                <p className="mt-1 text-xs text-slate-600">Personal access token (PAT).</p>
                              </button>
                            </div>
                          </div>
                          {newAuthType === 'cloud_basic' ? (
                            <>
                              <label className="text-sm text-slate-600">
                                Atlassian account email
                                <input
                                  value={newEmail}
                                  onChange={(event) => setNewEmail(event.target.value)}
                                  type="email"
                                  className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                                  placeholder="name@company.com"
                                />
                              </label>
                              <label className="text-sm text-slate-600">
                                API token
                                <input
                                  value={newCredential}
                                  onChange={(event) => setNewCredential(event.target.value)}
                                  type="password"
                                  // Browsers ignore "off" on password fields; "new-password"
                                  // (plus the manager hints) suppresses the save prompt for
                                  // this write-only credential.
                                  autoComplete="new-password"
                                  data-1p-ignore
                                  data-lpignore="true"
                                  className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                                  placeholder="Atlassian API token"
                                />
                              </label>
                            </>
                          ) : (
                            <label className="text-sm text-slate-600 md:col-span-2">
                              Personal access token
                              <input
                                value={newCredential}
                                onChange={(event) => setNewCredential(event.target.value)}
                                type="password"
                                autoComplete="new-password"
                                data-1p-ignore
                                data-lpignore="true"
                                className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                                placeholder="Confluence PAT"
                              />
                            </label>
                          )}
                        </div>
                        <p className="mt-2 text-xs text-slate-500">
                          Tokens are stored encrypted and are write-only: they are never shown again.
                        </p>
                        <div className="mt-3">
                          <Button onClick={() => void createSource()} disabled={createBusy}>
                            {createBusy ? 'Creating...' : 'Create connection'}
                          </Button>
                        </div>
                      </div>
                    )}

                    {!creatingSource && selectedSource && (
                      <div className="flex flex-wrap items-center gap-3">
                        <Button variant="outline" onClick={() => void testConnection()} disabled={testDisabled}>
                          {testBusy
                            ? 'Testing...'
                            : cooldownSeconds > 0
                              ? `Test connection (${cooldownSeconds}s)`
                              : 'Test connection'}
                        </Button>
                        {testResult && (
                          <span
                            className={`inline-flex items-center gap-1.5 text-sm ${
                              testResult.ok ? 'text-emerald-700' : 'text-red-600'
                            }`}
                          >
                            {testResult.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
                            {testResult.detail ?? (testResult.ok ? 'Connected' : 'Connection failed')}
                          </span>
                        )}
                      </div>
                    )}

                    {connectionMessage && <p className="text-sm text-slate-600">{connectionMessage}</p>}
                    {sourcesLoadFailed && (
                      <div>
                        <Button variant="outline" onClick={retryLoadSources}>
                          Retry loading connections
                        </Button>
                      </div>
                    )}
                    {!creatingSource && selectedSource && !selectedSource.server_kind && !(testResult?.ok ?? false) && (
                      <p className="text-xs text-amber-700">
                        This connection has not been tested successfully yet; starting an import requires a successful
                        test.
                      </p>
                    )}

                    <div className="flex justify-end">
                      <Button onClick={() => setWizardStep(2)} disabled={!selectedSourceId || creatingSource}>
                        Continue
                      </Button>
                    </div>
                  </>
                )}
              </motion.div>
            )}

            {wizardStep === 2 && (
              <motion.div
                key="step-2"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -12 }}
                className="space-y-4"
              >
                <div className="grid gap-3 md:grid-cols-2">
                  <button
                    type="button"
                    onClick={() => setScopeType('space')}
                    className={`rounded-xl border p-4 text-left ${
                      scopeType === 'space' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                    }`}
                  >
                    <p className="text-sm font-semibold text-slate-950">Whole space</p>
                    <p className="mt-1 text-xs text-slate-600">Import a space starting at its homepage.</p>
                  </button>
                  <button
                    type="button"
                    onClick={() => setScopeType('page')}
                    className={`rounded-xl border p-4 text-left ${
                      scopeType === 'page' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                    }`}
                  >
                    <p className="text-sm font-semibold text-slate-950">Page tree</p>
                    <p className="mt-1 text-xs text-slate-600">Import a page and its children.</p>
                  </button>
                </div>
                <label className="block text-sm text-slate-600">
                  {scopeType === 'space' ? 'Space key or space URL' : 'Page URL or page id'}
                  <input
                    value={scopeValue}
                    onChange={(event) => setScopeValue(event.target.value)}
                    className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    placeholder={
                      scopeType === 'space'
                        ? 'DOCS or https://acme.atlassian.net/wiki/spaces/DOCS/...'
                        : '123456 or https://acme.atlassian.net/wiki/spaces/DOCS/pages/123456/Title'
                    }
                  />
                </label>
                <p className="text-xs text-slate-500">
                  Pasting a full Confluence URL works — the server extracts the {scopeType === 'space' ? 'space key' : 'page id'}.
                </p>
                <div className="flex justify-between gap-3">
                  <Button variant="outline" onClick={() => setWizardStep(1)}>
                    Back
                  </Button>
                  <Button onClick={() => setWizardStep(3)} disabled={!scopeValue.trim()}>
                    Continue
                  </Button>
                </div>
              </motion.div>
            )}

            {wizardStep === 3 && (
              <motion.div
                key="step-3"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -12 }}
                className="space-y-4"
              >
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="text-sm text-slate-600">
                    Max pages
                    <input
                      value={maxPages}
                      onChange={(event) => setMaxPages(event.target.value)}
                      type="number"
                      min={1}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder={`${SERVER_DEFAULT_MAX_PAGES} (server default)`}
                    />
                    <p className="mt-1 text-xs text-slate-500">The server clamps values above its limit.</p>
                  </label>
                  <label className="text-sm text-slate-600">
                    Max depth
                    <input
                      value={maxDepth}
                      onChange={(event) => setMaxDepth(event.target.value)}
                      type="number"
                      min={0}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder={`${SERVER_DEFAULT_MAX_DEPTH} (server default)`}
                    />
                    <p className="mt-1 text-xs text-slate-500">0 imports only the root page.</p>
                  </label>
                </div>

                <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-4">
                  <label className="flex items-center gap-2 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      checked={includeAttachments}
                      onChange={(event) => setIncludeAttachments(event.target.checked)}
                      className="h-4 w-4 accent-emerald-600"
                    />
                    Include attachments and images
                  </label>
                  <label
                    className={`flex items-center gap-2 text-sm ${includeAttachments ? 'text-slate-700' : 'text-slate-400'}`}
                  >
                    <input
                      type="checkbox"
                      checked={includeAttachments && ocrAttachments}
                      disabled={!includeAttachments}
                      onChange={(event) => setOcrAttachments(event.target.checked)}
                      className="h-4 w-4 accent-emerald-600"
                    />
                    OCR supported attachments (PDF, Office, images) as separate jobs
                  </label>
                  {includeAttachments && ocrAttachments && (
                    <label className="block text-sm text-slate-600">
                      OCR profile
                      <select
                        value={selectedProfileId}
                        onChange={(event) => setSelectedProfileId(event.target.value)}
                        className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      >
                        {capabilities.profiles.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                      <p className="mt-1 text-xs text-slate-500">
                        {capabilities.profiles.find((option) => option.value === selectedProfileId)?.description ??
                          'Profiles load from the OCR capabilities endpoint.'}
                      </p>
                    </label>
                  )}
                </div>

                <div className="grid gap-3 md:grid-cols-2">
                  <label className="text-sm text-slate-600">
                    Target folder (optional)
                    <select
                      value={folder}
                      onChange={(event) => {
                        const nextFolder = event.target.value;
                        setFolder(nextFolder);
                        if (!nextFolder || !(folderOptions[nextFolder] ?? []).includes(subfolder)) {
                          setSubfolder('');
                        }
                      }}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    >
                      <option value="">Default (imports/&lt;space or page name&gt;)</option>
                      {Object.keys(folderOptions)
                        .sort((left, right) => left.localeCompare(right))
                        .map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                    </select>
                  </label>
                  <label className="text-sm text-slate-600">
                    Target subfolder (optional)
                    <select
                      value={subfolder}
                      onChange={(event) => setSubfolder(event.target.value)}
                      disabled={!folder}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    >
                      <option value="">No subfolder</option>
                      {selectedSubfolderOptions.map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-sm text-slate-600">
                    New folder
                    <input
                      value={newFolderName}
                      onChange={(event) => setNewFolderName(event.target.value)}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder="wiki"
                    />
                  </label>
                  <label className="text-sm text-slate-600">
                    New subfolder
                    <input
                      value={newSubfolderName}
                      onChange={(event) => setNewSubfolderName(event.target.value)}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder="2026/product"
                    />
                  </label>
                  <div className="flex items-end md:col-span-2">
                    <Button type="button" variant="outline" onClick={() => void createFolder()} disabled={folderBusy}>
                      {folderBusy ? 'Adding...' : 'Add Folder'}
                    </Button>
                  </div>
                  <label className="text-sm text-slate-600 md:col-span-2">
                    Tags, comma separated (optional)
                    <input
                      value={tags}
                      onChange={(event) => setTags(event.target.value)}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder="wiki, confluence, docs"
                    />
                  </label>
                  <label className="text-sm text-slate-600 md:col-span-2">
                    Email (optional)
                    <input
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      type="email"
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder="name@company.com"
                    />
                  </label>
                </div>

                {startError && (
                  <p className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{startError}</p>
                )}

                <div className="flex justify-between gap-3">
                  <Button variant="outline" onClick={() => setWizardStep(2)}>
                    Back
                  </Button>
                  <Button onClick={() => void startImport()} disabled={startBusy}>
                    {startBusy ? 'Starting...' : 'Start import'}
                  </Button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </section>
      </div>
    </main>
  );
}
