/**
 * Types + tiny helpers for the Confluence import surface.
 * Field names mirror backend/app/schemas/import_.py exactly.
 */

export type ImportAuthType = 'cloud_basic' | 'pat_bearer';

export type ImportRunStatus = 'pending' | 'running' | 'finished' | 'failed' | 'cancelled';

export type ImportJobStatus = 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';

export type ImportSource = {
  id: string;
  name: string;
  base_url: string;
  server_kind: string;
  auth_type: ImportAuthType;
  auth_username: string;
  has_credential: boolean;
  last_validated_at: string | null;
  created_at: string;
};

export type ImportSourceListResponse = {
  items: ImportSource[];
};

export type ImportSourceTestResponse = {
  ok: boolean;
  detail: string | null;
  server_kind: string | null;
};

export type ImportRun = {
  id: string;
  kind: string;
  status: ImportRunStatus;
  scope_type: string;
  scope_value: string;
  root_page_title: string;
  pages_discovered: number;
  pages_imported: number;
  pages_failed: number;
  attachments_saved: number;
  artifact_bytes: number;
  content_bytes: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  owner: { id: string; username: string } | null;
};

export type ImportRunListResponse = {
  items: ImportRun[];
};

export type ImportRunError = {
  page_id: string;
  title: string;
  error: string;
};

export type ImportRunJobSummary = {
  id: string;
  title: string;
  status: ImportJobStatus;
};

export type ImportRunDetail = ImportRun & {
  current_page_title: string;
  error_message: string | null;
  cancel_requested: boolean;
  errors: ImportRunError[];
  jobs: ImportRunJobSummary[];
};

export type ImportRunCancelResponse = {
  id: string;
  status: ImportRunStatus;
  cancel_requested: boolean;
};

export const RUN_ACTIVE_STATUSES: ImportRunStatus[] = ['pending', 'running'];

export function isRunActive(status: ImportRunStatus): boolean {
  return RUN_ACTIVE_STATUSES.includes(status);
}

/** Status chip classes, matching the job-status badge visual language. */
export const runStatusChip: Record<ImportRunStatus, string> = {
  pending: 'bg-slate-100 text-slate-700',
  running: 'bg-sky-100 text-sky-800',
  finished: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-amber-100 text-amber-800',
};

export const importJobStatusChip: Record<ImportJobStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-sky-100 text-sky-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-100 text-red-700',
};

export function runScopeLabel(run: Pick<ImportRun, 'scope_type' | 'scope_value'>): string {
  return run.scope_type === 'space' ? `Space ${run.scope_value}` : `Page ${run.scope_value}`;
}

export function runTitle(run: Pick<ImportRun, 'root_page_title' | 'scope_type' | 'scope_value'>): string {
  return run.root_page_title.trim() || runScopeLabel(run);
}

/**
 * Matches import_test_cooldown_seconds' documented default: the 429 from
 * POST /import/sources/{id}/test carries no Retry-After header, so the
 * client falls back to this window before re-enabling the button.
 */
export const TEST_COOLDOWN_FALLBACK_MS = 10_000;
