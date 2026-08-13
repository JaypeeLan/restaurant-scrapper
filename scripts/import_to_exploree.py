"""
Turn the mapped records into an ordered set of Exploree API calls.

Order is not a preference. `Experience.owner` is a Mongo id with `refPath`
pointing at Organizer or Restaurant, so those have to exist before any
experience can reference one. This script writes organizers and restaurants
first, keeps the ids it gets back, and resolves each experience's `ownerRef`
against them.

    python scripts/import_to_exploree.py                  # validate only
    python scripts/import_to_exploree.py --mongo          # show what it would write
    python scripts/import_to_exploree.py --mongo --write  # write it

This service is standalone, so the default path writes straight to the product
database using MONGODB_URI. Only records that pass validation are written:
a partial venue is worse than an absent one.

Nothing is written without `--write`. Re-runs upsert rather than duplicate,
matching restaurants on googlePlaceId and the other two on stored provenance.

An API path exists too (`--api-base` with an admin JWT, since every create sits
behind adminAuth(Access.Editor)), but it is not the normal route for a
standalone service.

Every create schema is `.unknown(false)`, so provenance keys the mappers add
(sourceRef, ownerRef, googlePlaceId, rating, photos) are stripped from the
body and kept only in the plan for matching.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLES = Path("docs/sample_payloads")

# Keys the mappers attach for provenance or convenience. None belong in a POST
# body: Joi rejects unknown keys outright.
STRIP = {
    "sourceRef", "ownerRef", "googlePlaceId", "rating", "photos", "menuUrl",
    "isIndividual", "eventCount", "tixUsername", "imageUrl", "sourceUrl",
    "_meta", "_evidence",
}

REQUIRED = {
    "organizers": ["name", "description"],
    "restaurants": [
        "name", "address", "openingTimes", "meal", "service", "lighting",
        "minimumSpend", "dressCode", "seatingOptions",
    ],
    "experiences": [
        "name", "description", "tags", "categories", "schedule",
        "pricePoints", "sourceType", "location", "owner",
    ],
}

# Verified against exploree-api: routes mount under /v1 in
# src/core/server/index.ts, and collections are plural in app/routes.ts.
ENDPOINTS = {
    "organizers": "/v1/organizers",
    "restaurants": "/v1/restaurants",
    "experiences": "/v1/experiences",
}


def load(kind: str) -> list[dict[str, Any]]:
    path = SAMPLES / f"exploree_{kind}_sample.json"
    if not path.exists():
        print(f"  {kind}: no sample file at {path}", file=sys.stderr)
        return []
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else []


def to_body(record: dict[str, Any]) -> dict[str, Any]:
    """Schema keys only, and drop nulls the API would rather not receive."""
    return {
        k: v for k, v in record.items()
        if k not in STRIP and not k.startswith("_") and v is not None
    }


# Read the enums out of the API source rather than restating them here, so a
# schema change upstream shows up as a validation failure instead of silently
# passing records the API will reject.
EXPLOREE_SRC = Path("/Users/mac/Desktop/exploree-api/src/app/services")
_TYPE_FILES = (
    EXPLOREE_SRC / "restaurant/restaurant/restaurant.types.ts",
    EXPLOREE_SRC / "experience/experience.types.ts",
)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Lagos sits near 6.5 N, 3.4 E. Anything outside this box is a bad geocode or
# a swapped lat/lng pair.
_LAGOS_BOX = {"lng": (2.5, 4.5), "lat": (6.0, 7.2)}

ENUM_FIELDS = {
    "restaurants": {
        "lighting": "Lighting", "bathroom": "BathroomQuality",
        "picturesque": "Picturesque", "coziness": "Coziness",
        "music": "Music", "serviceSpeed": "ServiceSpeed",
        "cuisine": "Cuisine", "meal": "Meal", "service": "Service",
        "dietaryOptions": "DietaryOption", "suitableFor": "Suitability",
        "seatingOptions": "SeatingOption",
    },
    "experiences": {
        "categories": "ExperienceCategory", "ageLimit": "AgeLimit",
        "sourceType": "SourceType",
    },
    "organizers": {},
}


def load_enums() -> dict[str, set[str]]:
    """Parse `export enum X { A = 'a' }` blocks out of the API's TypeScript."""
    enums: dict[str, set[str]] = {}
    for path in _TYPE_FILES:
        if not path.exists():
            continue
        src = path.read_text()
        for match in re.finditer(r"export enum (\w+) \{(.*?)\}", src, re.S):
            enums[match.group(1)] = set(re.findall(r"=\s*'([^']+)'", match.group(2)))
    return enums


