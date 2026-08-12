/**
 * Typed API client + a small fetch hook.
 *
 * Deliberately no data-fetching library. There are five endpoints, all reads,
 * and the interesting behaviour (abort on unmount, abort on param change,
 * short TTL keep-alive across tab remounts) is still cheaper than React Query.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  Account,
  AccountQuery,
  Capacity,
  EventsResponse,
  HighlightsResponse,
  Paged,
  Post,
  PostQuery,
  RunsResponse,
  Summary,
} from './types';

const BASE = import.meta.env.VITE_API_BASE ?? '/api';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function toQueryString(params: Record<string, unknown> = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    if (typeof value === 'boolean' && !value) continue;
    search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : '';
}

async function request<T>(
  path: string,
  params?: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(`${BASE}${path}${toQueryString(params)}`, { signal });

  if (!res.ok) {
    // FastAPI puts the useful part in `detail`; fall back to the status text.
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(detail, res.status);
  }

  return (await res.json()) as T;
}

export const api = {
  summary: (signal?: AbortSignal) => request<Summary>('/summary', undefined, signal),
  capacity: (signal?: AbortSignal) => request<Capacity>('/capacity', undefined, signal),
  posts: (q: PostQuery = {}, signal?: AbortSignal) =>
    request<Paged<Post>>('/posts', q as Record<string, unknown>, signal),
  events: (
    q: {
      handle?: string;
      min_score?: number;
      grouped?: boolean;
      limit?: number;
      skip?: number;
      llm?: boolean;
    } = {},
    signal?: AbortSignal,
  ) => request<EventsResponse>('/events', q as Record<string, unknown>, signal),
  highlights: (
    q: {
      handle?: string;
      q?: string;
      menus_only?: boolean;
      grouped?: boolean;
      limit?: number;
      skip?: number;
    } = {},
    signal?: AbortSignal,
  ) => request<HighlightsResponse>('/highlights', q as Record<string, unknown>, signal),
  accounts: (q: AccountQuery = {}, signal?: AbortSignal) =>
    request<Paged<Account>>('/accounts', q as Record<string, unknown>, signal),
  runs: (q: { limit?: number; skip?: number; kind?: string } = {}, signal?: AbortSignal) =>
    request<RunsResponse>('/runs', q as Record<string, unknown>, signal),
  health: (signal?: AbortSignal) =>
    request<{ ok: boolean; db?: string; error?: string }>('/health', undefined, signal),
};

export interface FetchState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export interface FetchCacheOptions {
  /** Stable id for this query family (e.g. "capacity", "events"). */
  key: string;
  /** How long a successful response stays warm across remounts. Default 60s. */
  ttlMs?: number;
}

const DEFAULT_CACHE_TTL_MS = 60_000;

type CacheEntry = { expires: number; data: unknown };
const _responseCache = new Map<string, CacheEntry>();

function cacheLookup<T>(id: string | null): T | null {
  if (!id) return null;
  const hit = _responseCache.get(id);
  if (!hit) return null;
  if (hit.expires <= Date.now()) {
    _responseCache.delete(id);
    return null;
  }
  return hit.data as T;
}

function cacheStore(id: string | null, data: unknown, ttlMs: number): void {
  if (!id || ttlMs <= 0) return;
  _responseCache.set(id, { expires: Date.now() + ttlMs, data });
}

/**
 * Runs `fn` whenever `deps` change, aborting any in-flight request first.
 *
 * The abort matters here: typing in the caption search fires a request per
 * keystroke, and without it a slow early response can land after a fast later
 * one and overwrite the correct results.
 *
 * Pass `cache` to keep the last good response warm across tab remounts so
 * switching Experiences → Menus → Capacity does not flash a loading state.
 * `reload()` always bypasses the cache.
 */
export function useFetch<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  cache?: FetchCacheOptions,
): FetchState<T> {
  const cacheId = cache ? `${cache.key}:${JSON.stringify(deps)}` : null;
  const ttlMs = cache?.ttlMs ?? DEFAULT_CACHE_TTL_MS;

  const [data, setData] = useState<T | null>(() => cacheLookup(cacheId));
  const [loading, setLoading] = useState(() => cacheLookup(cacheId) == null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const bypassOnceRef = useRef(false);

  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const bypass = bypassOnceRef.current;
    bypassOnceRef.current = false;

    if (!bypass) {
      const hit = cacheLookup<T>(cacheId);
      if (hit != null) {
        setData(hit);
        setError(null);
        setLoading(false);
        return () => {
          active = false;
          controller.abort();
        };
      }
    }

    setLoading(true);
    setError(null);

    fnRef
      .current(controller.signal)
      .then((result) => {
        if (!active) return;
        setData(result);
        setError(null);
        cacheStore(cacheId, result, ttlMs);
      })
      .catch((err: unknown) => {
        if (!active) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setError(err instanceof Error ? err.message : 'request failed');
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, cacheId, ttlMs]);

  const reload = useCallback(() => {
    if (cacheId) _responseCache.delete(cacheId);
    bypassOnceRef.current = true;
    setNonce((n) => n + 1);
  }, [cacheId]);

  return { data, loading, error, reload };
}

/** Delays a rapidly-changing value — used for the caption search box. */
export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}
