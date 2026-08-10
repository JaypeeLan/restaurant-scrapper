/**
 * Wire types — mirror what serve.py emits, not what Mongo stores.
 *
 * Datetimes arrive as ISO strings. Anything the ingest may legitimately not
 * know (a caption-only embed row has no timestamp or engagement) is typed
 * `| null` rather than optional, so the UI is forced to handle it.
 */

export type Tier = 'hot' | 'warm' | 'cold' | 'dormant';
export type SourceName = 'graph' | 'web_json' | 'embed';
export type MediaType = 'IMAGE' | 'VIDEO' | 'CAROUSEL_ALBUM' | null;

export interface Paged<T> {
  items: T[];
  total: number;
  limit: number;
  skip: number;
}

export interface Post {
  id: string;
  handle: string;
  igUserId: string | null;
  postId: string | null;
  shortcode: string | null;
  permalink: string | null;
  caption: string;
  mediaType: MediaType;
  mediaUrl: string | null;
  likeCount: number | null;
  commentCount: number | null;
  postedAt: string | null;
  firstSeenAt?: string;
  updatedAt?: string;
  contentHash: string;
  source: { name: SourceName };
}

export interface AccountProfile {
  handle: string;
  igUserId: string | null;
  name: string | null;
  biography: string | null;
  website: string | null;
  followers: number | null;
  mediaCount: number | null;
  profilePicUrl: string | null;
  isBusiness?: boolean;
  isPrivate?: boolean;
  sourceName: SourceName;
}

export interface Account {
  id: string;
  handle: string;
  tier: Tier;
  igUserId?: string | null;
  profile?: AccountProfile;
  newestPostId: string | null;
  newestPostedAt: string | null;
  lastFetchedAt?: string | null;
  lastSuccessAt?: string | null;
  nextFetchAt: string | null;
  consecutiveFailures: number;
  totalPostsSeen?: number;
  lastError?: string;
  lastSource?: SourceName;
  backfilled?: boolean;
}

export interface AccountRunResult {
  handle: string;
  tier?: string | null;
  status: string;
  source?: string | null;
  postsNew?: number;
  postsChanged?: number;
  error?: string | null;
}

export interface Run {
  id: string;
  kind?: 'ingest' | 'discover';
  accounts?: number;
  graphOk?: number;
  graphMissed?: number;
  fallbackOk?: number;
  failed?: number;
  postsNew?: number;
  postsChanged?: number;
  postsUnchanged?: number;
  accountResults?: AccountRunResult[];
  city?: string;
  placesFound?: number;
  placesUpserted?: number;
  resolveAttempted?: number;
  resolved?: number;
  skipped?: number;
  seeded?: number;
  handles?: string[];
  startedAt: string;
  finishedAt: string;
  durationS: number;
}

export interface RunSchedule {
  ingestEveryMinutes: number;
  ingestLimit: number;
  discoverEveryHours: number;
  discoverCity: string;
  discoverPlaceLimit: number;
  discoverResolveLimit: number;
  tierIntervalsHours: Record<Tier, number>;
  fallbackMaxPerRun: number;
  fallbackEnabled: boolean;
  graphConfigured: boolean;
  primarySource: 'playwright' | 'graph';
}

export interface RunsResponse extends Paged<Run> {
  schedule: RunSchedule;
  lastIngestAt: string | null;
  lastDiscoverAt: string | null;
  observedIngestGapMinutes: number | null;
  observedDiscoverGapHours: number | null;
  now?: string;
  nextIngestAt?: string | null;
  nextIngestInSeconds?: number | null;
  nextDiscoverAt?: string | null;
  nextDiscoverInSeconds?: number | null;
  ingestCron?: string | null;
  discoverCron?: string | null;
}

export interface Summary {
  accounts: number;
  accountsDue: number;
  accountsFailing: number;
  posts: number;
  postsLast24h: number;
  postsLast7d: number;
  highlights: number;
  events: number;
  generatedAt: string;
  now?: string;
  nextIngestAt?: string | null;
  nextIngestInSeconds?: number | null;
  nextDiscoverAt?: string | null;
  nextDiscoverInSeconds?: number | null;
  ingestCron?: string | null;
  discoverCron?: string | null;
}

export interface ExperienceDraft {
  _id: string;
  active: boolean;
  name: string;
  slug: string;
  description: string;
  tags: string[];
  ageLimit: string;
  categories: string[];
  dressCode: string;
  sourceType: string;
  owner: null;
  ownerName: string;
  host?: string | null;
  emails?: string[];
  phones?: string[];
  website?: string | null;
  coverImage?: string | null;
  imageUrl?: string | null;
  pricePoints: { type: string; description?: string | null; price: number }[];
  location: null;
  appearances: string[];
  schedule: {
    eventType: 'one-time' | 'recurring';
    date?: string | null;
    startDate: string;
    endDate: string;
    recurrence?: { days: string[]; startDate: string; endDate: string };
    startTime: string;
    endTime: string;
  };
  rating?: number | null;
}

export interface EventCandidate {
  id: string;
  postId: string;
  handle: string;
  title: string;
  caption: string;
  signals: string[];
  score: number;
  whenHints: string[];
  priceHints: string[];
  permalink: string | null;
  mediaUrl: string | null;
  mediaType: MediaType;
  postedAt: string | null;
  shortcode: string | null;
  filled?: string[];
  missing?: string[];
  nameSource?: 'card' | 'caption' | 'deepseek';
  postCount?: number;
  sourcePosts?: {
    postId?: string | null;
    shortcode?: string | null;
    permalink?: string | null;
    postedAt?: string | null;
    mediaUrl?: string | null;
  }[];
  experience?: ExperienceDraft;
}

export interface EventProfileGroup {
  handle: string;
  profileName?: string | null;
  eventCount: number;
  experienceCount?: number;
  events: EventCandidate[];
}

export interface EventsResponse {
  grouped: boolean;
  total: number;
  limit: number;
  skip: number;
  llm?: {
    enabled: boolean;
    model: string | null;
    refined: number;
  };
  profiles?: EventProfileGroup[];
  items?: EventCandidate[];
}

export interface Capacity {
  dailyDemand: number;
  dailyCapacity: number;
  utilization: number | null;
  withinBudget: boolean;
  breakdown: Record<string, number>;
  tierCounts: Record<Tier, number>;
  callsPerHour: number;
  tierIntervalsHours: Record<Tier, number>;
  graphConfigured: boolean;
  fallbackEnabled: boolean;
  primarySource: 'playwright' | 'graph' | 'none';
  playwright?: {
    dailyCapacity: number;
    callsPerHour: number;
    runsPerDay: number;
    maxPerRun: number;
    avgGapS: number;
  };
}

export interface PostQuery {
  handle?: string;
  q?: string;
  since?: string;
  until?: string;
  media_type?: string;
  source?: string;
  limit?: number;
  skip?: number;
  sort?: 'postedAt' | 'firstSeenAt' | 'likeCount';
}

export interface AccountQuery {
  tier?: string;
  failing?: boolean;
  q?: string;
  limit?: number;
  skip?: number;
  sort?: 'nextFetchAt' | 'lastFetchedAt' | 'handle' | 'consecutiveFailures' | 'newestPostedAt';
}
