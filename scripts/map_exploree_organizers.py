"""
Map Lagos event hosts into the exploree-api ``Organizer`` create shape.

Organizers are what `Experience.owner` points at when `sourceType` is
Organizer, so this is the import that unblocks the Tix experiences. Tix's
`User` node carries a `bio`, which is the only source anywhere for the
required `description`.

    python scripts/map_exploree_organizers.py --count 5

Joi-forbidden create keys (organizerId, logo) are omitted. `sourceRef` is
provenance for resolving `Experience.owner` later — strip before POSTing.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A Tix account is either a brand or a private individual. Two given names and
# nothing else is a person, and a person's email/phone is personal data — not
# the same thing as a venue's booking line.
_BUSINESS_HINT = re.compile(
    r"\b(ltd|limited|llc|inc|company|co|studios?|events?|lounge|restaurant|"
    r"bar|club|cafe|kitchen|group|hub|academy|school|church|foundation|"
    r"media|productions?|entertainment|africa|lagos|nigeria)\b",
    re.I,
)


def looks_like_individual(org: dict[str, Any]) -> bool:
    name = (org.get("name") or "").strip()
    if not name:
        return False
    if _BUSINESS_HINT.search(name):
        return False
    # A brand can sit in the name field too ('WAFFLESNCREAM'), so matching the
    # account holder is necessary but not sufficient — a person's name is also
    # multi-token and title-cased.
    if len(name.split()) < 2 or not name.istitle():
        return False
    person = (org.get("personName") or "").strip()
    return bool(person) and person.lower() == name.lower()


def collect_organizers(*, limit: int, detail_cap: int) -> list[dict[str, Any]]:
    """Tix events → deduped organizer accounts (one detail call per event)."""
    from discover.tix import fetch_tix_event_detail, fetch_tix_events

    events = fetch_tix_events(limit=limit, lagos_only=True)
    print(f"  tix: {len(events)} events", file=sys.stderr)

    by_id: dict[str, dict[str, Any]] = {}
    for event in events[:detail_cap]:
        slug = event.get("slug")
        if not slug:
            continue
        detail = fetch_tix_event_detail(str(slug)) or {}
        org = detail.get("organizer")
        if not org or not org.get("id"):
            continue
        existing = by_id.setdefault(org["id"], {**org, "events": [], "accountIds": []})
        existing["events"].append(event.get("name"))
        if org["id"] not in existing["accountIds"]:
            existing["accountIds"].append(org["id"])
        # Later events can carry fields an earlier one left blank.
        for key, value in org.items():
            if value and not existing.get(key):
                existing[key] = value

    # One brand can hold several Tix accounts (LDMA LIMITED has two). Distinct
    # ids are correct upstream but would create duplicate Organizer rows here,
    # so collapse on name and keep the richest record.
    by_name: dict[str, dict[str, Any]] = {}
    for org in by_id.values():
        key = re.sub(r"[^a-z0-9]", "", (org.get("name") or "").lower())
        if not key:
            by_name[org["id"]] = org
            continue
        merged = by_name.get(key)
        if merged is None:
            by_name[key] = org
            continue
        for field, value in org.items():
            if field in ("events", "accountIds"):
                merged.setdefault(field, []).extend(value or [])
            elif value and not merged.get(field):
                merged[field] = value

    print(
        f"  {len(by_id)} accounts → {len(by_name)} unique organizers",
        file=sys.stderr,
    )
    return list(by_name.values())


_ORG_SYSTEM = """You read Google result snippets about an event organizer and
write a factual one-paragraph description of them. JSON only.

  description: 2-3 sentences describing what this organizer actually does,
               built only from the snippets. Null if the snippets do not
               establish what they are.
  website:     their own site, or null
  dateEstablished: "YYYY" or null, only if a snippet states when they started
  evidence:    the snippet phrases you relied on

