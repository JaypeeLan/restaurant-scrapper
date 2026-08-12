import { useMemo, useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import type { Highlight, HighlightProfile, MenuItem } from '../types';
import { Empty, ErrorState, Loading, Panel } from './Common';

const LIMIT = 200;

function formatPrice(price: number | null | undefined): string {
  const n = Number(price ?? 0);
  if (!n) return '—';
  if (n >= 1000) {
    return `₦${n.toLocaleString('en-NG')}`;
  }
  return String(n);
}

function formatCategory(category: string): string {
  return category.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
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

function MenuItemRow({ row }: { row: MenuItem }) {
  return (
    <div className="menu-item">
      <div className="menu-item__main">
        <div className="menu-item__name">{row.itemName}</div>
        {row.description ? <p className="menu-item__desc">{row.description}</p> : null}
        <div className="menu-item__tags">
          <span className="badge badge--muted">{row.type}</span>
          <span className="badge badge--muted">{formatCategory(row.category)}</span>
        </div>
      </div>
      <div className="menu-item__price">{formatPrice(row.price)}</div>
    </div>
  );
}

function HighlightTray({ item }: { item: Highlight }) {
  const [open, setOpen] = useState(false);
  const menuItems = item.menuItems ?? [];
  const count = item.menuItemCount ?? menuItems.length;
  const sections = useMemo(() => groupBySection(menuItems), [menuItems]);
  const title = item.title?.trim() || 'Untitled tray';

  return (
    <article className={`menu-tray${open ? ' menu-tray--open' : ''}`}>
      <button
        type="button"
        className="menu-tray__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="menu-tray__info">
          <h3 className="menu-tray__title">{title}</h3>
          <div className="menu-tray__meta">
            {count > 0 ? (
              <span className="menu-tray__stat">
                {count} item{count === 1 ? '' : 's'}
              </span>
            ) : (
              <span className="menu-tray__stat menu-tray__stat--muted">No items yet</span>
            )}
            {item.mediaCount != null && item.mediaCount > 0 && (
              <span className="menu-tray__stat">{item.mediaCount} slides</span>
            )}
          </div>
        </div>
        <span className="menu-tray__chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="menu-tray__body">
          {item.permalink && (
            <a
              className="menu-tray__link"
              href={item.permalink}
              target="_blank"
              rel="noreferrer noopener"
            >
              View highlight on Instagram
            </a>
          )}
          {menuItems.length === 0 ? (
            <p className="menu-tray__empty">
              Items not extracted yet — tray may be video-only or waiting for the weekly menu
              backfill.
            </p>
          ) : (
            sections.map((sec) => (
              <section key={sec.section} className="menu-section">
                <h4 className="menu-section__title">{sec.section}</h4>
                <div className="menu-section__items">
                  {sec.items.map((row) => (
                    <MenuItemRow key={row._id} row={row} />
                  ))}
                </div>
              </section>
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
  const trayLabel =
    profile.menuCount === 1 ? '1 tray' : `${profile.menuCount} trays`;

  return (
    <section className="menu-profile">
      <button
        type="button"
        className="menu-profile__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="menu-profile__info">
          <div className="menu-profile__handle">@{profile.handle}</div>
          <div className="menu-profile__sub">
            {trayLabel}
            {itemTotal > 0 ? ` · ${itemTotal.toLocaleString()} items` : ''}
          </div>
        </div>
        <span className="menu-profile__chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="menu-tray-list">
          {profile.highlights.map((h) => (
            <HighlightTray key={h.id} item={h} />
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
    { key: 'highlights', ttlMs: 180_000 },
  );

  const profiles = data?.profiles ?? [];
  const itemTotal = profiles.reduce((n, p) => n + (p.menuItemCount ?? 0), 0);

  return (
    <Panel
      title="Menus"
      hint={
        data
          ? `${profiles.length} restaurant${profiles.length === 1 ? '' : 's'} · ${itemTotal.toLocaleString()} items`
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
          <label htmlFor="menu-handle">Restaurant</label>
          <input
            id="menu-handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            placeholder="@handle or name"
            autoComplete="off"
          />
        </div>
        <div className="filters__field">
          <label htmlFor="menu-title">Tray title</label>
          <input
            id="menu-title"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Food, cocktails…"
            autoComplete="off"
          />
        </div>
        <label className="filters__check">
          <input
            type="checkbox"
            checked={menusOnly}
            onChange={(e) => setMenusOnly(e.target.checked)}
          />
          <span>Menu trays only</span>
        </label>
      </div>

      {loading && !data && <Loading label="Loading menus…" />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && profiles.length === 0 && (
        <Empty label="No menu trays yet — try turning off “Menu trays only” or wait for ingest." />
      )}

      <div className="menu-board">
        {profiles.map((p) => (
          <ProfileBlock key={p.handle} profile={p} />
        ))}
      </div>
    </Panel>
  );
}
