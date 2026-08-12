import { useMemo, useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import type { Highlight, HighlightProfile, MenuItem } from '../types';
import { Empty, ErrorState, Loading, Panel } from './Common';

const LIMIT = 200;

function formatPrice(price: number | null | undefined): string {
  const n = Number(price ?? 0);
  if (!n) return '—';
  // Lagos menus are usually thousands; small integers are often EUR/$ as printed.
  if (n >= 1000) {
    return `₦${n.toLocaleString('en-NG')}`;
  }
  return String(n);
}

function groupBySection(items: MenuItem[]): { section: string; items: MenuItem[] }[] {
  const map = new Map<string, MenuItem[]>();
  for (const item of items) {
    const key = (item.section || item.category || 'Other').trim() || 'Other';
    const list = map.get(key);
    if (list) list.push(item);
    else map.set(key, [item]);
  }
  return [...map.entries()].map(([section, rows]) => ({ section, items: rows }));
}

function HighlightCard({ item }: { item: Highlight }) {
  const [imgFailed, setImgFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const showImage = item.coverUrl && !imgFailed;
  const menuItems = item.menuItems ?? [];
  const count = item.menuItemCount ?? menuItems.length;
  const sections = useMemo(() => groupBySection(menuItems), [menuItems]);

  return (
    <article className={`menu-card${open ? ' menu-card--open' : ''}`}>
      <button
        type="button"
        className="menu-card__toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
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
            {count > 0 ? (
              <span className="menu-card__count">
                {count} item{count === 1 ? '' : 's'}
              </span>
            ) : (
              <span>no items yet</span>
            )}
            {item.mediaCount != null && <span>{item.mediaCount} slides</span>}
            <span className="menu-card__kind">{item.kind === 'menu' ? 'menu' : 'highlight'}</span>
            <span className="menu-card__chev">{open ? '▾' : '▸'}</span>
          </div>
        </div>
      </button>

      {open && (
        <div className="menu-card__detail">
          {item.permalink && (
            <a
              className="menu-card__ig"
              href={item.permalink}
              target="_blank"
              rel="noreferrer noopener"
            >
              Open on Instagram
            </a>
          )}
          {menuItems.length === 0 ? (
            <p className="menu-card__empty">
              No extracted items — highlight may be promo video only, or waiting for menu backfill.
            </p>
          ) : (
            sections.map((sec) => (
              <div key={sec.section} className="menu-section">
                <h4 className="menu-section__title">{sec.section}</h4>
                <div className="table-wrap">
                  <table className="table menu-table">
                    <thead>
                      <tr>
                        <th>Item</th>
                        <th>Type</th>
                        <th>Category</th>
                        <th>Price</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sec.items.map((row) => (
                        <tr key={row._id}>
                          <td>
                            <div className="menu-table__name">{row.itemName}</div>
                            {row.description ? (
                              <div className="menu-table__desc">{row.description}</div>
                            ) : null}
                          </td>
                          <td>{row.type}</td>
                          <td>{row.category.replace(/_/g, ' ')}</td>
                          <td className="menu-table__price">{formatPrice(row.price)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </article>
  );
}

function ProfileBlock({ profile }: { profile: HighlightProfile }) {
  const [open, setOpen] = useState(true);
  const itemTotal = profile.menuItemCount ?? 0;
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
            {itemTotal > 0 ? ` · ${itemTotal} items` : ''}
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
  const itemTotal = profiles.reduce((n, p) => n + (p.menuItemCount ?? 0), 0);

  return (
    <Panel
      title="Menus"
      hint={
        data
          ? `${data.total.toLocaleString()} highlight${data.total === 1 ? '' : 's'}${
              itemTotal ? ` · ${itemTotal.toLocaleString()} items` : ''
            }${data.menusOnly ? ' · menus filter on' : ''}`
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
            placeholder="redbarlagos"
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
