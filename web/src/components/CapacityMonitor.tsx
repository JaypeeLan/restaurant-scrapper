import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { api, useFetch } from '../api';
import { fullNumber } from '../format';
import type { Tier } from '../types';
import { ErrorState, Loading, Panel } from './Common';

const TIER_COLORS: Record<Tier, string> = {
  hot: '#3fb950',
  warm: '#58a6ff',
  cold: '#d29922',
  dormant: '#6e7681',
};

const TIER_ORDER: Tier[] = ['hot', 'warm', 'cold', 'dormant'];

export function CapacityMonitor() {
  const { data, loading, error, reload } = useFetch(
    (signal) => api.capacity(signal),
    [],
    { key: 'capacity', ttlMs: 120_000 },
  );

  if (error) {
    return (
      <Panel title="Capacity">
        <ErrorState message={error} onRetry={reload} />
      </Panel>
    );
  }
  if (loading && !data) {
    return (
      <Panel title="Capacity">
        <Loading />
      </Panel>
    );
  }
  if (!data) return null;

  const pct = Math.round((data.utilization ?? 0) * 100);
  const level = pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : 'ok';
  const playwrightOnly = data.primarySource === 'playwright';

  const pieData = TIER_ORDER.map((tier) => ({
    name: tier,
    value: data.tierCounts[tier] ?? 0,
  })).filter((d) => d.value > 0);

  const totalAccounts = TIER_ORDER.reduce((sum, t) => sum + (data.tierCounts[t] ?? 0), 0);

  return (
    <div className="stack">
      <div className="capacity">
        <Panel
          title="Daily fetch budget"
          hint={
            playwrightOnly
              ? `Playwright pacing — ~${data.callsPerHour}/hour from gap + per-run caps`
              : `Against the free Graph API ceiling of ${data.callsPerHour}/hour`
          }
        >
          <div className="gauge">
            <div className="gauge__head">
              <span className="gauge__pct">{pct}%</span>
              <span className={data.withinBudget ? '' : 'table__danger'}>
                {data.withinBudget ? 'within budget' : 'OVER BUDGET'}
              </span>
            </div>
            <div className="gauge__track">
              <div
                className={`gauge__fill gauge__fill--${level}`}
                style={{ width: `${Math.min(100, pct)}%` }}
              />
            </div>
            <div className="gauge__foot">
              <span>{fullNumber(data.dailyDemand)} fetches/day projected</span>
              <span>{fullNumber(data.dailyCapacity)} available</span>
            </div>
          </div>

          <dl className="kv" style={{ marginTop: 24 }}>
            <dt>Accounts tracked</dt>
            <dd>{fullNumber(totalAccounts)}</dd>
            <dt>Primary source</dt>
            <dd>{playwrightOnly ? 'Playwright' : data.graphConfigured ? 'Graph API' : 'none'}</dd>
            <dt>Graph API</dt>
            <dd>{data.graphConfigured ? 'configured' : 'optional — not set yet'}</dd>
            <dt>Playwright</dt>
            <dd>
              {data.fallbackEnabled
                ? data.playwright
                  ? `enabled (max ${data.playwright.maxPerRun}/run, ~${data.playwright.avgGapS}s gap)`
                  : 'enabled'
                : 'disabled'}
            </dd>
          </dl>

       
          {!data.fallbackEnabled && !data.graphConfigured && (
            <p className="note note--warn">
              No ingest source is enabled. Turn on Playwright or set Graph credentials.
            </p>
          )}
          {!data.withinBudget && (
            <p className="note note--warn">
              {playwrightOnly
                ? 'Projected demand exceeds Playwright pacing. Raise tier intervals, lower account count, or add Graph later for more headroom.'
                : 'Projected demand exceeds the Graph ceiling. Raise the tier intervals in settings, or split the list across a second Meta app.'}
            </p>
          )}
        </Panel>

        <Panel title="Tier distribution" hint="Derived from each account's most recent post">
          <div className="chart">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={pieData}
                  dataKey="value"
                  nameKey="name"
                  innerRadius={55}
                  outerRadius={90}
                  paddingAngle={2}
                >
                  {pieData.map((entry) => (
                    <Cell key={entry.name} fill={TIER_COLORS[entry.name as Tier]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{ background: '#161b22', border: '1px solid #364150' }}
                  labelStyle={{ color: '#8b949e' }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      </div>

      <Panel
        title="Where the fetches go"
        hint="Demand per tier per day — cadence × account count"
      >
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Tier</th>
                <th className="table__num">Accounts</th>
                <th className="table__num">Every</th>
                <th className="table__num">Fetches/day</th>
                <th className="table__num">Share</th>
              </tr>
            </thead>
            <tbody>
              {TIER_ORDER.map((tier) => {
                const count = data.tierCounts[tier] ?? 0;
                const perDay = data.breakdown[tier] ?? 0;
                const share = data.dailyDemand ? (perDay / data.dailyDemand) * 100 : 0;
                return (
                  <tr key={tier}>
                    <td>
                      <span className={`badge badge--${tier}`}>{tier}</span>
                    </td>
                    <td className="table__num">{fullNumber(count)}</td>
                    <td className="table__num">{data.tierIntervalsHours[tier]}h</td>
                    <td className="table__num">{perDay.toFixed(1)}</td>
                    <td className="table__num">{share.toFixed(0)}%</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="note">
          Dormant and cold accounts are most of the list but almost none of the cost. That
          asymmetry is what keeps a large handle list inside a free rate limit.
        </p>
      </Panel>
    </div>
  );
}
