import { lazy, Suspense, useEffect, useState } from 'react';
import { api, useFetch } from './api';
import { AccountTable } from './components/AccountTable';
import { Loading } from './components/Common';
import { EventBoard } from './components/EventBoard';
import { PostFeed } from './components/PostFeed';
import { SummaryBar } from './components/SummaryBar';

// Recharts is ~380 kB of the bundle and only two of the four tabs need it.
// Lazy-loading keeps the default Posts view fast on first paint.
const RunStats = lazy(() =>
  import('./components/RunStats').then((m) => ({ default: m.RunStats })),
);
const CapacityMonitor = lazy(() =>
  import('./components/CapacityMonitor').then((m) => ({ default: m.CapacityMonitor })),
);

const TABS = [
  { id: 'events', label: 'Experiences' },
  { id: 'posts', label: 'Posts' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'runs', label: 'Runs' },
  { id: 'capacity', label: 'Capacity' },
] as const;

type TabId = (typeof TABS)[number]['id'];

function isTabId(value: string): value is TabId {
  return TABS.some((t) => t.id === value);
}

export default function App() {
  // Tab lives in the hash so a view is linkable and survives reload.
  const [tab, setTab] = useState<TabId>(() => {
    const hash = window.location.hash.replace('#', '');
    return isTabId(hash) ? hash : 'events';
  });

  useEffect(() => {
    window.location.hash = tab;
  }, [tab]);

  useEffect(() => {
    const onHashChange = () => {
      const hash = window.location.hash.replace('#', '');
      if (isTabId(hash)) setTab(hash);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const { data: health } = useFetch((signal) => api.health(signal), []);

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <strong>Ingest</strong>
          <small>Instagram posts</small>
        </div>

        <nav className="tabs" aria-label="Views">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`tabs__btn${tab === t.id ? ' tabs__btn--active' : ''}`}
              onClick={() => setTab(t.id)}
              aria-current={tab === t.id ? 'page' : undefined}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="app__spacer" />

        <div className="app__status" title={health?.db ? `database: ${health.db}` : undefined}>
          {health ? (
            health.ok ? (
              <>
                <span className="status-dot status-dot--ok" aria-hidden />
                connected
              </>
            ) : (
              <>
                <span className="status-dot status-dot--bad" aria-hidden />
                offline
              </>
            )
          ) : (
            <span className="spinner" aria-hidden />
          )}
        </div>
      </header>

      <main className="app__main">
        <div className="stack">
          <SummaryBar />
          {tab === 'events' && <EventBoard />}
          {tab === 'posts' && <PostFeed />}
          {tab === 'accounts' && <AccountTable />}
          <Suspense fallback={<Loading label="Loading charts…" />}>
            {tab === 'runs' && <RunStats />}
            {tab === 'capacity' && <CapacityMonitor />}
          </Suspense>
        </div>
      </main>
    </div>
  );
}