The danger is same-name confusion: an organizer called "Chapter" is not the
publisher, the film, or a business in another country. If the snippets are
about something else, return nulls. Never invent activities or credentials.
"""


def enrich_organizer(org: dict[str, Any], *, client: Any = None) -> dict[str, Any]:
    """
    Fill an organizer's gaps the same way the restaurant mapper does.

    Tix supplies a bio for about two thirds of accounts. For the rest, search
    plus an LLM is the only route to the required `description`.
    """
    from config import settings
    from discover.places import handle_from_search, harvest_contacts
    from pipeline import deepseek_extract as ds

    name = (org.get("name") or "").strip()
    out: dict[str, Any] = {}
    if not name:
        return out

    # Instagram handle, same validation the venue path uses.
    if not org.get("instagram"):
        handle = handle_from_search(name, city="Lagos", client=client)
        if handle:
            out["instagram"] = handle

    # WhatsApp / extra email from their own site, never from search results.
    if org.get("website"):
        found = harvest_contacts(org["website"], client=client)
        if found.get("whatsApp"):
            out["whatsApp"] = found["whatsApp"]
        if found.get("emails") and not org.get("email"):
            out["email"] = found["emails"][0]

    needs_description = not (org.get("bio") or "").strip()
    if not (needs_description or not org.get("website")):
        return out
    if not settings.SERPER_API_KEY or not ds.enabled():
        return out

    import httpx

    snippets: list[str] = []
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.post(
            settings.SERPER_ENDPOINT,
            headers={
                "X-API-KEY": settings.SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "q": f"{name} Lagos events organizer OR company",
                "num": 8,
                "gl": settings.SERPER_COUNTRY,
            },
        )
        if resp.status_code < 400:
            for item in (resp.json().get("organic") or []):
                text = f"{item.get('title','')} — {item.get('snippet','')}".strip(" —")
                if text:
                    snippets.append(text[:300])
    except Exception as exc:  # noqa: BLE001
        print(f"    organizer serp failed: {exc}", file=sys.stderr)
    finally:
        if own and http is not client:
            http.close()
    if not snippets:
        return out

    key = ds._cache_key(
        handle="organizer-facts", post_id=name,
        caption=" || ".join(snippets)[:4000], ocr_text="",
    )
    cached = ds.read_cached(key)
    if cached is None:
        original = ds._SYSTEM
        try:
            ds._SYSTEM = _ORG_SYSTEM
            result = ds._call_api(
                json.dumps({"organizer": name, "snippets": snippets}, ensure_ascii=False)
            ) or {}
        finally:
            ds._SYSTEM = original
        cached = {}
        if isinstance(result.get("description"), str) and len(result["description"]) > 40:
            cached["description"] = result["description"].strip()[:600]
        if isinstance(result.get("website"), str) and result["website"].startswith("http"):
            cached["website"] = result["website"]
        year = str(result.get("dateEstablished") or "")
        if re.fullmatch(r"(?:19|20)\d{2}", year):
            cached["dateEstablished"] = f"{year}-01-01"
        ds.write_cached(key, cached)

    for field in ("description", "website", "dateEstablished"):
        if cached.get(field) and not org.get(field if field != "description" else "bio"):
            out[field] = cached[field]
    return out


def to_organizer_record(
    org: dict[str, Any], *, instagram: str | None = None
) -> dict[str, Any]:
    name = (org.get("name") or "").strip() or None
    record = {
        "name": name,
        # REQUIRED. Tix bio when the account wrote one, otherwise a
        # search-derived summary. Null only when neither exists.
        "description": org.get("bio") or org.get("description"),
        "socialMedia": {"ig": instagram or org.get("instagram"), "twitter": None}
        if (instagram or org.get("instagram")) else None,
        "emails": [org["email"]] if org.get("email") else None,
        "phones": [org["phone"]] if org.get("phone") else None,
        "website": org.get("website"),
        "whatsApp": org.get("whatsApp"),
        # Tix `createdAt` is account signup, not when the org was founded, so
        # it is never used. A stated founding year from search is.
        "dateEstablished": org.get("dateEstablished"),
        # Not part of addOrganizerValidation — strip before POSTing.
        "sourceRef": {
            "provider": "tix",
            "externalId": org.get("id"),
            "accountIds": org.get("accountIds") or [org.get("id")],
        },
        "tixUsername": org.get("username"),
        "eventCount": len(org.get("events") or []),
        "isIndividual": looks_like_individual(org),
    }
    missing = [k for k in ("name", "description") if not record.get(k)]
    filled = [k for k, v in record.items() if v not in (None, [], {}, False)]
    return {"record": record, "_meta": {"missingRequired": missing, "filled": len(filled)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--per-source", type=int, default=40)
    ap.add_argument("--detail-cap", type=int, default=40, help="max Tix detail calls")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--no-ig", action="store_true", help="skip website→IG lookup")
    ap.add_argument(
        "--no-enrich", action="store_true",
        help="skip search-derived description, contacts and handle lookup",
    )
    ap.add_argument(
        "--postable-only", action="store_true",
        help="only sample organizers that already satisfy name+description",
    )
    ap.add_argument("--out", default="docs/sample_payloads/exploree_organizers_sample.json")
    args = ap.parse_args()

    print("fetching Lagos event hosts…", file=sys.stderr)
    organizers = collect_organizers(limit=args.per_source, detail_cap=args.detail_cap)
    if not organizers:
        print("no organizers found", file=sys.stderr)
        return 1

    pool = organizers
    if args.postable_only:
        pool = [o for o in pool if (o.get("name") or "").strip() and o.get("bio")] or pool

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    sample = rng.sample(pool, min(args.count, len(pool)))

    if not args.no_enrich:
        import httpx

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            filled = 0
            for org in sample:
                extra = enrich_organizer(org, client=client)
                org.update(extra)
                filled += len(extra)
        print(f"  enriched: {filled} values across {len(sample)} organizers", file=sys.stderr)

    handles: list[str | None] = []
    if args.no_ig:
        handles = [None] * len(sample)
    else:
        import httpx

        from discover.places import extract_instagram_handle, handle_from_website

        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            for org in sample:
                site = org.get("website")
                handle = extract_instagram_handle(site) if site else None
                if not handle and site:
                    handle = handle_from_website(
                        site, name=org.get("name") or "", client=client
                    )
                handles.append(handle)
    print(f"  instagram: {sum(1 for h in handles if h)}/{len(sample)}", file=sys.stderr)

    mapped = [
        to_organizer_record(o, instagram=h) for o, h in zip(sample, handles)
    ]
    for row in mapped:
        rec, meta = row["record"], row["_meta"]
        flag = " [individual]" if rec["isIndividual"] else ""
        state = "postable" if not meta["missingRequired"] else f"missing {meta['missingRequired']}"
        print(f"  {rec['name']}: {state}{flag}", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([m["record"] for m in mapped], indent=2, ensure_ascii=False, default=str)
    )
    print(f"wrote {out} (seed={seed})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