def validate(kind: str, body: dict[str, Any], enums: dict[str, set[str]]) -> list[str]:
    """Everything the API would reject, beyond a field simply being absent."""
    problems: list[str] = []

    for field, enum_name in ENUM_FIELDS[kind].items():
        allowed = enums.get(enum_name)
        value = body.get(field)
        if not allowed or value in (None, [], {}):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item not in allowed:
                problems.append(f"{field}={item!r} not in {enum_name}")

    hours = body.get("openingTimes") or {}
    if isinstance(hours, dict):
        for day, span in hours.items():
            if not isinstance(span, dict):
                continue
            for edge in ("open", "close"):
                clock = span.get(edge)
                if clock and not _HHMM.match(str(clock)):
                    problems.append(f"openingTimes.{day}.{edge}={clock!r} not HH:MM")

    schedule = body.get("schedule") or {}
    if isinstance(schedule, dict) and schedule:
        for edge in ("startTime", "endTime"):
            clock = schedule.get(edge)
            if clock and not _HHMM.match(str(clock)):
                problems.append(f"schedule.{edge}={clock!r} not HH:MM")
        event_type = schedule.get("eventType")
        # Joi forbids `date` on recurring and `recurrence` on one-time; sending
        # both is a rejection, not a merge.
        if event_type == "recurring" and schedule.get("date"):
            problems.append("schedule.date present on a recurring event (forbidden)")
        if event_type == "one-time" and schedule.get("recurrence"):
            problems.append("schedule.recurrence present on a one-time event (forbidden)")

    geo_key = {"restaurants": "address", "experiences": "location"}.get(kind)
    if geo_key:
        coords = (body.get(geo_key) or {}).get("coordinates")
        if isinstance(coords, list) and len(coords) == 2:
            lng, lat = coords
            if not (_LAGOS_BOX["lng"][0] <= lng <= _LAGOS_BOX["lng"][1]):
                problems.append(f"{geo_key}.coordinates lng={lng} outside Lagos")
            if not (_LAGOS_BOX["lat"][0] <= lat <= _LAGOS_BOX["lat"][1]):
                problems.append(f"{geo_key}.coordinates lat={lat} outside Lagos")
        elif coords is not None:
            problems.append(f"{geo_key}.coordinates malformed: {coords!r}")

    for field in ("pricePoints",):
        for point in body.get(field) or []:
            if not isinstance(point, dict):
                continue
            if not point.get("type"):
                problems.append("pricePoints[].type is required")
            if point.get("price") is None:
                problems.append("pricePoints[].price is required")

    return problems


def missing_required(kind: str, body: dict[str, Any], record: dict[str, Any]) -> list[str]:
    gaps = [f for f in REQUIRED[kind] if body.get(f) in (None, [], {})]
    # Organizer has no address or location; only the other two carry geo.
    geo_key = {"restaurants": "address", "experiences": "location"}.get(kind)
    if geo_key:
        geo = body.get(geo_key) or {}
        if isinstance(geo, dict) and geo.get("coordinates") in (None, []):
            gaps.append(f"{geo_key}.coordinates")
    if kind == "experiences" and not record.get("ownerRef"):
        gaps.append("ownerRef (cannot resolve owner)")
    return gaps


def owner_key(ref: dict[str, Any] | None) -> str | None:
    """Stable key for matching an experience to the organizer it belongs to."""
    if not isinstance(ref, dict):
        return None
    external = ref.get("externalId")
    if external:
        return f"{ref.get('provider')}:{external}"
    name = (ref.get("name") or "").strip().lower()
    return f"name:{name}" if name else None


def organizer_keys(record: dict[str, Any]) -> list[str]:
    """Every id an experience might reference this organizer by."""
    ref = record.get("sourceRef") or {}
    keys = [f"{ref.get('provider')}:{i}" for i in (ref.get("accountIds") or []) if i]
    if ref.get("externalId"):
        keys.append(f"{ref.get('provider')}:{ref['externalId']}")
    name = (record.get("name") or "").strip().lower()
    if name:
        keys.append(f"name:{name}")
    return list(dict.fromkeys(keys))


COLLECTIONS = {
    "organizers": "organizers",
    "restaurants": "restaurants",
    "experiences": "experiences",
}

# Direct writes bypass Mongoose, so the fields it would have added have to be
# set here. Matches the shape of the rows already in the database.
DOC_DEFAULTS = {
    "active": False,   # inferred fields need a human pass before going live
    "archived": False,
    "views": 0,
}


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return base or "venue"


