'use client';

import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Sparkles, UploadCloud } from 'lucide-react';
import { useRouter } from 'next/navigation';

import { Button } from '@/components/ui/button';
import {
  API,
  type FolderOptions,
  type Job,
  type PaddleCapabilities,
  type PaddleSettings,
  type UploadMode,
  type UploadProgress,
  buildFolderOptions,
  formatBytes,
  sendFormDataWithProgress,
} from './shared';

export function ProcessingFlow() {
  const router = useRouter();

  const [busy, setBusy] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [wizardStep, setWizardStep] = useState(1);
  const [mode, setMode] = useState<UploadMode>('single');

  const [email, setEmail] = useState('');
  const [department, setDepartment] = useState('');
  const [folder, setFolder] = useState('');
  const [subfolder, setSubfolder] = useState('');
  const [folderOptions, setFolderOptions] = useState<FolderOptions>({});
  const [newFolderName, setNewFolderName] = useState('');
  const [newSubfolderName, setNewSubfolderName] = useState('');
  const [password, setPassword] = useState('');
  const [folderBusy, setFolderBusy] = useState(false);
  const [tags, setTags] = useState('');

  const [flowMessage, setFlowMessage] = useState<string | null>(null);
  const [collectionId, setCollectionId] = useState<string | null>(null);
  const [collectionFiles, setCollectionFiles] = useState<string[]>([]);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);

  const [capabilities, setCapabilities] = useState<PaddleCapabilities>({ profiles: [] });
  const [selectedProfileId, setSelectedProfileId] = useState('ppocrv6_tiny');
  const [settings, setSettings] = useState<PaddleSettings>({
    default_profile: 'ppocrv6_tiny',
    timeout_seconds: 300,
  });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);

  const singleFileInputRef = useRef<HTMLInputElement>(null);
  const collectionFileInputRef = useRef<HTMLInputElement>(null);

  const selectedProfile =
    capabilities.profiles.find((option) => option.value === selectedProfileId) ?? capabilities.profiles[0];
  const selectedProfileCopy = selectedProfile?.description ?? 'Pick a profile to see details.';
  const selectedSubfolderOptions = folder ? (folderOptions[folder] ?? []) : [];

  const refreshFolderOptions = async () => {
    const response = await fetch(`${API}/api/v1/jobs`, { cache: 'no-store' });
    if (!response.ok) return;
    const payload = await response.json();
    const items = (payload.items ?? []) as Job[];
    setFolderOptions((prev) => buildFolderOptions(prev, items));
  };

  const refreshPaddleSettings = async () => {
    const response = await fetch(`${API}/api/v1/paddle/settings`, { cache: 'no-store' });
    if (!response.ok) return;
    const payload = await response.json();
    setSettings({
      default_profile: payload.default_profile,
      timeout_seconds: payload.timeout_seconds,
    });
    setSelectedProfileId(payload.default_profile ?? 'ppocrv6_tiny');
  };

  const refreshPaddleCapabilities = async () => {
    const response = await fetch(`${API}/api/v1/paddle/capabilities`, { cache: 'no-store' });
    if (!response.ok) return;
    const payload = await response.json();
    setCapabilities({ profiles: payload.profiles ?? [] });
  };

  useEffect(() => {
    void refreshFolderOptions();
    void refreshPaddleSettings();
    void refreshPaddleCapabilities();
  }, []);

  const uploadSingle = async (file: File) => {
    if (!selectedProfile) {
      setSettingsMessage('No profile available yet. Try again after capabilities load.');
      return;
    }
    setBusy(true);
    setFlowMessage(null);
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
      formData.append('profile_id', selectedProfile.value);
      formData.append('email', email.trim());
      formData.append('folder', folder.trim());
      formData.append('subfolder', subfolder.trim());
      formData.append('tags', tags.trim());
      formData.append('password', password.trim());
      formData.append('mode', 'single');
      await sendFormDataWithProgress(`${API}/api/v1/upload`, formData, (loaded, total) => {
        setUploadProgress({
          phase: 'single',
          currentFile: file.name,
          filesCompleted: 0,
          filesTotal: 1,
          bytesLoaded: loaded,
          bytesTotal: total || file.size || 1,
        });
      });
      setFlowMessage('Single file uploaded and processing started.');
      await refreshFolderOptions();
      router.push('/jobs');
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Single upload failed. Please verify the file type.';
      setFlowMessage(detail);
    } finally {
      setUploadProgress(null);
      setBusy(false);
    }
  };

  const ensureCollection = async () => {
    if (collectionId) {
      return collectionId;
    }
    const response = await fetch(`${API}/api/v1/collections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: email.trim(),
        department: department.trim(),
        folder: folder.trim(),
        subfolder: subfolder.trim(),
        password: password.trim(),
      }),
    });
    if (!response.ok) {
      throw new Error('Collection could not be created');
    }
    const payload = await response.json();
    setCollectionId(payload.collection_id);
    return payload.collection_id as string;
  };

  const uploadCollectionFiles = async (files: FileList | File[]) => {
    if (!files.length) {
      return;
    }
    setBusy(true);
    setFlowMessage(null);
    try {
      const id = await ensureCollection();
      const uploadedNames: string[] = [];
      const fileList = Array.from(files);
      const totalBytes = fileList.reduce((sum, entry) => sum + (entry.size || 0), 0) || 1;
      let completedBytes = 0;
      for (const [index, file] of fileList.entries()) {
        setUploadProgress({
          phase: 'collection',
          currentFile: file.name,
          filesCompleted: index,
          filesTotal: fileList.length,
          bytesLoaded: completedBytes,
          bytesTotal: totalBytes,
        });
        const formData = new FormData();
        formData.append('file', file);
        formData.append('folder', folder.trim());
        formData.append('subfolder', subfolder.trim());
        formData.append('tags', tags.trim());
        try {
          await sendFormDataWithProgress(`${API}/api/v1/collections/${id}/upload`, formData, (loaded, total) => {
            setUploadProgress({
              phase: 'collection',
              currentFile: file.name,
              filesCompleted: index,
              filesTotal: fileList.length,
              bytesLoaded: completedBytes + loaded,
              bytesTotal: totalBytes || total || 1,
            });
          });
        } catch (error) {
          const detail = error instanceof Error ? error.message : 'upload failed';
          setFlowMessage(`Failed to upload ${file.name}: ${detail}`);
          continue;
        }
        uploadedNames.push(file.name);
        completedBytes += file.size || 0;
      }
      if (uploadedNames.length > 0) {
        setCollectionFiles((prev) => [...prev, ...uploadedNames]);
        setFlowMessage(`${uploadedNames.length} file(s) uploaded to collection.`);
      }
      await refreshFolderOptions();
    } finally {
      setUploadProgress(null);
      setBusy(false);
    }
  };

  const startCollection = async () => {
    if (!collectionId || !selectedProfile) {
      setFlowMessage('Upload collection files first.');
      return;
    }
    setBusy(true);
    setFlowMessage(null);
    const response = await fetch(`${API}/api/v1/collections/${collectionId}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: selectedProfile.value }),
    });
    if (!response.ok) {
      setFlowMessage('Failed to start collection processing.');
      setBusy(false);
      return;
    }
    const payload = await response.json();
    setFlowMessage(`Collection started (${payload.started_jobs} jobs).`);
    await refreshFolderOptions();
    router.push('/jobs');
    setBusy(false);
  };

  const onDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const files = event.dataTransfer.files;
    if (files && files.length > 0) {
      if (mode === 'single') {
        await uploadSingle(files[0]);
      } else {
        await uploadCollectionFiles(files);
      }
    }
  };

  const savePaddleSettings = async () => {
    setSavingSettings(true);
    setSettingsMessage(null);
    const requestedProfile = settings.default_profile;
    const response = await fetch(`${API}/api/v1/paddle/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    });

    if (!response.ok) {
      setSettingsMessage('Failed to save settings');
      setSavingSettings(false);
      return;
    }

    const payload = await response.json();
    setSettings({
      default_profile: payload.default_profile,
      timeout_seconds: payload.timeout_seconds,
    });
    setSelectedProfileId(payload.default_profile ?? requestedProfile);
    if (payload.default_profile !== requestedProfile) {
      setSettingsMessage(`Profile '${requestedProfile}' is not available. Saved as '${payload.default_profile}'.`);
    } else {
      setSettingsMessage('Settings saved');
    }
    setSavingSettings(false);
  };

  const createFolder = async () => {
    const folderValue = newFolderName.trim();
    const subfolderValue = newSubfolderName.trim();
    if (!folderValue && !subfolderValue) {
      setFlowMessage('Please enter a folder or subfolder name first.');
      return;
    }
    setFolderBusy(true);
    const response = await fetch(`${API}/api/v1/folders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: folderValue, subfolder: subfolderValue }),
    });
    if (!response.ok) {
      setFlowMessage('Failed to create folder. Check folder names.');
      setFolderBusy(false);
      return;
    }
    const payload = await response.json();
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
    setFlowMessage(`Folder created: ${payload.path}`);
    setFolderBusy(false);
  };

  const steps =
    mode === 'collection'
      ? [
          { id: 1, title: 'Folder + metadata', description: 'Choose where this multi-file batch should be stored.' },
          { id: 2, title: 'Upload files', description: 'Upload all files into the selected folder.' },
          { id: 3, title: 'Settings + start', description: 'Choose OCR profile and start processing.' },
        ]
      : [
          { id: 1, title: 'Folder + metadata', description: 'Single-file metadata is optional.' },
          { id: 2, title: 'Choose profile', description: 'Pick OCR preset for this conversion.' },
          { id: 3, title: 'Upload', description: 'Upload one file into the selected folder.' },
        ];

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 text-slate-950 sm:px-6 lg:px-8">
      <section className="mb-8 rounded-lg border border-slate-200 bg-white p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Paddle Settings</h2>
          <Button variant="outline" size="sm" onClick={() => setSettingsOpen((value) => !value)}>
            {settingsOpen ? 'Close' : 'Open'}
          </Button>
        </div>
        {settingsOpen && (
          <>
            <p className="mb-3 mt-2 text-sm text-slate-600">Set the default profile used when you do not override it in step 1.</p>
            <div className="grid gap-3 md:grid-cols-2">
              <label className="text-sm text-slate-600">
                Default Profile
                <select
                  className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                  value={settings.default_profile}
                  onChange={(event) => {
                    setSettings((prev) => ({ ...prev, default_profile: event.target.value }));
                    setSelectedProfileId(event.target.value);
                  }}
                >
                  {capabilities.profiles.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-slate-500">
                  {capabilities.profiles.find((option) => option.value === settings.default_profile)?.description}
                </p>
              </label>
              <label className="text-sm text-slate-600">
                Timeout (seconds)
                <input
                  type="number"
                  min={1}
                  className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                  value={settings.timeout_seconds}
                  onChange={(event) =>
                    setSettings((prev) => ({ ...prev, timeout_seconds: Number(event.target.value) || 1 }))
                  }
                />
              </label>
            </div>
            <div className="mt-4 flex items-center gap-3">
              <Button onClick={savePaddleSettings} disabled={savingSettings}>
                {savingSettings ? 'Saving...' : 'Save Settings'}
              </Button>
              {settingsMessage && <span className="text-sm text-slate-600">{settingsMessage}</span>}
            </div>
          </>
        )}
      </section>

      <section className="mb-8">
        <h1 className="text-3xl font-semibold">Transform Documents Into Structured Markdown</h1>
        <p className="mt-2 text-slate-600">Step through mode, metadata, profile, and upload. The flow supports single files and ordered collections.</p>
      </section>

      <section id="upload-flow" className="mb-8 rounded-xl border border-slate-200 bg-gradient-to-br from-emerald-50 to-white p-5">
        <div className="mb-5 grid gap-3 md:grid-cols-3">
          {steps.map((step) => {
            const active = wizardStep === step.id;
            const completed = wizardStep > step.id;
            return (
              <button
                key={step.id}
                type="button"
                onClick={() => setWizardStep(step.id)}
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
              <div className="grid gap-3 md:grid-cols-2">
                <button
                  type="button"
                  onClick={() => setMode('single')}
                  className={`rounded-xl border p-4 text-left ${mode === 'single' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'}`}
                >
                  <p className="text-sm font-semibold text-slate-950">Single file</p>
                  <p className="mt-1 text-xs text-slate-600">Upload one document and start processing immediately.</p>
                </button>
                <button
                  type="button"
                  onClick={() => setMode('collection')}
                  className={`rounded-xl border p-4 text-left ${mode === 'collection' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'}`}
                >
                  <p className="text-sm font-semibold text-slate-950">Multiple files</p>
                  <p className="mt-1 text-xs text-slate-600">Upload multiple files into one folder, then start together.</p>
                </button>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="text-sm text-slate-600">
                  Email (optional)
                  <input
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    type="email"
                    className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    placeholder="name@company.com"
                  />
                </label>
                {mode === 'collection' && (
                  <label className="text-sm text-slate-600">
                    Department (optional)
                    <input
                      value={department}
                      onChange={(event) => setDepartment(event.target.value)}
                      className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                      placeholder="Finance"
                    />
                  </label>
                )}
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
                    <option value="">No folder (inbox)</option>
                    {Object.keys(folderOptions).sort((left, right) => left.localeCompare(right)).map((option) => (
                      <option key={option} value={option}>{option}</option>
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
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </label>
                <label className="text-sm text-slate-600">
                  New folder
                  <input
                    value={newFolderName}
                    onChange={(event) => setNewFolderName(event.target.value)}
                    className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    placeholder="invoices"
                  />
                </label>
                <label className="text-sm text-slate-600">
                  New subfolder
                  <input
                    value={newSubfolderName}
                    onChange={(event) => setNewSubfolderName(event.target.value)}
                    className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    placeholder="2026/april"
                  />
                </label>
                <div className="flex items-end md:col-span-2">
                  <Button type="button" variant="outline" onClick={createFolder} disabled={folderBusy}>
                    {folderBusy ? 'Adding...' : 'Add Folder'}
                  </Button>
                </div>
                <label className="text-sm text-slate-600 md:col-span-2">
                  Tags, comma separated (optional)
                  <input
                    value={tags}
                    onChange={(event) => setTags(event.target.value)}
                    className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    placeholder="invoice, finance, 2026"
                  />
                </label>
                <label className="text-sm text-slate-600 md:col-span-2">
                  Password (optional)
                  <input
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    className="mt-1 w-full rounded border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950"
                    type="password"
                    placeholder="Leave empty for no protection"
                  />
                </label>
              </div>
              <div className="flex justify-end">
                <Button onClick={() => setWizardStep(2)}>
                  Continue
                </Button>
              </div>
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
              {mode === 'single' ? (
                <>
                  <div className="flex items-start gap-3 rounded-lg border border-slate-200 bg-emerald-50 p-4">
                    <Sparkles className="mt-1 h-5 w-5 text-slate-600" />
                    <div>
                      <p className="font-medium text-slate-950">Available profile</p>
                      <p className="text-sm text-slate-600">{selectedProfile?.label ?? 'Loading profiles...'}</p>
                      <p className="mt-1 text-xs text-slate-500">{selectedProfileCopy}</p>
                    </div>
                  </div>
                  <div className="grid gap-3 md:grid-cols-2">
                    {capabilities.profiles.map((profile) => {
                      const active = profile.value === selectedProfileId;
                      return (
                        <button
                          key={profile.value}
                          type="button"
                          onClick={() => setSelectedProfileId(profile.value)}
                          className={`rounded-xl border p-4 text-left transition ${
                            active ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                          }`}
                        >
                          <p className="text-sm font-semibold text-slate-950">{profile.label}</p>
                          <p className="mt-1 text-xs text-slate-600">{profile.description}</p>
                        </button>
                      );
                    })}
                  </div>
                </>
              ) : (
                <>
                  <motion.div
                    onDrop={onDrop}
                    onDragOver={(event) => event.preventDefault()}
                    onDragEnter={() => setDragActive(true)}
                    onDragLeave={() => setDragActive(false)}
                    className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-10 text-center"
                    animate={{ borderColor: dragActive ? '#6ee7b7' : '#10b981' }}
                  >
                    <UploadCloud className="mx-auto mb-4 h-10 w-10 text-slate-600" />
                    <p className="mb-2 text-lg font-medium">Upload all collection files</p>
                    <p className="mb-4 text-sm text-slate-600">After upload, choose profile and start processing all files.</p>
                    <input
                      ref={collectionFileInputRef}
                      type="file"
                      multiple
                      className="hidden"
                      accept=".pdf,.docx,.pptx,.xlsx,.xls,.png,.jpg,.jpeg"
                      onChange={async (event) => {
                        const files = event.currentTarget.files;
                        if (files) {
                          await uploadCollectionFiles(files);
                        }
                        event.currentTarget.value = '';
                      }}
                    />
                    <Button variant="outline" onClick={() => collectionFileInputRef.current?.click()}>
                      Select files
                    </Button>
                  </motion.div>
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600">
                    <p className="font-semibold text-slate-950">Uploaded files: {collectionFiles.length}</p>
                    {collectionFiles.length > 0 && (
                      <ul className="mt-2 max-h-28 space-y-1 overflow-y-auto text-xs text-slate-500">
                        {collectionFiles.map((name, index) => (
                          <li key={`${name}-${index}`}>{name}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </>
              )}
              <div className="flex justify-between gap-3">
                <Button variant="outline" onClick={() => setWizardStep(1)}>
                  Back
                </Button>
                <Button onClick={() => setWizardStep(3)} disabled={mode === 'collection' && collectionFiles.length === 0}>
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
            >
              {mode === 'single' ? (
                <motion.div
                  onDrop={onDrop}
                  onDragOver={(event) => event.preventDefault()}
                  onDragEnter={() => setDragActive(true)}
                  onDragLeave={() => setDragActive(false)}
                  className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-10 text-center"
                  animate={{ borderColor: dragActive ? '#6ee7b7' : '#10b981' }}
                >
                  <UploadCloud className="mx-auto mb-4 h-10 w-10 text-slate-600" />
                  <p className="mb-2 text-lg font-medium">Drag and drop file here</p>
                  <p className="mb-4 text-sm text-slate-600">PDF, DOCX, PPTX, XLSX, XLS, PNG, JPG, JPEG (max. 100 MB)</p>
                  <p className="mb-4 text-xs text-slate-500">
                    Target folder: {folder.trim() || 'single'}{subfolder.trim() ? ` / ${subfolder.trim()}` : ''}
                  </p>
                  <input
                    ref={singleFileInputRef}
                    type="file"
                    className="hidden"
                    accept=".pdf,.docx,.pptx,.xlsx,.xls,.png,.jpg,.jpeg"
                    onChange={async (event) => {
                      const file = event.currentTarget.files?.[0];
                      if (file) {
                        await uploadSingle(file);
                      }
                      event.currentTarget.value = '';
                    }}
                  />
                  <Button variant="outline" onClick={() => singleFileInputRef.current?.click()}>
                    Select file
                  </Button>
                </motion.div>
              ) : (
                <div className="rounded-xl border border-slate-200 bg-slate-50 p-5">
                  <p className="text-sm font-semibold text-slate-950">Collection settings and start</p>
                  <p className="mt-2 text-xs text-slate-600">
                    Uploaded files: {collectionFiles.length}. Select profile and start processing for all uploaded files.
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    Target folder: collections / {folder.trim() || 'collection'}{subfolder.trim() ? ` / ${subfolder.trim()}` : ''}
                  </p>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {capabilities.profiles.map((profile) => {
                      const active = profile.value === selectedProfileId;
                      return (
                        <button
                          key={profile.value}
                          type="button"
                          onClick={() => setSelectedProfileId(profile.value)}
                          className={`rounded-xl border p-4 text-left transition ${
                            active ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
                          }`}
                        >
                          <p className="text-sm font-semibold text-slate-950">{profile.label}</p>
                          <p className="mt-1 text-xs text-slate-600">{profile.description}</p>
                        </button>
                      );
                    })}
                  </div>
                  <div className="mt-4">
                    <Button onClick={startCollection} disabled={!collectionId || collectionFiles.length === 0 || busy}>
                      Start Collection Processing
                    </Button>
                  </div>
                </div>
              )}
              <div className="mt-4 flex justify-between gap-3">
                <Button variant="outline" onClick={() => setWizardStep(2)}>
                  Back
                </Button>
                <p className="self-center text-xs text-slate-500">
                  Selected: {selectedProfile?.label ?? 'No profile selected'}
                </p>
              </div>
              {flowMessage && <p className="mt-2 text-sm text-slate-600">{flowMessage}</p>}
              {uploadProgress && (
                <div className="mt-3 rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700">
                  <div className="flex items-center justify-between gap-4">
                    <div>
                      <p className="font-medium text-slate-950">
                        {uploadProgress.phase === 'single' ? 'Uploading file' : 'Uploading collection files'}
                      </p>
                      <p className="text-xs text-slate-500">{uploadProgress.currentFile}</p>
                    </div>
                    <p className="text-xs font-semibold text-slate-600">
                      {uploadProgress.filesCompleted}/{uploadProgress.filesTotal} files
                    </p>
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
                    <span>{Math.min(100, Math.round((uploadProgress.bytesLoaded / uploadProgress.bytesTotal) * 100))}%</span>
                    <span>
                      {formatBytes(uploadProgress.bytesLoaded)} / {formatBytes(uploadProgress.bytesTotal)}
                    </span>
                  </div>
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </section>
    </div>
  );
}
