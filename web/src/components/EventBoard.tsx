import { useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import { dateTime } from '../format';
import type { EventCandidate, EventProfileGroup, ExperienceDraft } from '../types';
import { Empty, ErrorState, Loading, Pager, Panel } from './Common';

const LIMIT = 20;

function scheduleSummary(exp: ExperienceDraft | undefined): string {
  const s = exp?.schedule;
  if (!s) return '';
  const parts: string[] = [s.eventType];
  if (s.recurrence?.days?.length) parts.push(s.recurrence.days.join(', '));
  if (s.date) parts.push(s.date);
  if (s.startTime) {
    parts.push(s.endTime ? `${s.startTime}–${s.endTime}` : s.startTime);
  }
  return parts.join(' · ');
}

function EventRow({ event }: { event: EventCandidate }) {
  const exp = event.experience;
  const cats = exp?.categories ?? [];
  const prices = exp?.pricePoints ?? [];

  return (
    <article className="event">
      <div className="event__main">
        <h3 className="event__title">{exp?.name || event.title}</h3>

        <div className="event__hints">
          {cats.map((c) => (
            <span key={c} className="event__chip">
              {c}
            </span>
          ))}
          {exp?.schedule?.eventType && (
            <span className="event__chip">{exp.schedule.eventType}</span>
          )}
          {exp?.schedule?.recurrence?.days?.map((d) => (
            <span key={d} className="event__chip">
              {d}
            </span>
          ))}
          {exp?.schedule?.startTime && (
            <span className="event__chip">
              {exp.schedule.startTime}
              {exp.schedule.endTime ? `–${exp.schedule.endTime}` : ''}
            </span>
          )}
          {prices.map((p) => (
            <span key={`${p.type}-${p.price}`} className="event__chip event__chip--price">
              {typeof p.price === 'number' ? `₦${p.price.toLocaleString()}` : p.price}
              {p.type && p.type !== 'Admission' ? ` · ${p.type}` : ''}
            </span>
          ))}
          {exp?.dressCode ? (
            <span className="event__chip">{exp.dressCode}</span>
          ) : null}
        </div>

        <p className="event__caption">{exp?.description || event.caption}</p>

        <div className="event__meta">
          <span>{dateTime(event.postedAt)}</span>
          <span>at @{event.handle}</span>
          {(event.postCount ?? 1) > 1 && (
            <span>{event.postCount} posts</span>
          )}
          {event.nameSource === 'card' && <span>name from card</span>}
          {event.nameSource === 'deepseek' && <span>name from DeepSeek</span>}
          {scheduleSummary(exp) && <span>{scheduleSummary(exp)}</span>}
          {event.permalink && (
            <a href={event.permalink} target="_blank" rel="noreferrer noopener">
              Instagram
            </a>
          )}
        </div>
      </div>
      {event.mediaUrl ? (
        <img
          className="event__thumb"
          src={event.mediaUrl}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
        />
      ) : null}
    </article>
  );
}

function ProfileBlock({ group }: { group: EventProfileGroup }) {
  const [open, setOpen] = useState(false);
  const count = group.experienceCount ?? group.eventCount;

  return (
    <section className="event-profile">
      <button
        type="button"
        className="event-profile__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div>
          <div className="event-profile__handle">@{group.handle}</div>
          {group.profileName && (
            <div className="event-profile__name">{group.profileName}</div>
          )}
        </div>
        <div className="event-profile__count">
          {count} experience{count === 1 ? '' : 's'}
          <span aria-hidden>{open ? ' −' : ' +'}</span>
        </div>
      </button>
      {open && (
        <div className="event-profile__list">
          {group.events.map((event) => (
            <EventRow key={event.id} event={event} />
          ))}
        </div>
      )}
    </section>
  );
}

export function EventBoard() {
  const [handle, setHandle] = useState('');
  const [skip, setSkip] = useState(0);
  const debouncedHandle = useDebounced(handle, 350);

  const { data, loading, error, reload } = useFetch(
    (signal) =>
      api.events(
        {
          handle: debouncedHandle || undefined,
          grouped: true,
          limit: LIMIT,
          skip,
          llm: false,
        },
        signal,
      ),
    [debouncedHandle, skip],
    { key: 'events', ttlMs: 60_000 },
  );

  const profiles = data?.profiles ?? [];
  const experienceTotal =
    data?.experienceTotal ??
    profiles.reduce((n, g) => n + (g.experienceCount ?? g.eventCount ?? 0), 0);

  return (
    <Panel
      title="Experiences"
      hint={
        data
          ? `${experienceTotal} experiences · ${data.total} profiles`
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
          <label htmlFor="evt-handle">Profile</label>
          <input
            id="evt-handle"
            type="text"
            placeholder="handle"
            value={handle}
            onChange={(e) => {
              setHandle(e.target.value);
              setSkip(0);
            }}
          />
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={reload} />}
      {!error && loading && !data && <Loading />}
      {!error && data && profiles.length === 0 && (
        <Empty label="No experience-like posts found for these profiles yet." />
      )}

      {!error && profiles.length > 0 && (
        <div className="event-board" style={{ opacity: loading ? 0.55 : 1 }}>
          {profiles.map((group) => (
            <ProfileBlock key={group.handle} group={group} />
          ))}
        </div>
      )}

      {data && (
        <Pager skip={skip} limit={LIMIT} total={data.total} onChange={setSkip} />
      )}
    </Panel>
  );
}
