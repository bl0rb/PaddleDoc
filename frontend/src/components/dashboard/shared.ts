export type JobStatus = 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';
export type UIState = 'Idle' | 'Processing' | 'Finished';

export type Job = {
  id: string;
  original_filename: string;
  status: JobStatus;
  tags: string[];
  processing_info?: {
    settings?: {
      folder?: string | null;
      subfolder?: string | null;
    };
  } | null;
  created_at: string;
};

export type PaddleIndicator = 'running' | 'failed' | 'stopped';

export type ContainerState = {
  name: string;
  state: 'running' | 'stopped' | 'degraded' | 'unknown';
  detail?: string | null;
};

export type RuntimeCapabilityInfo = {
  torch_available: boolean;
  cuda_available: boolean;
  selected_device: 'cuda' | 'cpu';
  platform: string;
  no_cuda_reason?: string | null;
};

export type PaddleStatusResponse = {
  status: PaddleIndicator;
  detail?: string | null;
  runtime?: RuntimeCapabilityInfo | null;
  pending_jobs?: number;
  running_jobs?: number;
  queue_total?: number;
  running_workers?: number;
  worker_nodes?: string[];
  containers?: ContainerState[];
};

export type PaddleSettings = {
  default_profile: string;
  timeout_seconds: number;
};

export type PaddleOption = {
  value: string;
  label: string;
  description: string;
};

export type PaddleCapabilities = {
  profiles: PaddleOption[];
};

export type DashboardStats = {
  processed_documents: number;
  processed_pages: number;
  errors: number;
  database_size_bytes: number | null;
};

export type UploadMode = 'single' | 'collection';
export type DashboardView = 'home' | 'processing';

export type UploadProgress = {
  phase: 'single' | 'collection';
  currentFile: string;
  filesCompleted: number;
  filesTotal: number;
  bytesLoaded: number;
  bytesTotal: number;
};

export type FolderOptions = Record<string, string[]>;

export const API = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export function formatBytes(bytes: number | null) {
  if (bytes === null) {
    return 'n/a';
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

/**
 * Builds a sorted folder -> subfolders map from the job list. Existing entries
 * are preserved so locally-created folders survive a refresh that has not yet
 * produced jobs for them.
 */
export function buildFolderOptions(previous: FolderOptions, jobs: Job[]): FolderOptions {
  const map = new Map<string, Set<string>>();

  for (const [folderName, subfolders] of Object.entries(previous)) {
    const set = map.get(folderName) ?? new Set<string>();
    for (const entry of subfolders) {
      if (entry.trim()) set.add(entry.trim());
    }
    map.set(folderName, set);
  }

  for (const job of jobs) {
    const folderName = (job.processing_info?.settings?.folder ?? '').trim();
    const subfolderName = (job.processing_info?.settings?.subfolder ?? '').trim();
    if (!folderName) continue;
    const set = map.get(folderName) ?? new Set<string>();
    if (subfolderName) set.add(subfolderName);
    map.set(folderName, set);
  }

  const next: FolderOptions = {};
  const sortedFolders = Array.from(map.keys()).sort((a, b) => a.localeCompare(b));
  for (const folderName of sortedFolders) {
    next[folderName] = Array.from(map.get(folderName) ?? []).sort((a, b) => a.localeCompare(b));
  }
  return next;
}

/**
 * POSTs a FormData payload via XHR so upload progress events can be reported.
 */
export function sendFormDataWithProgress(
  url: string,
  formData: FormData,
  onProgress?: (loaded: number, total: number) => void,
) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(event.loaded, event.total);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
        return;
      }
      reject(new Error(`Upload failed with status ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('Network error while uploading'));
    xhr.send(formData);
  });
}