def upsert_filter(kind: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """
    A stable identity for re-running the import.

    Restaurants match the existing rows' `googlePlaceId`. The other two have no
    natural key in the schema, so provenance is stored on the document and
    matched on instead. Without this a second run duplicates everything.
    """
    if kind == "restaurants":
        place_id = record.get("googlePlaceId")
        return {"googlePlaceId": place_id} if place_id else None
    ref = record.get("sourceRef") or record.get("ownerRef") or {}
    external = ref.get("externalId")
    if external:
        return {"source.provider": ref.get("provider"), "source.externalId": external}
    name = (record.get("name") or "").strip()
    return {"name": name} if name else None


def to_document(kind: str, body: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Request body plus the fields Mongoose would have supplied."""
    now = datetime.now(timezone.utc)
    doc = dict(body)
    doc.update(DOC_DEFAULTS)
    doc["slug"] = slugify(record.get("name") or "")
    doc["updatedAt"] = now
    if kind == "restaurants" and record.get("googlePlaceId"):
        doc["googlePlaceId"] = record["googlePlaceId"]
    # Provenance is kept here, unlike the API path where `.unknown(false)`
    # forbids it. It is what makes a re-run idempotent.
    ref = record.get("sourceRef") or record.get("ownerRef")
    if isinstance(ref, dict) and ref.get("externalId"):
        doc["source"] = {
            "provider": ref.get("provider"),
            "externalId": ref.get("externalId"),
            "accountIds": ref.get("accountIds") or [],
        }
    return doc


def write_mongo(
    uri: str, plan_steps: list[dict[str, Any]], *, execute: bool, db_name: str = ""
) -> dict[str, Any]:
    """
    Upsert valid records straight into the product database.

    Only records with no missing required fields and no invalid values are
    written. Everything else is left for a human, because a partial venue is
    worse than an absent one.
    """
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=15000)
    # MONGODB_URI often carries no database in its path, so fall back to the
    # configured name rather than failing at connect time.
    try:
        db = client.get_default_database()
    except Exception:  # noqa: BLE001
        if not db_name:
            raise
        db = client[db_name]
    stats = {"inserted": 0, "updated": 0, "wouldWrite": 0, "skipped": 0, "unresolved": 0}
    organizer_ids: dict[str, Any] = {}

    try:
        for step in plan_steps:
            kind = step["entity"]
            record = step["_record"]
            if step["missingRequired"] or step["invalidValues"]:
                stats["skipped"] += 1
                continue

            body = dict(step["body"])
            if kind == "experiences":
                key = owner_key(record.get("ownerRef"))
                owner_id = organizer_ids.get(key) if key else None
                if not owner_id:
                    stats["unresolved"] += 1
                    step["error"] = "owner not resolved"
                    continue
                body["owner"] = owner_id

            doc = to_document(kind, body, record)
            where = upsert_filter(kind, record)
            if not where:
                stats["skipped"] += 1
                continue

            if not execute:
                step["wouldWrite"] = {"collection": COLLECTIONS[kind], "filter": where}
                stats["wouldWrite"] += 1
                # Organizer ids only exist after a real write, so experiences
                # cannot resolve an owner during a dry run. Counting them as
                # unresolved here would be misleading.
                continue

            collection = db[COLLECTIONS[kind]]
            result = collection.update_one(
                where,
                {"$set": doc, "$setOnInsert": {"createdAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
            if result.upserted_id:
                stats["inserted"] += 1
                doc_id = result.upserted_id
            else:
                stats["updated"] += 1
                existing = collection.find_one(where, {"_id": 1}) or {}
                doc_id = existing.get("_id")
            step["writtenId"] = str(doc_id) if doc_id else None

            if kind == "organizers" and doc_id:
                for key in organizer_keys(record):
                    organizer_ids[key] = doc_id
    finally:
        client.close()
    return stats


def post(client: Any, base: str, path: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(
        f"{base.rstrip('/')}{path}",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        json=body,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    payload = resp.json()
    # The API always answers {success, message, data}. A 200 carrying
    # success:false is still a failure, so the status code is not the verdict.
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"api rejected: {payload.get('message')}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default=None, help="e.g. https://api.exploree.app")
    ap.add_argument(
        "--token", default="",
        help="admin JWT signed with secretKeyAdmin, Editor access or higher",
    )
    ap.add_argument("--out", default="docs/sample_payloads/exploree_import_plan.json")
    ap.add_argument(
        "--mongo", action="store_true",
        help="write straight to the product database instead of the API",
    )
    ap.add_argument("--db-name", default="", help="override MONGODB_DB_NAME")
    ap.add_argument(
        "--write", action="store_true",
        help="actually execute the writes. Without it --mongo only reports.",
    )
    ap.add_argument(
        "--skip-invalid", action="store_true",
        help="drop records missing required fields instead of listing them",
    )
    args = ap.parse_args()
    live = bool(args.api_base)

    enums = load_enums()
    if not enums:
        print("  ! could not read API enums; validation limited", file=sys.stderr)
    else:
        print(f"  loaded {len(enums)} enums from the API source", file=sys.stderr)

    data = {kind: load(kind) for kind in ("organizers", "restaurants", "experiences")}
    for kind, rows in data.items():
        print(f"  {kind}: {len(rows)} records", file=sys.stderr)

    plan: dict[str, Any] = {
        "note": (
            "Ordered import. Organizers and restaurants first so experiences "
            "can resolve owner. Bodies contain schema keys only."
        ),
        "mode": "live" if live else "dry-run",
        "steps": [],
    }
    # Organizer key -> created id. In a dry run these stay null and the
    # experience step reports which owner it was unable to resolve.
    resolved: dict[str, str | None] = {}
    counts = {"posted": 0, "skipped": 0, "invalid": 0}

    client = None
    if live:
        import httpx

        client = httpx.Client(timeout=60.0)

    try:
        for kind in ("organizers", "restaurants", "experiences"):
            for record in data[kind]:
                body = to_body(record)

                if kind == "experiences":
                    key = owner_key(record.get("ownerRef"))
                    owner_id = resolved.get(key) if key else None
                    if owner_id:
                        body["owner"] = owner_id

                gaps = missing_required(kind, body, record)
                invalid = validate(kind, body, enums)
                step = {
                    "entity": kind,
                    "name": record.get("name"),
                    "method": "POST",
                    "path": ENDPOINTS[kind],
                    "body": body,
                    "missingRequired": gaps,
                    "invalidValues": invalid,
                }
                if kind == "experiences":
                    step["ownerRef"] = record.get("ownerRef")
                step["_record"] = record
                if kind == "restaurants" and record.get("googlePlaceId"):
                    # Existing rows are keyed on this; match before inserting.
                    step["matchOn"] = {"googlePlaceId": record["googlePlaceId"]}

                if gaps or invalid:
                    counts["invalid"] += 1
                    if args.skip_invalid:
                        counts["skipped"] += 1
                        continue

                if live and not gaps and not invalid:
                    try:
                        created = post(client, args.api_base, ENDPOINTS[kind], args.token, body)
                        created_data = created.get("data") or {}
                        new_id = (
                            created_data.get("_id")
                            or created_data.get("organizerId")
                            or created_data.get("id")
                        )
                        step["createdId"] = new_id
                        counts["posted"] += 1
                        if kind == "organizers" and new_id:
                            for k in organizer_keys(record):
                                resolved[k] = str(new_id)
                    except Exception as exc:  # noqa: BLE001
                        step["error"] = str(exc)
                        print(f"  ! {kind} {record.get('name')}: {exc}", file=sys.stderr)
                plan["steps"].append(step)
    finally:
        if client is not None:
            client.close()

    if args.mongo:
        from config import settings

        uri = settings.MONGODB_URI.strip()
        if not uri:
            print("  ! MONGODB_URI is not set; nothing written", file=sys.stderr)
        else:
            stats = write_mongo(
                uri, plan["steps"], execute=args.write,
                db_name=args.db_name or settings.MONGODB_DB_NAME,
            )
            plan["mongo"] = stats
            if args.write:
                print(
                    f"  mongo wrote: {stats['inserted']} inserted, "
                    f"{stats['updated']} updated, {stats['skipped']} skipped "
                    f"(invalid), {stats['unresolved']} owner unresolved",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  mongo dry run: {stats['wouldWrite']} would be written, "
                    f"{stats['skipped']} skipped as invalid. Pass --write to execute.",
                    file=sys.stderr,
                )

    by_entity: dict[str, dict[str, int]] = {}
    for step in plan["steps"]:
        bucket = by_entity.setdefault(step["entity"], {"total": 0, "ready": 0})
        bucket["total"] += 1
        if not step["missingRequired"] and not step["invalidValues"]:
            bucket["ready"] += 1
    plan["summary"] = {"byEntity": by_entity, **counts}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for step in plan["steps"]:
        step.pop("_record", None)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str))

    print("", file=sys.stderr)
    for entity, bucket in by_entity.items():
        print(f"  {entity:<13} {bucket['ready']}/{bucket['total']} ready to POST", file=sys.stderr)
    if not live:
        print(
            "  dry run: nothing was sent. Pass --api-base and --token to execute.",
            file=sys.stderr,
        )
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
