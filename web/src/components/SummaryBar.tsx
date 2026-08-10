import { api, useFetch } from '../api';
import { fullNumber, relativeTime } from '../format';
import { ErrorState } from './Common';

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: 'warn' | 'danger';
}) {
  return (
    <div className="stats__card">
      <div className="stats__label">{label}</div>
      <div className={`stats__value${tone ? ` stats__value--${tone}` : ''}`}>{value}</div>
      {sub && <div className="stats__sub">{sub}</div>}
    </div>
  );
}

export function SummaryBar() {
  const { data, error, reload } = useFetch((signal) => api.summary(signal), []);

  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (!data) return null;

  return (
    <div className="stats">
      <Stat label="Experiences" value={fullNumber(data.events)} sub="from captions" />
      <Stat label="Posts" value={fullNumber(data.posts)} sub={`${fullNumber(data.postsLast24h)} in 24h`} />
      <Stat
        label="Accounts"
        value={fullNumber(data.accounts)}
        sub={`${fullNumber(data.accountsDue)} due`}
      />
      <Stat
        label="Failing"
        value={fullNumber(data.accountsFailing)}
        tone={data.accountsFailing > 0 ? (data.accountsFailing > 20 ? 'danger' : 'warn') : undefined}
      />
      <Stat label="Updated" value={relativeTime(data.generatedAt)} />
    </div>
  );
}
