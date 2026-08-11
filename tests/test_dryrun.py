"""
Offline verification — no network, no Mongo, no credentials.

Covers the logic that is expensive to get wrong: normalization of both source
shapes, change detection via contentHash, tier classification boundaries, the
rate limiter's window accounting, and the capacity projection.

    python -m tests.test_dryrun
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone

from ig.graph_client import RateLimiter
from ig.http import CircuitBreaker
from pipeline import normalizer as norm
from pipeline import tiers

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label} {detail}")
        FAILURES.append(label)


# ── fixtures mirroring the real API shapes ────────────────────────────────────

GRAPH_ITEM = {
    "id": "17895695668004550",
    "caption": "Live jazz this Friday 8pm — $15 at the door 🎷",
    "media_type": "IMAGE",
    "media_url": "https://scontent.cdninstagram.com/v/abc.jpg",
    "permalink": "https://www.instagram.com/p/DEiyb48AeB9/",
    "timestamp": "2026-08-05T18:23:11+0000",
    "like_count": 214,
    "comments_count": 11,
}

GRAPH_PROFILE = {
    "id": "17841400000000000",
    "username": "test_bistro",
    "name": "Test Bistro",
    "biography": "Neighbourhood kitchen",
    "website": "https://testbistro.com",
    "followers_count": 8421,
    "media_count": 613,
    "profile_picture_url": "https://scontent.cdninstagram.com/pic.jpg",
}

WEB_NODE = {
    "__typename": "GraphVideo",
    "id": "3540614075954356349",
    "shortcode": "DEiyb48AeB9",
    "display_url": "https://scontent.cdninstagram.com/v/xyz.jpg",
    "is_video": True,
    "taken_at_timestamp": 1754418191,
    "edge_media_to_caption": {
        "edges": [{"node": {"text": "Taco Tuesday all night, 5-10pm"}}]
    },
    "edge_media_to_comment": {"count": 12},
    "edge_liked_by": {"count": 126},
    "owner": {"id": "2700692569", "username": "test_bistro"},
}

# 2026 logged-out Polaris shape — profile + timeline split across Relay blobs.
POLARIS_USER = {
    "pk": "13496131",
    "username": "test_bistro",
    "full_name": "Test Bistro",
    "biography": "Neighbourhood kitchen",
    "follower_count": 8421,
    "all_media_count": 613,
    "profile_pic_url": "https://scontent.cdninstagram.com/pic.jpg",
    "is_private": False,
    "bio_links": [{"url": "https://testbistro.com"}],
    "id": "17841400000000000",
    "polaris_ordered_timeline_connection": {
        "edges": [
            {
                "node": {
                    "__typename": "XIGPolarisVideoMedia",
                    "pk": "3461506908121543778",
                    "code": "DEiyb48AeB9",
                    "caption": {"text": "Taco Tuesday all night, 5-10pm"},
                    "display_uri": "https://scontent.cdninstagram.com/v/polaris.jpg",
                    "media_type": 2,
                    "user": {"pk": "2700692569", "username": "test_bistro", "id": "2700692569"},
                    "id": "POLARIS_3461506908121543778",
                }
            }
        ],
        "page_info": {"has_next_page": False, "end_cursor": None},
    },
    "lox_highlights_connection": {"edges": [], "page_info": {"has_next_page": False}},
}


def test_graph_normalization() -> None:
    print("\ngraph normalization")
    doc = norm.from_graph(GRAPH_ITEM, handle="test_bistro", ig_user_id="17841400000000000")

    check("shortcode parsed from permalink", doc["shortcode"] == "DEiyb48AeB9", doc["shortcode"])
    check("_id is handle:shortcode", doc["_id"] == "test_bistro:DEiyb48AeB9", doc["_id"])
    check("caption preserved", "Live jazz" in doc["caption"])
    check(
        "Graph ISO timestamp with +0000 parsed",
        isinstance(doc["postedAt"], datetime) and doc["postedAt"].year == 2026,
        str(doc["postedAt"]),
    )
    check("postedAt is tz-aware UTC", doc["postedAt"].tzinfo is not None)
    check("raw payload retained", doc["source"]["raw"] is GRAPH_ITEM)
    check("contentHash present", len(doc["contentHash"]) == 40)


def test_web_normalization() -> None:
    print("\nweb_json normalization")
    doc = norm.from_web_json(WEB_NODE, handle="test_bistro")

    check("_id matches graph key format", doc["_id"] == "test_bistro:DEiyb48AeB9", doc["_id"])
    check("caption pulled from edge", "Taco Tuesday" in doc["caption"])
    check("video detected", doc["mediaType"] == "VIDEO", str(doc["mediaType"]))
    check("unix timestamp parsed", isinstance(doc["postedAt"], datetime))
    check("like count mapped", doc["likeCount"] == 126)

    # The critical property: both sources must produce the same _id for the
    # same post, or the fallback double-writes every post it touches.
    graph_doc = norm.from_graph(GRAPH_ITEM, handle="test_bistro")
    check("graph and web _id agree for same post", doc["_id"] == graph_doc["_id"])


def test_polaris_normalization() -> None:
    print("\npolaris web_json normalization")
    profile = norm.profile_from_web(POLARIS_USER, handle="test_bistro")
    nodes = norm.timeline_nodes(POLARIS_USER)
    doc = norm.from_web_json(nodes[0], handle="test_bistro")

    check("followers from follower_count", profile["followers"] == 8421)
    check("website from bio_links", profile["website"] == "https://testbistro.com")
    check("timeline edges from polaris connection", len(nodes) == 1)
    check("shortcode from code", doc["shortcode"] == "DEiyb48AeB9", str(doc["shortcode"]))
    check("caption from caption.text", "Taco Tuesday" in doc["caption"])
    check("mediaUrl from display_uri", "polaris.jpg" in (doc["mediaUrl"] or ""))
    check("video from media_type=2", doc["mediaType"] == "VIDEO", str(doc["mediaType"]))
    check("POLARIS_ prefix stripped from postId", doc["postId"] == "3461506908121543778")
    check(
        "polaris and graph _id agree",
        doc["_id"] == norm.from_graph(GRAPH_ITEM, handle="test_bistro")["_id"],
    )


def test_change_detection() -> None:
    print("\nchange detection")
    a = norm.from_graph(GRAPH_ITEM, handle="test_bistro")
    b = norm.from_graph(dict(GRAPH_ITEM), handle="test_bistro")
    check("identical payloads hash identically", a["contentHash"] == b["contentHash"])

    edited = dict(GRAPH_ITEM, caption="Live jazz Friday — SOLD OUT")
    c = norm.from_graph(edited, handle="test_bistro")
    check("caption edit changes hash", a["contentHash"] != c["contentHash"])

    engaged = dict(GRAPH_ITEM, like_count=999)
    d = norm.from_graph(engaged, handle="test_bistro")
    check("engagement change changes hash", a["contentHash"] != d["contentHash"])

    # media_url carries a rotating CDN signature — it must NOT affect the hash,
    # or every post looks "changed" on every fetch and dedup buys you nothing.
    resigned = dict(GRAPH_ITEM, media_url="https://scontent.cdninstagram.com/v/abc.jpg?sig=NEW")
    e = norm.from_graph(resigned, handle="test_bistro")
    check("rotating CDN url does NOT change hash", a["contentHash"] == e["contentHash"])


def test_tiers() -> None:
    print("\ntier classification")
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    cases = [
        (now - timedelta(days=1), "hot"),
        (now - timedelta(days=3), "hot"),
        (now - timedelta(days=10), "warm"),
        (now - timedelta(days=40), "cold"),
        (now - timedelta(days=200), "dormant"),
        (None, "warm"),
    ]
    for posted, expected in cases:
        got = tiers.classify(posted, now=now)
        label = posted.date().isoformat() if posted else "unknown"
        check(f"{label} → {expected}", got == expected, f"got {got}")

    check("cold promotes to warm on new posts", tiers.promote_on_new_posts("cold", 2) == "warm")
    check("no promotion without new posts", tiers.promote_on_new_posts("cold", 0) == "cold")
    check("hot cannot promote past hot", tiers.promote_on_new_posts("hot", 5) == "hot")

    nxt = tiers.next_fetch_at("hot", now=now)
    delta_h = (nxt - now).total_seconds() / 3600
    check("hot reschedules ~12h out with jitter", 9 < delta_h < 15, f"{delta_h:.1f}h")


def test_capacity() -> None:
    print("\ncapacity projection")
    plan = tiers.plan_capacity(
        {"hot": 150, "warm": 450, "cold": 300, "dormant": 100}, calls_per_hour=180
    )
    check("1,000 accounts fits the free ceiling", plan["withinBudget"], str(plan))
    check("utilization under 50%", plan["utilization"] < 0.5, str(plan["utilization"]))
    print(f"    demand {plan['dailyDemand']}/day vs capacity {plan['dailyCapacity']}/day")

    over = tiers.plan_capacity({"hot": 5000}, calls_per_hour=180)
    check("5,000 hot accounts flagged over budget", not over["withinBudget"])


def test_rate_limiter() -> None:
    print("\nrate limiter")

    async def run() -> None:
        limiter = RateLimiter(calls_per_hour=5)
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        check("5 calls within capacity are instant", time.monotonic() - start < 0.2)
        check("window accounting correct", limiter.used_in_window == 5)

    asyncio.run(run())


def test_circuit_breaker() -> None:
    print("\ncircuit breaker")
    cb = CircuitBreaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    check("holds below threshold", not cb.tripped)
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    check("success resets the counter", not cb.tripped)
    cb.record_failure()
    check("trips at threshold", cb.tripped)


def test_watermark() -> None:
    print("\nwatermark")
    from pipeline.store import newest_watermark

    docs = [
        norm.from_graph(dict(GRAPH_ITEM, id="1", timestamp="2026-08-01T10:00:00+0000"),
                        handle="h"),
        norm.from_graph(dict(GRAPH_ITEM, id="2", timestamp="2026-08-07T10:00:00+0000"),
                        handle="h"),
        norm.from_graph(dict(GRAPH_ITEM, id="3", timestamp="2026-08-03T10:00:00+0000"),
                        handle="h"),
    ]
    post_id, posted_at = newest_watermark(docs)
    check("picks the newest post", posted_at.day == 7, str(posted_at))
    check("returns an id", bool(post_id))
    check("empty batch is safe", newest_watermark([]) == (None, None))


def test_event_extract() -> None:
    print("\nexperience extract")
    from pipeline import event_extract

    hit = event_extract.extract_from_caption(
        "Unlimited Sushi & Dim Sum\n\nMonday – Friday\n12:30 PM – 3:30 PM\nPrice: ₦40,000"
    )
    miss = event_extract.extract_from_caption("Inside NOK by ALÁRA")
    check("detects recurring lunch offer", hit is not None and "offering" in (hit or {})["signals"])
    check("captures time hints", bool(hit and hit["whenHints"]))
    check("captures price", bool(hit and hit["priceHints"]))
    check(
        "range absorbs bare Monday/Friday",
        hit is not None
        and any("monday" in h.lower() and "friday" in h.lower() for h in hit["whenHints"])
        and "Monday" not in hit["whenHints"]
        and "Friday" not in hit["whenHints"],
        str((hit or {}).get("whenHints")),
    )
    check("ignores vibe-only caption", miss is None)

    vibe = event_extract.extract_from_caption(
        "The only acceptable form of martial 'culinary' art.\n"
        "Teppanyaki at Shiro is a high-octane performance.\n"
        "Join us this week — reserve via the link in bio."
    )
    check("ignores brand vibe post", vibe is None)

    soft_vibe = (
        "Wednesday is calling, and Crossroads is the place to be!\n\n"
        "Get ready for a night of great music, amazing energy, good company, "
        "and unforgettable moments. Come eat, drink, dance, and enjoy the vibes "
        "that make every Wednesday special.\n\n"
        "Gather your people and let’s make tonight one to remember."
    )
    check(
        "vibe 'Wednesday special' is not an offering",
        event_extract.extract_from_text(soft_vibe) is None,
    )
    check(
        "untitled soft vibe not drafted",
        event_extract.experience_from_post(
            {"_id": "crossroads:soft", "handle": "crossroads_texmex", "caption": soft_vibe},
            use_card_ocr=False,
            use_llm=False,
        )
        is None,
    )

    park = (
        "Fun doesn't wait for the weekend\n"
        "From Monday to Sunday, our park is open for everyone, kids and adults alike.\n"
        "Exciting rides\nSnacks & chill zones\nUnlimited fun\n"
        "#familytime #allweekfunweek #parkvibes"
    )
    check("park open-hours promo is not an experience", event_extract.extract_from_text(park) is None)

    sunday = (
        "Your Sunday should be effortless. Start by booking it this Today.\n"
        "The Details:\n📅 Every Sunday | 12:30 PM - 4:00 PM\n"
        "🥂 ₦75,000 — Food & Alcohol\n"
        "#ShiroLagos #SundayBrunchAffair #TasteOfShiro"
    )
    check(
        "sunday brunch uses hashtag not 'Your Sunday'",
        "brunch" in event_extract._experience_name(sunday).lower(),
        event_extract._experience_name(sunday),
    )

    post = {
        "_id": "shirolagos:abc",
        "handle": "shirolagos",
        "caption": (
            "Unlimited Sushi & Dim Sum\n\n"
            "Available: Monday – Friday\n"
            "Time: 12:30 PM start – 3:30 PM end\n"
            "Price: ₦40,000\n"
            "Book now #sushi #lagos"
        ),
        "shortcode": "abc",
        "permalink": "https://www.instagram.com/p/abc/",
        "mediaUrl": "https://cdn.example/x.jpg",
        "postedAt": None,
    }
    draft = event_extract.experience_from_post(
        post, profile_name="Shiro Lagos", use_card_ocr=False, use_llm=False
    )
    check("draft built", draft is not None)
    exp = (draft or {}).get("experience") or {}
    check("name set", bool(exp.get("name")))
    check("categories include Food", "Food" in exp.get("categories", []), str(exp.get("categories")))
    check("recurring schedule", exp.get("schedule", {}).get("eventType") == "recurring")
    check(
        "weekdays Mon-Fri",
        exp.get("schedule", {}).get("recurrence", {}).get("days") == [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
        ],
        str(exp.get("schedule", {}).get("recurrence")),
    )
    check("startTime 12:30", exp.get("schedule", {}).get("startTime") == "12:30")
    check("endTime 15:30", exp.get("schedule", {}).get("endTime") == "15:30")
    check("pricePoints", (exp.get("pricePoints") or [{}])[0].get("price") == 40000)
    check("tags from hashtags", "sushi" in exp.get("tags", []))
    check("ownerName from profile", exp.get("ownerName") == "Shiro Lagos")
    check("coverImage from media", exp.get("coverImage") == "https://cdn.example/x.jpg")
    check("location missing", "location" in (draft or {}).get("missing", []))
    check("owner missing", "owner" in (draft or {}).get("missing", []))

    brunch_caption = """The ultimate Sunday ritual, perfectly paired.

