/**
 * Typed API client + a small fetch hook.
 *
 * Deliberately no data-fetching library. There are five endpoints, all reads,
 * and the interesting behaviour (abort on unmount, abort on param change) is
 * about twenty lines. Adding React Query here would be more config than code.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  Account,
  AccountQuery,
  Capacity,
  EventsResponse,
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

/**
 * Runs `fn` whenever `deps` change, aborting any in-flight request first.
 *
 * The abort matters here: typing in the caption search fires a request per
 * keystroke, and without it a slow early response can land after a fast later
 * one and overwrite the correct results.
 */
export function useFetch<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): FetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setLoading(true);
    setError(null);

    fnRef
      .current(controller.signal)
      .then((result) => {
        if (!active) return;
        setData(result);
        setError(null);
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
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

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
