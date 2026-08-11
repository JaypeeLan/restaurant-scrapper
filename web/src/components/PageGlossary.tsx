import type { ReactNode } from 'react';

export type GlossaryTerm = {
  term: string;
  meaning: string;
};

export function PageGlossary({
  title = 'On this page',
  terms,
}: {
  title?: string;
  terms: GlossaryTerm[];
}) {
  if (!terms.length) return null;

  return (
    <aside className="page-glossary" aria-label={title}>
      <h3 className="page-glossary__title">{title}</h3>
      <dl className="page-glossary__list">
        {terms.map((item) => (
          <div key={item.term} className="page-glossary__row">
            <dt>{item.term}</dt>
            <dd>{item.meaning}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}

/** Shared ops terms that appear in the summary strip above every tab. */
export const SUMMARY_TERMS: GlossaryTerm[] = [
  {
    term: 'Ingest',
    meaning: 'A fetch cycle that pulls recent Instagram posts for due accounts into Mongo.',
  },
  {
    term: 'Discover',
    meaning:
      'Finds local venues (via maps), resolves Instagram handles with a logged-in session, and seeds the account list.',
  },
];

export const TAB_GLOSSARIES: Record<string, GlossaryTerm[]> = {
  events: [
    ...SUMMARY_TERMS,
    {
      term: 'Experience',
      meaning:
        'A draft offering people can book or attend (brunch, show, tasting) extracted from a post caption or flyer.',
    },
    {
      term: 'Gate / score',
      meaning:
        'Heuristic check that a post looks like a real announcement (when + offer/ticket). Low-signal vibe posts are dropped.',
    },
    {
      term: 'one-time vs recurring',
      meaning:
        'Schedule shape: a dated show vs something that repeats on named weekdays (e.g. every Sunday).',
    },
    {
      term: 'name from card / DeepSeek',
      meaning:
        'Where the title came from — OCR of the flyer image, or an optional LLM refine. Default dashboard view uses captions + cached OCR only.',
    },
  ],
  posts: [
    ...SUMMARY_TERMS,
    {
      term: 'Post',
      meaning: 'Raw Instagram media stored as-is (caption, media URL, engagement, permalink).',
    },
    {
      term: 'Source',
      meaning:
        'How the post was fetched: Graph API (official), Playwright web scrape, or embed — shown as a small badge.',
    },
    {
      term: 'Changed vs new',
      meaning:
        'New means first time we saw this shortcode; changed means caption/engagement updated on a later fetch.',
    },
  ],
  accounts: [
    ...SUMMARY_TERMS,
    {
      term: 'Tier (hot / warm / cold / dormant)',
      meaning:
        'Poll frequency band based on how recently the venue posts. Hot accounts are checked most often.',
    },
    {
      term: 'Due',
      meaning: 'Accounts whose nextFetchAt is in the past — they are candidates for the next ingest run.',
    },
    {
      term: 'Failing',
      meaning:
        'Repeated fetch errors (blocked session, missing handle, Graph undiscoverable). Needs attention or cookies refresh.',
    },
    {
      term: 'nextFetchAt',
      meaning: 'When the scheduler plans to pull this account again (tier interval + jitter).',
    },
  ],
  runs: [
    ...SUMMARY_TERMS,
    {
      term: 'Run',
      meaning: 'One completed ingest or discover job with counts, duration, and optional per-account outcomes.',
    },
    {
      term: 'Playwright / fallback',
      meaning:
        'Browser-based fetch used when Graph is off or a handle is not a discoverable business account.',
    },
    {
      term: 'Graph miss → Playwright',
      meaning: 'Graph could not resolve the handle, so that account was handed to the browser path.',
    },
    {
      term: 'Observed gap',
      meaning: 'Actual time between the last two finished runs of that kind — useful to verify cron health.',
    },
    {
      term: 'over cap / blocked',
      meaning:
        'Skipped because this run hit the max-accounts limit, or Instagram challenged / blocked the browser session.',
    },
  ],
  capacity: [
    ...SUMMARY_TERMS,
    {
      term: 'Daily fetch budget',
      meaning:
        'Rough ceiling of how many account fetches you can afford per day from pacing (gaps × cron) or Graph rate limits.',
    },
    {
      term: 'Projected demand',
      meaning: 'Accounts × their tier cadence — how many fetches the current list would want each day.',
    },
    {
      term: 'Utilization',
      meaning: 'Demand ÷ capacity. Over ~90% means you are likely to queue or miss due accounts.',
    },
    {
      term: 'Within budget',
      meaning: 'Projected demand fits under the daily ceiling with the current settings.',
    },
  ],
};

export function TabGlossary({ tab }: { tab: string }): ReactNode {
  return <PageGlossary terms={TAB_GLOSSARIES[tab] ?? SUMMARY_TERMS} />;
}
