"""
API smoke test against an in-memory Mongo (mongomock) — no real database.

Verifies the things a typed frontend actually depends on: route wiring, filter
construction, and that every field the TS types declare is present with the
right shape. A response that silently drops `source` or ships a raw datetime
breaks the dashboard at runtime, and nothing else would catch it.

    pip install mongomock fastapi httpx
    python -m tests.test_api
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label} {detail}")
        FAILURES.append(label)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed(db) -> None:
    from config import settings

    db[settings.COL_ACCOUNTS].insert_many(
        [
            {
                "_id": "a1",
                "handle": "gjelina",
                "tier": "hot",
                "newestPostId": "p1",
                "newestPostedAt": _now() - timedelta(days=1),
                "lastFetchedAt": _now() - timedelta(hours=2),
                "nextFetchAt": _now() - timedelta(minutes=5),  # due
                "consecutiveFailures": 0,
                "lastSource": "graph",
                "profile": {"handle": "gjelina", "followers": 91000, "sourceName": "graph"},
            },
            {
                "_id": "a2",
                "handle": "closed_spot",
                "tier": "dormant",
                "newestPostId": None,
                "newestPostedAt": None,
                "nextFetchAt": _now() + timedelta(days=3),
                "consecutiveFailures": 5,
                "lastError": "not discoverable via Graph",
            },
            {
                "_id": "a3",
                "handle": "roberta",
                "tier": "warm",
                "nextFetchAt": _now() + timedelta(hours=6),
                "consecutiveFailures": 0,
                "lastSource": "web_json",
            },
        ]
    )

    db[settings.COL_POSTS].insert_many(
        [
            {
                "_id": "gjelina:ABC123",
                "handle": "gjelina",
                "shortcode": "ABC123",
                "postId": "p1",
                "permalink": "https://www.instagram.com/p/ABC123/",
                "caption": "Live jazz this Friday 8pm — $15 (at the door)",
                "mediaType": "IMAGE",
                "mediaUrl": "https://cdn/x.jpg",
                "likeCount": 214,
                "commentCount": 11,
                "postedAt": _now() - timedelta(days=1),
                "firstSeenAt": _now() - timedelta(hours=20),
                "contentHash": "h1",
                # Deliberately large: the API must strip this.
                "source": {"name": "graph", "raw": {"blob": "x" * 5000}},
            },
            {
                "_id": "roberta:DEF456",
                "handle": "roberta",
                "shortcode": "DEF456",
                "caption": "Taco Tuesday all night",
                "mediaType": "VIDEO",
                "likeCount": 88,
                "commentCount": 3,
                "postedAt": _now() - timedelta(days=10),
                "firstSeenAt": _now() - timedelta(days=9),
                "contentHash": "h2",
                "source": {"name": "web_json", "raw": {"blob": "y" * 5000}},
            },
        ]
    )

    db[settings.COL_RUNS].insert_many(
        [
            {
                "_id": "r1",
                "accounts": 100,
                "graphOk": 90,
                "graphMissed": 5,
                "fallbackOk": 4,
                "failed": 6,
                "postsNew": 12,
                "postsChanged": 3,
                "postsUnchanged": 400,
                "startedAt": _now() - timedelta(hours=3),
                "finishedAt": _now() - timedelta(hours=3) + timedelta(minutes=4),
                "durationS": 240.0,
            },
            {"_id": "budget:2026-08-09", "creditsSpent": 12},  # must be excluded
        ]
    )


def main() -> int:
    import mongomock

    from config import settings
    from pipeline import store

    print("=" * 62)
    print("API smoke test (mongomock)")
    print("=" * 62)

    client = mongomock.MongoClient()
    db = client[settings.MONGODB_DB_NAME]
    seed(db)

    # Point both the store and the already-imported serve module at the fake.
    store.get_db = lambda *_a, **_k: db  # type: ignore[assignment]

    import serve

    serve.store.get_db = lambda *_a, **_k: db  # type: ignore[assignment]

    from fastapi.testclient import TestClient

    api = TestClient(serve.app)

    print("\nhealth + summary")
    r = api.get("/api/health")
    check("health 200", r.status_code == 200, str(r.status_code))

    r = api.get("/api/summary")
    body = r.json()
    check("summary 200", r.status_code == 200)
    check("counts accounts", body["accounts"] == 3, str(body.get("accounts")))
    check("counts due accounts", body["accountsDue"] == 1, str(body.get("accountsDue")))
    check("counts failing accounts", body["accountsFailing"] == 1, str(body.get("accountsFailing")))
    check("counts posts", body["posts"] == 2, str(body.get("posts")))
    check("generatedAt is an ISO string", isinstance(body["generatedAt"], str))

    print("\nposts")
    r = api.get("/api/posts")
    body = r.json()
    check("posts 200", r.status_code == 200)
    check("returns both posts", body["total"] == 2, str(body["total"]))
    first = body["items"][0]
    check("newest first", first["handle"] == "gjelina", first["handle"])
    check("_id renamed to id", "id" in first and "_id" not in first)
    check("postedAt serialized to string", isinstance(first["postedAt"], str))
    check("source.name kept", first["source"]["name"] == "graph")
    check(
        "source.raw stripped from list view",
        "raw" not in first["source"],
        str(first["source"].keys()),
    )
    check(
        "payload stays small",
        len(r.content) < 4000,
        f"{len(r.content)} bytes",
    )

    print("\npost filters")
    r = api.get("/api/posts", params={"handle": "roberta"})
    check("filter by handle", r.json()["total"] == 1, str(r.json()["total"]))

    r = api.get("/api/posts", params={"q": "jazz"})
    check("caption search matches", r.json()["total"] == 1)

    # Regex-special characters in the query must not blow up or over-match.
    r = api.get("/api/posts", params={"q": "$15 (at the door)"})
    check("regex metacharacters escaped", r.status_code == 200 and r.json()["total"] == 1,
          f"{r.status_code} {r.json().get('total')}")

    r = api.get("/api/posts", params={"q": "nothing here"})
    check("no match returns empty", r.json()["total"] == 0)

    r = api.get("/api/posts", params={"source": "web_json"})
    check("filter by source", r.json()["total"] == 1)

    r = api.get("/api/posts", params={"media_type": "video"})
    check("media type is case-insensitive", r.json()["total"] == 1)

    since = (_now() - timedelta(days=3)).date().isoformat()
    r = api.get("/api/posts", params={"since": since})
    check("date lower bound applied", r.json()["total"] == 1, str(r.json()["total"]))

    r = api.get("/api/posts", params={"since": "not-a-date"})
    check("bad date returns 400", r.status_code == 400, str(r.status_code))

    r = api.get("/api/posts", params={"limit": 1, "skip": 1})
    check("pagination works", len(r.json()["items"]) == 1 and r.json()["total"] == 2)

    r = api.get("/api/posts", params={"sort": "bogus"})
    check("invalid sort rejected", r.status_code == 422, str(r.status_code))

    print("\nsingle post")
    r = api.get("/api/posts/gjelina:ABC123")
    check("colon in id routes correctly", r.status_code == 200, str(r.status_code))
    check("detail view includes raw", "raw" in r.json()["source"])

    r = api.get("/api/posts/does:not:exist")
    check("missing post 404s", r.status_code == 404, str(r.status_code))

    print("\naccounts")
    r = api.get("/api/accounts")
    body = r.json()
    check("accounts 200", r.status_code == 200)
    check("returns all", body["total"] == 3, str(body["total"]))
    check("sorted by nextFetchAt ascending", body["items"][0]["handle"] == "gjelina",
          body["items"][0]["handle"])

    r = api.get("/api/accounts", params={"failing": "true"})
    check("failing filter", r.json()["total"] == 1, str(r.json()["total"]))

    r = api.get("/api/accounts", params={"tier": "hot"})
    check("tier filter", r.json()["total"] == 1)

    r = api.get("/api/accounts", params={"q": "rober"})
    check("handle substring search", r.json()["total"] == 1)

    acct = api.get("/api/accounts", params={"tier": "dormant"}).json()["items"][0]
    check("null newestPostedAt survives", acct["newestPostedAt"] is None)

    print("\nruns")
    r = api.get("/api/runs")
    items = r.json()["items"]
    check("runs 200", r.status_code == 200)
    check("budget doc excluded from runs", len(items) == 1, str(len(items)))
    check("run has chart fields", {"graphOk", "fallbackOk", "failed"} <= set(items[0]))
    check("finishedAt serialized", isinstance(items[0]["finishedAt"], str))
    body = r.json()
    check("schedule meta present", "ingestEveryMinutes" in body.get("schedule", {}), str(body.get("schedule")))
    check("lastIngestAt present", body.get("lastIngestAt") is not None)

    print("\ncapacity")
    r = api.get("/api/capacity")
    body = r.json()
    check("capacity 200", r.status_code == 200)
    check("tier counts present", set(body["tierCounts"]) == {"hot", "warm", "cold", "dormant"})
    check("hot count correct", body["tierCounts"]["hot"] == 1)
    check("within budget for 3 accounts", body["withinBudget"] is True)
    check("exposes rate ceiling", body["callsPerHour"] > 0)
    check("primarySource playwright when graph empty", body.get("primarySource") == "playwright", str(body.get("primarySource")))

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
