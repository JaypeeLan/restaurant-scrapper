import { useEffect, useState } from 'react';
import { api, useFetch } from '../api';
import { countdown, dateTime, fullNumber, relativeTime } from '../format';
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

/** Live countdown from an absolute ISO target (ticks every second). */
function useCountdown(targetIso: string | null | undefined): number | null {
  const [seconds, setSeconds] = useState<number | null>(() => {
    if (!targetIso) return null;
    const t = new Date(targetIso).getTime();
    if (Number.isNaN(t)) return null;
    return Math.max(0, Math.floor((t - Date.now()) / 1000));
  });

  useEffect(() => {
    if (!targetIso) {
      setSeconds(null);
      return;
    }
    const tick = () => {
      const t = new Date(targetIso).getTime();
      if (Number.isNaN(t)) {
        setSeconds(null);
        return;
      }
      setSeconds(Math.max(0, Math.floor((t - Date.now()) / 1000)));
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [targetIso]);

  return seconds;
}

export function SummaryBar() {
  const { data, error, reload } = useFetch((signal) => api.summary(signal), []);
  const ingestLeft = useCountdown(data?.nextIngestAt);
  const discoverLeft = useCountdown(data?.nextDiscoverAt);

  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (!data) return null;

  return (
    <div className="stats">
      <Stat label="Experiences" value={fullNumber(data.events)} sub="deduped drafts" />
      <Stat
        label="Menus"
        value={fullNumber(data.menus ?? 0)}
        sub={
          data.menuItems
            ? `${fullNumber(data.menuItems)} items · ${fullNumber(data.highlights)} trays`
            : `${fullNumber(data.highlights)} highlights`
        }
      />
      <Stat label="Posts" value={fullNumber(data.posts)} sub={`${fullNumber(data.postsLast24h)} in 24h`} />
      <Stat
        label="Accounts"
        value={fullNumber(data.accounts)}
        sub={`${fullNumber(data.accountsDue)} due`}
      />
      <Stat
        label="Next ingest"
        value={countdown(ingestLeft)}
        sub={data.nextIngestAt ? dateTime(data.nextIngestAt) : data.ingestCron ?? undefined}
      />
      <Stat
        label="Next discover"
        value={countdown(discoverLeft)}
        sub={
          data.nextDiscoverAt
            ? dateTime(data.nextDiscoverAt)
            : data.discoverCron === null
              ? 'off'
              : undefined
        }
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
