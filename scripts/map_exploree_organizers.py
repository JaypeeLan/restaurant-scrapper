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


def to_organizer_record(
    org: dict[str, Any], *, instagram: str | None = None
) -> dict[str, Any]:
    name = (org.get("name") or "").strip() or None
    record = {
        "name": name,
        "description": org.get("bio"),          # REQUIRED — Tix `bio`
        "socialMedia": {"ig": instagram, "twitter": None} if instagram else None,
        "emails": [org["email"]] if org.get("email") else None,
        "phones": [org["phone"]] if org.get("phone") else None,
        "website": org.get("website"),
        "whatsApp": None,                        # no source
        # Tix `createdAt` is account signup, not when the org was founded —
        # using it would assert something false.
        "dateEstablished": None,
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
