import { API_BASE_URL } from '@/lib/api-base';

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

export const API = API_BASE_URL;

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

/** Body shape of the 409 duplicate-upload response (see UploadError). */
export type DuplicateUploadBody = {
  detail: string;
  duplicate_of: string;
  existing_version: number;
};

/**
 * Thrown by {@link sendFormDataWithProgress} for a non-2xx response. `body`
 * is the parsed JSON error body (or null if unparsable) — callers that need
 * to react to a specific status (e.g. 409 duplicate upload) can narrow it
 * themselves, e.g. `error.body as Partial<DuplicateUploadBody>`.
 */
export class UploadError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = 'UploadError';
    this.status = status;
    this.body = body;
  }
}

/**
 * POSTs a FormData payload via XHR so upload progress events can be
 * reported. Resolves with the parsed JSON response body (or null if the
 * body is empty/unparsable) on a 2xx response. Rejects with
 * {@link UploadError} on a non-2xx response, carrying the status and parsed
 * body so callers can read fields like `duplicate_of`/`existing_version`.
 */
export function sendFormDataWithProgress(
  url: string,
  formData: FormData,
  onProgress?: (loaded: number, total: number) => void,
): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(event.loaded, event.total);
      }
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body);
        return;
      }
      if (xhr.status === 401 && typeof window !== 'undefined') {
        window.location.assign('/login');
      }
      const detail =
        body && typeof body === 'object' && typeof (body as Record<string, unknown>).detail === 'string'
          ? ((body as Record<string, unknown>).detail as string)
          : `Upload failed with status ${xhr.status}`;
      reject(new UploadError(xhr.status, detail, body));
    };
    xhr.onerror = () => reject(new Error('Network error while uploading'));
    xhr.send(formData);
  });
}
