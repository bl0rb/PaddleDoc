'use client';

/**
 * Tiny SWR-style module-level cache for JSON GET fetches, shared by client
 * components that render the same data on different routes (paddle
 * capabilities/settings, jobs list, dashboard stats, ...) — deliberately
 * dependency-free (no react-query/swr) per project constraints.
 *
 * What this buys over a plain `useEffect(() => { fetch... }, [])` per
 * component:
 *  - Stale-while-revalidate: remounting a component that requested `key`
 *    recently (e.g. navigating Home -> Jobs -> Home) paints the last-known
 *    value immediately instead of resetting to a loading/empty state, then
 *    revalidates in the background.
 *  - Dedupe: two components requesting the same `key` in the same tick
 *    (e.g. the jobs list feeding both a table and a folder sidebar) share
 *    one in-flight request instead of firing two.
 *  - A request failure never clears what's already on screen — the cache
 *    keeps the last-known-good value and callers can surface the error
 *    separately.
 *
 * Not a general HTTP cache: there is no automatic cross-tab sync, no
 * revalidate-on-reconnect, and callers own their own polling cadence via
 * {@link useVisiblePolling}.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';

type CacheEntry<T> = {
  data: T;
  updatedAt: number;
  promise: Promise<T> | null;
};

const cache = new Map<string, CacheEntry<unknown>>();
const listeners = new Map<string, Set<() => void>>();

function notify(key: string): void {
  listeners.get(key)?.forEach((cb) => cb());
}

function subscribe(key: string, cb: () => void): () => void {
  let set = listeners.get(key);
  if (!set) {
    set = new Set();
    listeners.set(key, set);
  }
  set.add(cb);
  return () => {
    set?.delete(cb);
    if (set && set.size === 0) listeners.delete(key);
  };
}

/** Read the last cached value for `key`, if any — never triggers a fetch. */
export function peekCached<T>(key: string): T | undefined {
  return cache.get(key)?.data as T | undefined;
}

/**
 * Runs `fetcher` for `key`, deduping concurrent callers and updating the
 * shared cache on success. On failure the cache is left untouched (so the
 * last-known-good value keeps showing) and the rejection propagates to the
 * caller.
 */
export function loadCached<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const existing = cache.get(key) as CacheEntry<T> | undefined;
  if (existing?.promise) {
    return existing.promise;
  }
  const promise = fetcher()
    .then((data) => {
      cache.set(key, { data, updatedAt: Date.now(), promise: null });
      notify(key);
      return data;
    })
    .catch((err) => {
      const entry = cache.get(key) as CacheEntry<T> | undefined;
      if (entry) {
        cache.set(key, { ...entry, promise: null });
      }
      throw err;
    });
  cache.set(key, { data: existing?.data as T, updatedAt: existing?.updatedAt ?? 0, promise });
  return promise;
}

/** Drops the cached value for `key` and notifies subscribers (e.g. after a mutation). */
export function invalidateCached(key: string): void {
  cache.delete(key);
  notify(key);
}

/**
 * Writes `data` into the cache directly and notifies subscribers — for
 * when a mutation response (e.g. a settings PUT) already carries the
 * authoritative new value, so callers can update every subscribed view
 * without an extra round trip.
 */
export function setCached<T>(key: string, data: T): void {
  cache.set(key, { data, updatedAt: Date.now(), promise: null });
  notify(key);
}

export interface UseCachedResourceOptions {
  /** How long a cached value is considered fresh enough to skip an automatic revalidate. Default 10s. */
  ttlMs?: number;
  /** Skip fetching entirely (e.g. a required id/filter is not ready yet). */
  enabled?: boolean;
}

export interface CachedResource<T> {
  data: T | undefined;
  /** True only while there is no cached value yet to show (first-ever load for this key). */
  isLoading: boolean;
  error: Error | null;
  /**
   * True when `data` is showing but the most recent revalidate for this key
   * rejected — i.e. what's on screen is last-known-good, not confirmed
   * current. Cleared as soon as a revalidate for this key succeeds again.
   * Callers that render live/health-sensitive data should check this rather
   * than assuming a present `data` value means "confirmed up to date".
   */
  isStale: boolean;
  /** Re-runs the fetcher in the background; resolves once the cache (or error) settles. */
  revalidate: () => Promise<void>;
}

const NO_SUBSCRIPTION = () => () => {};

/**
 * Subscribes to `key` in the shared cache, fetching via `fetcher` when the
 * cached value is missing or older than `ttlMs`. Returns the last-known
 * value synchronously on mount when present, so remounted views render
 * instantly instead of flashing an empty state.
 *
 * `data` is read straight from the module-level cache via
 * `useSyncExternalStore` — the cache is an external mutable store, not
 * React state, so this avoids the "setState in effect" ping-pong a
 * useState+useEffect mirror would otherwise need.
 */
export function useCachedResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  { ttlMs = 10_000, enabled = true }: UseCachedResourceOptions = {},
): CachedResource<T> {
  const getSnapshot = useCallback(() => (key ? peekCached<T>(key) : undefined), [key]);
  const subscribeToKey = useCallback(
    (onStoreChange: () => void) => (key ? subscribe(key, onStoreChange) : NO_SUBSCRIPTION()),
    [key],
  );
  const data = useSyncExternalStore(subscribeToKey, getSnapshot, getSnapshot);

  const [error, setError] = useState<Error | null>(null);
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const revalidate = useCallback((): Promise<void> => {
    if (!key || !enabled) return Promise.resolve();
    return loadCached(key, () => fetcherRef.current())
      .then(() => setError(null))
      .catch((err: unknown) => setError(err instanceof Error ? err : new Error('Request failed')));
  }, [key, enabled]);

  useEffect(() => {
    if (!key || !enabled) return;
    const entry = cache.get(key);
    const isFresh = Boolean(entry) && Date.now() - (entry?.updatedAt ?? 0) < ttlMs;
    if (!isFresh) {
      void revalidate();
    }
    // ttlMs intentionally only gates the freshness check when this effect
    // runs (mount, or key/enabled change) — it is not meant to re-trigger
    // a fetch merely because the ttl configuration value itself changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, revalidate]);

  return {
    data,
    isLoading: Boolean(key && enabled) && data === undefined,
    error,
    isStale: Boolean(error) && data !== undefined,
    revalidate,
  };
}

/**
 * Calls `callback` on an interval, paused while the tab is hidden and
 * immediately refreshed when it regains visibility — avoids burning
 * requests (and re-render work) on backgrounded tabs. Pass `intervalMs:
 * null` to disable polling entirely (e.g. nothing left to watch).
 */
export function useVisiblePolling(callback: () => void, intervalMs: number | null): void {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  });

  useEffect(() => {
    if (!intervalMs || typeof document === 'undefined') return;

    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer === null) {
        timer = setInterval(() => callbackRef.current(), intervalMs);
      }
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const onVisibilityChange = () => {
      if (document.hidden) {
        stop();
      } else {
        callbackRef.current();
        start();
      }
    };

    if (!document.hidden) {
      start();
    }
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [intervalMs]);
}
