import { useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, useFetch } from '../api';
import { dateTime, fullNumber, relativeTime } from '../format';
import type { Run, RunSchedule } from '../types';
import { Empty, ErrorState, Loading, Pager, Panel } from './Common';

const COLORS = {
  graph: '#3fb950',
  fallback: '#d29922',
  failed: '#f85149',
  neu: '#4f8cff',
  changed: '#a371f7',
};

interface ChartRow {
  label: string;
  graphOk: number;
  fallbackOk: number;
  failed: number;
  postsNew: number;
  postsChanged: number;
  durationS: number;
  successRate: number;
}

function isIngest(r: Run): boolean {
  return (r.kind ?? 'ingest') === 'ingest';
}

function toRows(runs: Run[]): ChartRow[] {
  return runs.filter(isIngest).map((r) => {
    const attempted = (r.graphOk ?? 0) + (r.fallbackOk ?? 0) + (r.failed ?? 0);
    return {
      label: dateTime(r.finishedAt),
      graphOk: r.graphOk ?? 0,
      fallbackOk: r.fallbackOk ?? 0,
      failed: r.failed ?? 0,
      postsNew: r.postsNew ?? 0,
      postsChanged: r.postsChanged ?? 0,
      durationS: r.durationS ?? 0,
      successRate: attempted
        ? Math.round(((attempted - (r.failed ?? 0)) / attempted) * 100)
        : 100,
    };
  });
}

