'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { LoaderCircle, RefreshCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import {
  mailAggregateStatus,
  mailDisplayDate,
  mailPartsSummary,
  type MailMessage,
  type MailMessageListResponse,
} from '@/lib/mail';

const PAGE_SIZE = 50;

type Filters = {
  query: string;
  source: string;
  fromDate: string;
  toDate: string;
};
export default function MailPage() {
  const [items, setItems] = useState<MailMessage[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [query, setQuery] = useState('');
  const [source, setSource] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');

  // `filters`/`offsetOverride` let callers (Reset, pagination) fetch with
  // values other than this render's state, since setters only land on the
  // next render — same pattern as document-browser.tsx's loadItems.
  //
  // Deliberately does not flip `loading` on here (only off, in `finally`):
  // the mount effect below calls this directly, and setting state
  // synchronously before the first await inside an effect-invoked function
  // trips the set-state-in-effect lint rule. `loading` starts `true`, and
  // every other call site (Refresh, Apply, Reset, pagination) flips it on
  // itself before calling in — same convention as admin/logs-tab.tsx.
  const loadItems = async (filters?: Filters, offsetOverride?: number) => {
    const active = filters ?? { query, source, fromDate, toDate };
    const nextOffset = offsetOverride ?? offset;
    const params = new URLSearchParams();
    if (active.query.trim()) {
      params.set('q', active.query.trim());
    }
    if (active.source.trim()) {
      params.set('source', active.source.trim());
    }
    if (active.fromDate) {
      params.set('from_date', active.fromDate);
    }
    if (active.toDate) {
      params.set('to_date', active.toDate);
    }
    params.set('limit', String(PAGE_SIZE));
    params.set('offset', String(nextOffset));

    try {
      const payload = await apiJson<MailMessageListResponse>(`/api/v1/mail/messages?${params.toString()}`, {
        cache: 'no-store',
      });
      setItems(payload.items);
      setTotal(payload.total);
      setOffset(nextOffset);
      setLoadError(null);
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.detail : 'Failed to load mail messages.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const run = async () => {
      // Load only API-ingested mails (source=api)
      await loadItems({ query: '', source: 'api', fromDate: '', toDate: '' }, 0);
      // Update the source filter display to api
      setSource('api');
    };
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyFilters = () => {
    setLoading(true);
    void loadItems(undefined, 0);
  };

  const resetFilters = () => {
    setQuery('');
    setSource('api');
    setFromDate('');
    setToDate('');
    setLoading(true);
    void loadItems({ query: '', source: 'api', fromDate: '', toDate: '' }, 0);
  };

  const goToOffset = (nextOffset: number) => {
    setLoading(true);
    void loadItems(undefined, nextOffset);
  };

  const rangeStart = items.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Mail</h1>
            <p className="mt-2 text-slate-600">
              API-ingested email messages
            </p>
          </div>
          <Button
            variant="outline"
            onClick={() => {
              setLoading(true);
              void loadItems();
            }}
            disabled={loading}
          >
            <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
          </Button>
        </section>

        {loadError && (
          <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</p>
        )}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className="text-sm text-slate-700 xl:col-span-2">
              Search subject / from
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && applyFilters()}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-300 focus:bg-white"
                placeholder="quarterly report, alice@partner.example"
              />
            </label>
            <label className="text-sm text-slate-700">
              Source
              <input
                value={source}
                readOnly
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-100 px-3 py-2 text-slate-950 outline-none cursor-not-allowed"
                title="This page displays API-ingested messages only"
              />
            </label>
            <div />
            <label className="text-sm text-slate-700">
              From date
              <input
                type="date"
                value={fromDate}
                onChange={(event) => setFromDate(event.target.value)}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
              />
            </label>
            <label className="text-sm text-slate-700">
              To date
              <input
                type="date"
                value={toDate}
                onChange={(event) => setToDate(event.target.value)}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
              />
            </label>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button onClick={applyFilters}>Apply Filters</Button>
            <Button variant="outline" onClick={resetFilters}>
              Reset
            </Button>
          </div>
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Messages</h2>
            <p className="text-sm text-slate-500">{total} message(s)</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full table-auto text-left text-xs sm:text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 font-medium">Subject</th>
                  <th className="pb-2 font-medium">From</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Date</th>
                  <th className="hidden pb-2 font-medium sm:table-cell">Source</th>
                  <th className="pb-2 font-medium">Parts</th>
                  <th className="pb-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((message) => {
                  const status = mailAggregateStatus(message.parts);
                  return (
                    <tr key={message.id} className="border-t border-slate-100">
                      <td className="py-3">
                        <Link
                          href={`/mail/${message.id}`}
                          className="line-clamp-2 font-medium text-slate-950 hover:text-emerald-700"
                        >
                          {message.subject.trim() || '(no subject)'}
                        </Link>
                        {message.rfc_message_id && (
                          <p className="mt-1 truncate text-xs text-slate-500" title={message.rfc_message_id}>
                            {message.rfc_message_id}
                          </p>
                        )}
                      </td>
                      <td className="py-3 text-slate-700">{message.from_address || '-'}</td>
                      <td className="hidden py-3 text-slate-700 md:table-cell">
                        {new Date(mailDisplayDate(message)).toLocaleString()}
                      </td>
                      <td className="hidden py-3 text-slate-700 sm:table-cell">{message.source || '-'}</td>
                      <td className="py-3 text-slate-700">{mailPartsSummary(message.parts)}</td>
                      <td className="py-3">
                        <span className={`rounded px-2 py-1 text-xs ${status.chipClass}`}>{status.label}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {items.length === 0 && !loading && (
              <div className="py-6 text-center">
                <p className="text-sm text-slate-600">No ingested messages yet. Send raw emails via POST /api/v1/mail/messages to see them here.</p>
              </div>
            )}
            {loading && (
              <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
                <LoaderCircle className="h-4 w-4 animate-spin" /> Loading mail...
              </div>
            )}
            {items.length > 0 && (
              <div className="mt-4 flex items-center justify-between gap-3 text-sm text-slate-600">
                <p>
                  Showing {rangeStart}-{rangeEnd} of {total}
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0 || loading}
                    onClick={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={rangeEnd >= total || loading}
                    onClick={() => goToOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
