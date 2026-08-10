import type { ReactNode } from 'react';

export function Panel({
  title,
  hint,
  action,
  children,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <h2 className="panel__title">{title}</h2>
          {hint && <p className="panel__hint">{hint}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state">
      <span className="spinner" aria-hidden />
      <div style={{ marginTop: 8 }}>{label}</div>
    </div>
  );
}

/**
 * Error state that distinguishes "backend is down" from "query was bad".
 * A 503 from serve.py means Mongo is unreachable, which is by far the most
 * common thing to hit while setting this up — so it gets a specific hint.
 */
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const looksLikeBackendDown =
    /failed to fetch|networkerror|mongo unavailable|load failed/i.test(message);

  return (
    <div className="state state--error">
      <div>{message}</div>
      {looksLikeBackendDown && (
        <div style={{ marginTop: 8 }}>
          Start the API with <code>uvicorn serve:app --port 8000</code>
        </div>
      )}
      {onRetry && (
        <button className="btn" style={{ marginTop: 12 }} onClick={onRetry} type="button">
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ label }: { label: string }) {
  return <div className="state">{label}</div>;
}

export function Badge({
  kind,
  children,
}: {
  kind: string;
  children: ReactNode;
}) {
  return <span className={`badge badge--${kind}`}>{children}</span>;
}

export function Pager({
  skip,
  limit,
  total,
  onChange,
}: {
  skip: number;
  limit: number;
  total: number;
  onChange: (skip: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(skip / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <div className="pager">
      <button
        className="btn"
        type="button"
        disabled={skip <= 0}
        onClick={() => onChange(Math.max(0, skip - limit))}
      >
        ← Prev
      </button>
      <span className="pager__label">
        {page} / {pages}
      </span>
      <button
        className="btn"
        type="button"
        disabled={skip + limit >= total}
        onClick={() => onChange(skip + limit)}
      >
        Next →
      </button>
    </div>
  );
}