The Details:
📅 Every Sunday | 12:30 PM - 4:00 PM
🥂 ₦75,000 — Food & Alcohol
🥢 ₦35,000 — Kids Brunch (Ages 6–12)

#ShiroLagos #ShiroSundayBrunch #TheShiroExperience
"""
    brunch = event_extract.experience_from_post(
        {"_id": "shirolagos:DYmrCQjKQ0k", "handle": "shirolagos", "caption": brunch_caption},
        use_card_ocr=False,
        use_llm=False,
    )
    brunch_name = ((brunch or {}).get("experience") or {}).get("name", "")
    check(
        "brunch name from hashtag not price tier",
        "Kids Brunch" not in brunch_name and "Sunday" in brunch_name and "Brunch" in brunch_name,
        brunch_name,
    )

    thin = "See you this weekend ✨"
    flyer = (
        "Sunday Brunch Affairs\n"
        "Every Sunday\n"
        "12:30 PM – 4:00 PM\n"
        "₦75,000 — Food & Alcohol"
    )
    check("thin caption alone is not an experience", event_extract.extract_from_text(thin) is None)
    check(
        "caption + flyer OCR passes the gate",
        event_extract.extract_from_text(f"{thin}\n{flyer}", min_len=12) is not None,
    )
    flyer_draft = event_extract.experience_from_post(
        {
            "_id": "shirolagos:flyer1",
            "handle": "shirolagos",
            "caption": thin,
            "mediaUrl": "https://cdn.example/flyer.jpg",
        },
        use_card_ocr=True,
        use_llm=False,
        ocr_text=flyer,
    )
    check("flyer-backed draft built", flyer_draft is not None, str(flyer_draft))
    check(
        "gateSource caption+flyer",
        (flyer_draft or {}).get("gateSource") == "caption+flyer",
        str((flyer_draft or {}).get("gateSource")),
    )
    check(
        "flyer schedule startTime",
        ((flyer_draft or {}).get("experience") or {}).get("schedule", {}).get("startTime") == "12:30",
        str(((flyer_draft or {}).get("experience") or {}).get("schedule")),
    )

    posts = [
        post,
        {
            "_id": "shirolagos:def",
            "handle": "shirolagos",
            "caption": "Pretty plate of sushi",
            "shortcode": "def",
        },
    ]
    events = event_extract.extract_events(posts, use_card_ocr=False, use_llm=False)
    groups = event_extract.group_by_handle(events)
    check("one experience from two posts", len(events) == 1)
    check("grouped under handle", groups[0]["handle"] == "shirolagos" and groups[0]["eventCount"] == 1)

    # Same show promoted across posts should collapse.
    kaffy_posts = [
        {
            "_id": "terrakulture:a",
            "handle": "terrakulture",
            "shortcode": "a",
            "postedAt": "2026-08-07T12:00:00Z",
            "caption": (
                "DEAR KAFFY: DIARY OF A SINGLE WOMAN\n"
                "7PM TONIGHT\nThe Shaw Theatre, London\nTickets via link in bio."
            ),
            "permalink": "https://www.instagram.com/p/a/",
        },
        {
            "_id": "terrakulture:b",
            "handle": "terrakulture",
            "shortcode": "b",
            "postedAt": "2026-08-05T12:00:00Z",
            "caption": (
                "In the final episode of The Director’s Series, @bolanleaustenpeters "
                "welcomes you as Dear Kaffy: Diary of a Single Woman officially opens "
                "at Shaw Theatre.\nAugust 7–9\nTickets: https://example.com/t"
            ),
            "permalink": "https://www.instagram.com/p/b/",
        },
        {
            "_id": "terrakulture:c",
            "handle": "terrakulture",
            "shortcode": "c",
            "postedAt": "2026-08-01T12:00:00Z",
            "caption": (
                "Dear Kaffy London is coming.\nShaw Theatre\n7–9 August\n"
                "Book now via link in bio #DearKaffyLondon"
            ),
            "permalink": "https://www.instagram.com/p/c/",
        },
        {
            "_id": "terrakulture:d",
            "handle": "terrakulture",
            "shortcode": "d",
            "postedAt": "2026-08-07T18:00:00Z",
            "caption": (
                "Doors open today. 🎭✨\n\n"
                "From the rehearsal room to the Shaw Theatre stage, "
                "Dear Kaffy London: Diary of a Single Woman is here.\n\n"
                "Aug 7–9 | Shaw Theatre, London. Tickets in bio."
            ),
            "permalink": "https://www.instagram.com/p/d/",
        },
    ]
    kaffy = event_extract.extract_events(kaffy_posts, use_card_ocr=False, use_llm=False)
    check("dear kaffy deduped to one", len(kaffy) == 1, len(kaffy))
    if kaffy:
        check("dedupe keeps source posts", (kaffy[0].get("postCount") or 0) >= 3, kaffy[0].get("postCount"))
        check(
            "dedupe name is Dear Kaffy",
            "kaffy" in ((kaffy[0].get("experience") or {}).get("name") or "").lower(),
            (kaffy[0].get("experience") or {}).get("name"),
        )
    prose = event_extract._experience_name(
        "Doors open today. From the stage, Dear Kaffy London: Diary of a Single Woman is here."
    )
    check("inline colon title parsed", "kaffy" in prose.lower() and "untitled" not in prose.lower(), prose)

    ferrari_caption = (
        "🚨FRIDAY HAS ONLY ONE ADDRESS. 🚨\n"
        "The lights are on. The DJs are locked in.\n"
        "This isn't just another night out, it's Ferrari Friday at Red Bar Lagos!\n"
        "🕘 9PM Till Dawn\n🎟️ Reserve your table now via DM.\n"
        "#ferrarifriday #redbarlagos #lagosevents"
    )
    ferrari_name = event_extract._experience_name(ferrari_caption)
    check(
        "ferrari friday beats slogan",
        ferrari_name.lower() == "ferrari friday",
        ferrari_name,
    )
    check(
        "ferrarifriday hashtag splits",
        event_extract._hashtag_to_title("ferrarifriday").lower() == "ferrari friday",
        event_extract._hashtag_to_title("ferrarifriday"),
    )
    ferrari_draft = event_extract.experience_from_post(
        {
            "_id": "redbar:ferrari",
            "handle": "redbarlagos",
            "caption": ferrari_caption,
            "permalink": "https://www.instagram.com/p/DbbC8cStzJo/",
        },
        use_card_ocr=False,
        use_llm=False,
    )
    check("ferrari draft qualifies", ferrari_draft is not None)
    if ferrari_draft:
        check(
            "ferrari draft name",
            ((ferrari_draft.get("experience") or {}).get("name") or "").lower()
            == "ferrari friday",
            (ferrari_draft.get("experience") or {}).get("name"),
        )

    tepp_caption = (
        "You say Hibachi. We say Teppanyaki.\n"
        "Teppanyaki Seatings:\n"
        "Monday: Closed\n"
        "Tuesday to Friday: 3:00 PM to 5:00 PM\n"
        "#ShiroLagos #Teppanyaki"
    )
    check(
        "schedule status is not a name",
        event_extract._is_bad_experience_name("Closed Tuesday"),
    )
    check(
        "teppanyaki beats Closed Tuesday",
        event_extract._experience_name(tepp_caption).lower() == "teppanyaki",
        event_extract._experience_name(tepp_caption),
    )
    tepp_draft = event_extract.experience_from_post(
        {
            "_id": "shirolagos:DbAmVuCuw_Q",
            "handle": "shirolagos",
            "caption": (
                "The only acceptable form of martial 'culinary' art.\n"
                "You say Hibachi. We say Teppanyaki.\n"
                "Teppanyaki Seatings:\n"
                "Monday: Closed\n"
                "Tuesday to Friday: 3:00 PM to 5:00 PM | 7:30 PM to 9:30 PM\n"
                "Saturday to Sunday: 1:00 PM to 3:30 PM\n"
                "Secure your front-row seat: Link in bio\n"
                "#ShiroLagos #Teppanyaki"
            ),
        },
        use_card_ocr=False,
        use_llm=False,
    )
    check("teppanyaki draft qualifies", tepp_draft is not None)
    if tepp_draft:
        check(
            "teppanyaki draft name",
            ((tepp_draft.get("experience") or {}).get("name") or "").lower() == "teppanyaki",
            (tepp_draft.get("experience") or {}).get("name"),
        )


def main() -> int:
    print("=" * 62)
    print("Instagram ingest — offline verification")
    print("=" * 62)

    test_graph_normalization()
    test_web_normalization()
    test_polaris_normalization()
    test_change_detection()
    test_tiers()
    test_capacity()
    test_rate_limiter()
    test_circuit_breaker()
    test_watermark()
    test_event_extract()

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
