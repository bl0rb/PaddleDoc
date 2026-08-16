import { AlertCircle } from 'lucide-react';

import { PaddleDocLogo } from '@/components/paddledoc-logo';

/**
 * Centered full-page shell for the auth pages (/login, /setup):
 * PaddleDoc logo mark on top, white rounded card below.
 */
export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3">
          <PaddleDocLogo className="h-12 w-12 drop-shadow-md" />
          <span className="text-lg font-semibold text-slate-950">PaddleDoc</span>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
          <h1 className="text-lg font-semibold text-slate-950">{title}</h1>
          {subtitle ? <p className="mt-1 text-sm text-slate-500">{subtitle}</p> : null}
          <div className="mt-6">{children}</div>
        </div>
      </div>
    </main>
  );
}

/** Full-page spinner shown while an auth page runs its mount checks. */
export function AuthPageSpinner() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50">
      <div
        role="status"
        aria-label="Loading"
        className="h-8 w-8 animate-spin rounded-full border-2 border-slate-200 border-t-emerald-600"
      />
    </main>
  );
}

interface AuthFieldProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label: string;
  id: string;
}

/** Labeled text input matching the app's slate/emerald visual language. */
export function AuthField({ label, id, ...props }: AuthFieldProps) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        id={id}
        className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100"
        {...props}
      />
    </div>
  );
}

/** Inline error banner; renders nothing when `message` is null. */
export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-xl border border-red-100 bg-red-50 px-3 py-2.5 text-sm text-red-700"
    >
      <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}
