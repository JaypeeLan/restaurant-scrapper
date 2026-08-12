import { useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import type { Highlight, HighlightProfile } from '../types';
import { Empty, ErrorState, Loading, Panel } from './Common';

const LIMIT = 200;

function HighlightCard({ item }: { item: Highlight }) {
  const [imgFailed, setImgFailed] = useState(false);
  const showImage = item.coverUrl && !imgFailed;

  return (
    <article className="menu-card">
      {showImage ? (
        <img
          className="menu-card__cover"
          src={item.coverUrl ?? ''}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="menu-card__cover-fallback">no cover</div>
      )}
      <div className="menu-card__body">
        <h3 className="menu-card__title">{item.title || 'Untitled highlight'}</h3>
        <div className="menu-card__meta">
          {item.mediaCount != null && <span>{item.mediaCount} slides</span>}
          <span className="menu-card__kind">{item.kind === 'menu' ? 'menu' : 'highlight'}</span>
          {item.permalink && (
            <a href={item.permalink} target="_blank" rel="noreferrer noopener">
              Open on Instagram
            </a>
          )}
        </div>
      </div>
    </article>
  );
}

function ProfileBlock({ profile }: { profile: HighlightProfile }) {
  const [open, setOpen] = useState(true);
  return (
    <section className="event-profile">
      <button
        type="button"
        className="event-profile__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div>
          <div className="event-profile__handle">@{profile.handle}</div>
          <div className="event-profile__name">
            {profile.menuCount} menu tray{profile.menuCount === 1 ? '' : 's'}
            {profile.highlightCount > profile.menuCount
              ? ` · ${profile.highlightCount} shown`
              : ''}
          </div>
        </div>
        <span className="event-profile__count">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="menu-grid event-profile__list">
          {profile.highlights.map((h) => (
            <HighlightCard key={h.id} item={h} />
          ))}
        </div>
      )}
    </section>
  );
}

export function MenuBoard() {
  const [handle, setHandle] = useState('');
  const [search, setSearch] = useState('');
  const [menusOnly, setMenusOnly] = useState(true);

  const debouncedHandle = useDebounced(handle, 350);
  const debouncedSearch = useDebounced(search, 350);

  const { data, loading, error, reload } = useFetch(
    (signal) =>
      api.highlights(
        {
          handle: debouncedHandle || undefined,
          q: debouncedSearch || undefined,
          menus_only: menusOnly,
          grouped: true,
          limit: LIMIT,
          skip: 0,
        },
        signal,
      ),
    [debouncedHandle, debouncedSearch, menusOnly],
  );

  const profiles = data?.profiles ?? [];

  return (
    <Panel
      title="Menus"
      hint={
        data
          ? `${data.total.toLocaleString()} highlight${data.total === 1 ? '' : 's'}${
              data.menusOnly ? ' · menus filter on' : ''
            }`
          : undefined
      }
      action={
        <button className="btn" type="button" onClick={reload} disabled={loading}>
          Refresh
        </button>
      }
    >
      <div className="filters">
        <div className="filters__field">
          <label htmlFor="menu-handle">Handle</label>
          <input
            id="menu-handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            placeholder="shirolagos"
            autoComplete="off"
          />
        </div>
        <div className="filters__field">
          <label htmlFor="menu-title">Title</label>
          <input
            id="menu-title"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="menu, drinks…"
            autoComplete="off"
          />
        </div>
        <label className="filters__check">
          <input
            type="checkbox"
            checked={menusOnly}
            onChange={(e) => setMenusOnly(e.target.checked)}
          />
          <span>Menus only</span>
        </label>
      </div>

      {loading && !data && <Loading label="Loading menus…" />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && profiles.length === 0 && (
        <Empty label="No menu highlights yet — try clearing Menus only, or wait for the next ingest." />
      )}

      <div className="event-board">
        {profiles.map((p) => (
          <ProfileBlock key={p.handle} profile={p} />
        ))}
      </div>
    </Panel>
  );
}
