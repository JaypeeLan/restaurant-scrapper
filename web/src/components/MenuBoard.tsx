import { useMemo, useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import { formatNaira } from '../lib/naira';
import type { Highlight, HighlightProfile, MenuItem } from '../types';
import { Empty, ErrorState, Loading, Panel } from './Common';

const LIMIT = 200;

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

interface MenuSourceLinkView {
  type: string;
  label: string;
  href: string;
}

function menuSources(item: Highlight): MenuSourceLinkView[] {
  if (item.sources?.length) {
    return item.sources.map((s) => ({
      type: s.type,
      label: s.label,
      href: s.href,
    }));
  }

  const out: MenuSourceLinkView[] = [];
  const isWeb = item.sourceType === 'web';

  if (isWeb) {
    if (item.menuUrl) {
      const isPdf = item.menuUrl.toLowerCase().includes('.pdf');
      out.push({
        type: item.webSource === 'linktree' ? 'Linktree' : 'Website',
        label: isPdf ? `${item.title?.trim() || 'Menu'} · PDF` : item.title?.trim() || 'Menu page',
        href: item.menuUrl,
      });
    }
    if (item.sourceUrl && item.sourceUrl !== item.menuUrl) {
      out.push({
        type: 'Bio link',
        label: hostLabel(item.sourceUrl),
        href: item.sourceUrl,
      });
    }
    return out;
  }

  if (item.permalink) {
    out.push({
      type: 'Instagram',
      label: item.title?.trim() || 'Highlight tray',
      href: item.permalink,
    });
  }
  return out;
}

function hostLabel(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

function MenuTraySources({ sources }: { sources: MenuSourceLinkView[] }) {
  if (sources.length === 0) return null;

  return (
    <div className="menu-tray__sources">
      <span className="menu-tray__sources-label">Source{sources.length === 1 ? '' : 's'}</span>
      <ul className="menu-source-list">
        {sources.map((src) => (
          <li key={src.href}>
            <a
              className="menu-source"
              href={src.href}
              target="_blank"
              rel="noreferrer noopener"
              onClick={(e) => e.stopPropagation()}
            >
              <span className="menu-source__type">{src.type}</span>
              <span className="menu-source__label">{src.label}</span>
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
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
      <div className="menu-item__price" title="Price (₦)">{formatNaira(row.price)}</div>
    </div>
  );
}

function HighlightTray({ item }: { item: Highlight }) {
  const [open, setOpen] = useState(false);
  const menuItems = item.menuItems ?? [];
  const count = item.menuItemCount ?? menuItems.length;
  const sections = useMemo(() => groupBySection(menuItems), [menuItems]);
  const title = item.title?.trim() || 'Untitled tray';
  const sources = useMemo(() => menuSources(item), [item]);

  return (
    <article className={`menu-tray${open ? ' menu-tray--open' : ''}`}>
      <div className="menu-tray__header">
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
              {item.mediaCount != null && item.mediaCount > 0 && item.sourceType !== 'web' && (
                <span className="menu-tray__stat">{item.mediaCount} slides</span>
              )}
            </div>
          </div>
          <span className="menu-tray__chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
        </button>
        <MenuTraySources sources={sources} />
      </div>

      {open && (
        <div className="menu-tray__body">
          {menuItems.length === 0 ? (
            <p className="menu-tray__empty">
              {item.sourceType === 'web'
                ? 'Menu link found but items not extracted yet — PDF may be image-only or backfill pending.'
                : 'Items not extracted yet — tray may be video-only or waiting for the weekly menu backfill.'}
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
