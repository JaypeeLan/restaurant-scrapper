import { useEffect, useState } from 'react';
import { api, useDebounced, useFetch } from '../api';
import { compactNumber, dateTime, sourceLabel } from '../format';
import type { Post } from '../types';
import { Badge, Empty, ErrorState, Loading, Pager, Panel } from './Common';

const LIMIT = 24;

function PostCard({ post }: { post: Post }) {
  const [imgFailed, setImgFailed] = useState(false);

  // Instagram CDN URLs are signed and expire, so a stored mediaUrl reliably
  // 403s after a few days. Show a placeholder rather than a broken image.
  const showImage = post.mediaUrl && !imgFailed;

  return (
    <article className="post">
      {showImage ? (
        <img
          className="post__media"
          src={post.mediaUrl ?? ''}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="post__media-fallback">
          {post.mediaUrl ? 'image expired' : 'no image'}
        </div>
      )}

      <div className="post__body">
        <div className="post__top">
          <span className="post__handle">@{post.handle}</span>
          <Badge kind={post.source.name}>{sourceLabel(post.source.name)}</Badge>
        </div>

        <p className="post__caption">{post.caption || <em>no caption</em>}</p>

        <div className="post__meta">
          <span>{dateTime(post.postedAt)}</span>
          <span className="post__engagement">
            <span title="likes">{compactNumber(post.likeCount)} likes</span>
            <span title="comments">{compactNumber(post.commentCount)} comments</span>
          </span>
          {post.permalink && (
            <a href={post.permalink} target="_blank" rel="noreferrer noopener">
              Instagram
            </a>
          )}
        </div>
      </div>
    </article>
  );
}

export function PostFeed() {
  const [handle, setHandle] = useState('');
  const [search, setSearch] = useState('');
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');
  const [sort, setSort] = useState<'postedAt' | 'firstSeenAt' | 'likeCount'>('postedAt');
  const [skip, setSkip] = useState(0);

  const debouncedSearch = useDebounced(search, 350);
  const debouncedHandle = useDebounced(handle, 350);

  // Any filter change invalidates the current page offset — without this you
  // can end up on page 5 of a result set that now has one page.
  useEffect(() => {
    setSkip(0);
  }, [debouncedSearch, debouncedHandle, since, until, sort]);

  const { data, loading, error, reload } = useFetch(
    (signal) =>
      api.posts(
        {
          handle: debouncedHandle || undefined,
          q: debouncedSearch || undefined,
          since: since || undefined,
          until: until || undefined,
          sort,
          limit: LIMIT,
          skip,
        },
        signal,
      ),
    [debouncedHandle, debouncedSearch, since, until, sort, skip],
  );

  return (
    <Panel
      title="Posts"
      hint={data ? `${data.total.toLocaleString()} matching` : undefined}
      action={
        <button className="btn" type="button" onClick={reload} disabled={loading}>
          Refresh
        </button>
      }
    >
      <div className="filters">
        <div className="filters__field">
          <label htmlFor="q">Caption search</label>
          <input
            id="q"
            type="search"
            placeholder="live music, happy hour, tasting…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <div className="filters__field">
          <label htmlFor="handle">Handle</label>
          <input
            id="handle"
            type="text"
            placeholder="exact handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
          />
        </div>

        <div className="filters__field">
          <label htmlFor="since">Posted after</label>
          <input id="since" type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        </div>

        <div className="filters__field">
          <label htmlFor="until">Posted before</label>
          <input id="until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        </div>

        <div className="filters__field">
          <label htmlFor="sort">Sort</label>
          <select
            id="sort"
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
          >
            <option value="postedAt">Newest posted</option>
            <option value="firstSeenAt">Recently ingested</option>
            <option value="likeCount">Most liked</option>
          </select>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={reload} />}
      {!error && loading && !data && <Loading />}
      {!error && data && data.items.length === 0 && (
        <Empty label="No posts match these filters." />
      )}

      {!error && data && data.items.length > 0 && (
        <>
          <div className="feed" style={{ opacity: loading ? 0.55 : 1 }}>
            {data.items.map((post) => (
              <PostCard key={post.id} post={post} />
            ))}
          </div>
          <Pager skip={skip} limit={LIMIT} total={data.total} onChange={setSkip} />
        </>
      )}
    </Panel>
  );
}