function formatGap(minutes: number | null | undefined): string {
  if (minutes == null) return '—';
  if (minutes < 90) return `${Math.round(minutes)} min`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

function runDetail(r: Run): string {
  if (isIngest(r)) {
    const parts = [
      `${r.accounts ?? 0} accounts`,
      `G ${r.graphOk ?? 0}`,
      `F ${r.fallbackOk ?? 0}`,
      `X ${r.failed ?? 0}`,
      `+${r.postsNew ?? 0} posts`,
    ];
    if (r.postsChanged) parts.push(`~${r.postsChanged} changed`);
    return parts.join(' · ');
  }
  const parts = [
    r.city ?? '—',
    `${r.placesFound ?? 0} places`,
    `${r.resolved ?? 0}/${r.resolveAttempted ?? 0} handles`,
    `${r.seeded ?? 0} seeded`,
  ];
  return parts.join(' · ');
}

function SchedulePanel({
  schedule,
  lastIngestAt,
  lastDiscoverAt,
  observedIngestGapMinutes,
  observedDiscoverGapHours,
}: {
  schedule: RunSchedule;
  lastIngestAt: string | null;
  lastDiscoverAt: string | null;
  observedIngestGapMinutes: number | null;
  observedDiscoverGapHours: number | null;
}) {
  const tiers = schedule.tierIntervalsHours;
  return (
    <Panel
      title="Cadence"
      hint="Configured intervals vs what the last few runs actually did"
    >
      <div className="schedule-grid">
        <div className="schedule-card">
          <h3>Ingest</h3>
          <dl className="kv">
            <dt>Configured</dt>
            <dd>every {schedule.ingestEveryMinutes} min</dd>
            <dt>Accounts / run</dt>
            <dd>up to {schedule.ingestLimit}</dd>
            <dt>Last finished</dt>
            <dd title={lastIngestAt ?? undefined}>{relativeTime(lastIngestAt)}</dd>
            <dt>Observed gap</dt>
            <dd>{formatGap(observedIngestGapMinutes)}</dd>
            <dt>Primary source</dt>
            <dd>{schedule.primarySource === 'graph' ? 'Graph API' : 'Playwright'}</dd>
            <dt>Graph</dt>
            <dd>
              {schedule.graphConfigured ? 'on' : 'optional — not set yet'}
            </dd>
            <dt>Playwright</dt>
            <dd>
              {schedule.fallbackEnabled
                ? `on (max ${schedule.fallbackMaxPerRun}/run)`
                : 'off'}
            </dd>
          </dl>
        </div>

        <div className="schedule-card">
          <h3>Discover</h3>
          <dl className="kv">
            <dt>Configured</dt>
            <dd>
              {schedule.discoverEveryHours > 0
                ? `every ${schedule.discoverEveryHours} h`
                : 'off'}
            </dd>
            <dt>City</dt>
            <dd>{schedule.discoverCity}</dd>
            <dt>Resolve / run</dt>
            <dd>up to {schedule.discoverResolveLimit}</dd>
            <dt>Last finished</dt>
            <dd title={lastDiscoverAt ?? undefined}>{relativeTime(lastDiscoverAt)}</dd>
            <dt>Observed gap</dt>
            <dd>
              {observedDiscoverGapHours != null
                ? `${observedDiscoverGapHours} h`
                : '—'}
            </dd>
          </dl>
        </div>

        <div className="schedule-card">
          <h3>Account refresh tiers</h3>
          <dl className="kv">
            <dt>Hot</dt>
            <dd>every {tiers.hot}h</dd>
            <dt>Warm</dt>
            <dd>every {tiers.warm}h</dd>
            <dt>Cold</dt>
            <dd>every {tiers.cold}h</dd>
            <dt>Dormant</dt>
            <dd>every {tiers.dormant}h</dd>
          </dl>
          <p className="note" style={{ marginTop: 12 }}>
            Tier cadence decides when each handle is due. The ingest cron just drains
            whatever is due that cycle.
          </p>
        </div>
      </div>
    </Panel>
  );
}

export function RunStats() {
  const [limit, setLimit] = useState(50);
  const [skip, setSkip] = useState(0);
  const [kindFilter, setKindFilter] = useState<'all' | 'ingest' | 'discover'>('all');
  const { data, loading, error, reload } = useFetch(
    (signal) =>
      api.runs(
        {
          limit,
          skip,
          kind: kindFilter === 'all' ? undefined : kindFilter,
        },
        signal,
      ),
    [limit, skip, kindFilter],
  );

  const rows = useMemo(() => toRows(data?.items ?? []), [data]);
  const tableRows = useMemo(
    () => [...(data?.items ?? [])].reverse(),
    [data],
  );

  const totals = useMemo(() => {
    return rows.reduce(
      (acc, r) => ({
        graphOk: acc.graphOk + r.graphOk,
        fallbackOk: acc.fallbackOk + r.fallbackOk,
        failed: acc.failed + r.failed,
        postsNew: acc.postsNew + r.postsNew,
      }),
      { graphOk: 0, fallbackOk: 0, failed: 0, postsNew: 0 },
    );
  }, [rows]);

  const resolved = totals.graphOk + totals.fallbackOk;
  const fallbackShare = resolved ? totals.fallbackOk / resolved : 0;

  if (error) {
    return (
      <Panel title="Runs">
        <ErrorState message={error} onRetry={reload} />
      </Panel>
    );
  }
  if (loading && !data) {
    return (
      <Panel title="Runs">
        <Loading />
      </Panel>
    );
  }

  return (
    <div className="stack">
      {data?.schedule && (
        <SchedulePanel
          schedule={data.schedule}
          lastIngestAt={data.lastIngestAt}
          lastDiscoverAt={data.lastDiscoverAt}
          observedIngestGapMinutes={data.observedIngestGapMinutes}
          observedDiscoverGapHours={data.observedDiscoverGapHours}
        />
      )}

      <Panel
        title="Run log"
        hint={
          data
            ? `${tableRows.length} of ${data.total} runs on this page`
            : undefined
        }
        action={
          <div className="filters" style={{ margin: 0 }}>
            <select
              className="btn"
              value={kindFilter}
              onChange={(e) => {
                setKindFilter(e.target.value as 'all' | 'ingest' | 'discover');
                setSkip(0);
              }}
              aria-label="Run kind"
            >
              <option value="all">All kinds</option>
              <option value="ingest">Ingest</option>
              <option value="discover">Discover</option>
            </select>
            <select
              className="btn"
              value={limit}
              onChange={(e) => {
                setLimit(Number(e.target.value));
                setSkip(0);
              }}
              aria-label="Page size"
            >
              <option value={20}>20 / page</option>
              <option value={50}>50 / page</option>
              <option value={100}>100 / page</option>
            </select>
            <button className="btn" type="button" onClick={reload} disabled={loading}>
              Refresh
            </button>
          </div>
        }
      >
        {!tableRows.length ? (
          <Empty label="No runs recorded yet. Run ingest or discover first." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Finished</th>
                  <th>Kind</th>
                  <th>Details</th>
                  <th className="table__num">Duration</th>
                  <th className="table__num">Success</th>
                </tr>
              </thead>
              <tbody>
                {tableRows.map((r) => {
                  const ingest = isIngest(r);
                  const attempted =
                    (r.graphOk ?? 0) + (r.fallbackOk ?? 0) + (r.failed ?? 0);
                  const success =
                    ingest && attempted
                      ? Math.round(((attempted - (r.failed ?? 0)) / attempted) * 100)
                      : null;
                  return (
                    <tr key={r.id}>
                      <td title={r.finishedAt}>
                        <div>{dateTime(r.finishedAt)}</div>
                        <div className="table__sub">{relativeTime(r.finishedAt)}</div>
                      </td>
                      <td>
                        <span className={`badge badge--${ingest ? 'warm' : 'cold'}`}>
                          {ingest ? 'ingest' : 'discover'}
                        </span>
                      </td>
                      <td>
                        <div>{runDetail(r)}</div>
                        {!ingest && r.handles?.length ? (
                          <div className="table__sub">
                            {r.handles
                              .slice(0, 6)
                              .map((h) => `@${h}`)
                              .join(', ')}
                            {r.handles.length > 6 ? '…' : ''}
                          </div>
                        ) : null}
                      </td>
                      <td className="table__num">{fullNumber(r.durationS)}s</td>
                      <td className="table__num">
                        {success == null ? '—' : `${success}%`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {data && (
          <Pager skip={skip} limit={limit} total={data.total} onChange={setSkip} />
        )}
      </Panel>

      {rows.length > 0 && (
        <Panel
          title="Ingest charts"
          hint={`${fullNumber(totals.postsNew)} new posts across ingest runs on this page`}
        >
          <div className="charts">
            <div>
              <h3 className="panel__hint">Accounts resolved per run, by source</h3>
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={rows} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#262c36" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{ background: '#161b22', border: '1px solid #364150' }}
                      labelStyle={{ color: '#8b949e' }}
                    />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Bar dataKey="graphOk" stackId="a" name="Graph" fill={COLORS.graph} />
                    <Bar dataKey="fallbackOk" stackId="a" name="Fallback" fill={COLORS.fallback} />
                    <Bar dataKey="failed" stackId="a" name="Failed" fill={COLORS.failed} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div>
              <h3 className="panel__hint">Posts collected per run</h3>
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={rows} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#262c36" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{ background: '#161b22', border: '1px solid #364150' }}
                      labelStyle={{ color: '#8b949e' }}
                    />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Area
                      type="monotone"
                      dataKey="postsNew"
                      name="New"
                      stroke={COLORS.neu}
                      fill={COLORS.neu}
                      fillOpacity={0.25}
                    />
                    <Area
                      type="monotone"
                      dataKey="postsChanged"
                      name="Changed"
                      stroke={COLORS.changed}
                      fill={COLORS.changed}
                      fillOpacity={0.2}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div>
              <h3 className="panel__hint">Success rate %</h3>
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={rows} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#262c36" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis domain={[0, 100]} tick={{ fontSize: 10 }} />
                    <Tooltip
                      contentStyle={{ background: '#161b22', border: '1px solid #364150' }}
                      labelStyle={{ color: '#8b949e' }}
                    />
                    <Line
                      type="monotone"
                      dataKey="successRate"
                      name="Success %"
                      stroke={COLORS.graph}
                      dot={false}
                      strokeWidth={2}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div>
              <h3 className="panel__hint">Run duration (seconds)</h3>
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={rows} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#262c36" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip
                      contentStyle={{ background: '#161b22', border: '1px solid #364150' }}
                      labelStyle={{ color: '#8b949e' }}
                    />
                    <Line
                      type="monotone"
                      dataKey="durationS"
                      name="Seconds"
                      stroke={COLORS.neu}
                      dot={false}
                      strokeWidth={2}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>

          {data?.schedule?.primarySource === 'graph' && fallbackShare > 0.2 && (
            <p className="note note--warn">
              Fallback is resolving {Math.round(fallbackShare * 100)}% of accounts. That path is
              rate-limited by Instagram and capped per run — either your list has many
              personal/private handles, or the Graph token needs attention.
            </p>
          )}
          {data?.schedule?.primarySource === 'playwright' && (
            <p className="note">
              Playwright-only mode — healthy runs show under Fallback, not Graph. Add Graph
              credentials later when ready.
            </p>
          )}
          {totals.failed > resolved * 0.15 && (
            <p className="note note--warn">
              Failure rate is above 15%. Check the Accounts tab sorted by failures — a spike here
              usually means the Graph token expired or the fallback started collecting
              interstitials.
            </p>
          )}
        </Panel>
      )}
    </div>
  );
}
