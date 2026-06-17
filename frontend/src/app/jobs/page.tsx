import { DocumentBrowser } from '@/components/document-browser';

export default function JobsPage() {
  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <DocumentBrowser
          title="Jobs"
          description="Browse jobs, filter documents, and download processed markdown results."
          endpoint="jobs"
          allowDelete
          includeDateFilters
        />
      </div>
    </main>
  );
}
