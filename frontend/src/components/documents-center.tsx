'use client';

import Link from 'next/link';

import { DocumentBrowser } from '@/components/document-browser';
import { Button } from '@/components/ui/button';
export function DocumentsCenter() {

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-8 text-slate-900 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-wrap gap-2">
        <Link href="/">
          <Button variant="outline" size="sm">Home</Button>
        </Link>
        <Link href="/">
          <Button variant="outline" size="sm">Upload</Button>
        </Link>
      </div>

      <DocumentBrowser
        title="Jobs and document search"
        description="Search across all jobs by filename, tag, and date. The separate search page has been merged into jobs."
        endpoint="jobs"
        allowDelete
        includeDateFilters
        hideHeader
      />
    </div>
  );
}
