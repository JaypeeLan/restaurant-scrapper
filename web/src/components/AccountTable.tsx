import { useEffect, useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import { compactNumber, isOverdue, relativeTime, sourceLabel } from '../format';
import type { AccountQuery, Tier } from '../types';
import { Badge, Empty, ErrorState, Loading, Pager, Panel } from './Common';

const LIMIT = 50;
const TIERS: Tier[] = ['hot', 'warm', 'cold', 'dormant'];

export function AccountTable() {
  const [tier, setTier] = useState('');
  const [failing, setFailing] = useState(false);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<NonNullable<AccountQuery['sort']>>('nextFetchAt');
  const [skip, setSkip] = useState(0);

  const debouncedSearch = useDebounced(search, 350);

  useEffect(() => {
    setSkip(0);
  }, [debouncedSearch, tier, failing, sort]);

  const { data, loading, error, reload } = useFetch(
    (signal) =>
      api.accounts(
        {
          tier: tier || undefined,
          failing: failing || undefined,
          q: debouncedSearch || undefined,
          sort,
          limit: LIMIT,
          skip,
        },
        signal,
      ),
    [tier, failing, debouncedSearch, sort, skip],
    { key: 'accounts', ttlMs: 30_000 },
  );

  return (
    <Panel
      title="Accounts"
      hint={
        data
          ? `${data.total.toLocaleString()} accounts — failures climb when Graph can't resolve a handle`
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
          <label htmlFor="acct-q">Handle</label>
          <input
            id="acct-q"
            type="search"
            placeholder="search handles"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <div className="filters__field">
          <label htmlFor="acct-tier">Tier</label>
          <select id="acct-tier" value={tier} onChange={(e) => setTier(e.target.value)}>
            <option value="">All</option>
            {TIERS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>

        <div className="filters__field">
          <label htmlFor="acct-sort">Sort</label>
          <select
            id="acct-sort"
            value={sort}
            onChange={(e) => setSort(e.target.value as NonNullable<AccountQuery['sort']>)}
          >
            <option value="nextFetchAt">Next fetch</option>
            <option value="lastFetchedAt">Last fetched</option>
            <option value="newestPostedAt">Newest post</option>
            <option value="consecutiveFailures">Most failures</option>
            <option value="handle">Handle</option>
          </select>
        </div>

        <div className="filters__field">
          <label htmlFor="acct-failing">Health</label>
          <select
            id="acct-failing"
            value={failing ? 'failing' : ''}
            onChange={(e) => setFailing(e.target.value === 'failing')}
          >
            <option value="">All</option>
            <option value="failing">Failing (3+)</option>
          </select>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={reload} />}
      {!error && loading && !data && <Loading />}
      {!error && data && data.items.length === 0 && <Empty label="No accounts match." />}

      {!error && data && data.items.length > 0 && (
        <>
          <div className="table-wrap" style={{ opacity: loading ? 0.55 : 1 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Handle</th>
                  <th>Tier</th>
                  <th>Source</th>
                  <th className="table__num">Followers</th>
                  <th>Newest post</th>
                  <th>Last fetched</th>
                  <th>Next fetch</th>
                  <th className="table__num">Fails</th>
                  <th>Last error</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((a) => {
                  const overdue = isOverdue(a.nextFetchAt);
                  return (
                    <tr key={a.id}>
                      <td className="table__handle">@{a.handle}</td>
                      <td>
                        <Badge kind={a.tier}>{a.tier}</Badge>
                      </td>
                      <td>
                        {a.lastSource ? (
                          <Badge kind={a.lastSource}>{sourceLabel(a.lastSource)}</Badge>
                        ) : (
                          <span style={{ opacity: 0.4 }}>—</span>
                        )}
                      </td>
                      <td className="table__num">{compactNumber(a.profile?.followers ?? null)}</td>
                      <td>{relativeTime(a.newestPostedAt)}</td>
                      <td>{relativeTime(a.lastFetchedAt)}</td>
                      <td className={overdue ? 'table__overdue' : undefined}>
                        {overdue ? 'due now' : relativeTime(a.nextFetchAt)}
                      </td>
                      <td
                        className={`table__num ${
                          a.consecutiveFailures >= 3 ? 'table__danger' : ''
                        }`}
                      >
                        {a.consecutiveFailures || 0}
                      </td>
                      <td
                        title={a.lastError ?? ''}
                        style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}
                      >
                        {a.lastError ? (
                          <span className="table__danger">{a.lastError}</span>
                        ) : (
                          <span style={{ opacity: 0.4 }}>—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pager skip={skip} limit={LIMIT} total={data.total} onChange={setSkip} />
          <p className="note">
            An account stuck on high failures is usually private, personal, or renamed — Graph
            can&apos;t discover it and the fallback queue is capped, so it retries with
            exponential backoff rather than burning calls.
          </p>
        </>
      )}
    </Panel>
  );
}
