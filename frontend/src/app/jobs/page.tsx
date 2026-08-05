import Link from 'next/link';

import { Button } from '@/components/ui/button';
import { DocumentBrowser } from '@/components/document-browser';

export default function JobsPage() {
  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-serif text-3xl font-semibold text-slate-950">Tasks</h1>
          <Link href="/processing#upload-flow">
            <Button>New Task</Button>
          </Link>
        </div>
        <DocumentBrowser
          title="Tasks"
          description="Browse tasks, filter documents, and download processed markdown results."
          endpoint="jobs"
          allowDelete
          includeDateFilters
        />
      </div>
    </main>
  );
}
