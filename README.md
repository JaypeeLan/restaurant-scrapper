# Lagos venue and event ingest

Collects Lagos restaurants, events and event hosts from public sources, and
maps them into the shapes the Exploree API expects (`Restaurant`, `Experience`,
`Organizer`).

The output is JSON, not database writes. Nothing here posts to Exploree yet.

## What it does

Four mapping scripts, one per entity:

| Script | Produces | Sources |
|---|---|---|
| `scripts/map_exploree_restaurants.py` | `Restaurant` records | Google Places, FlavorQueste, Serper, Instagram, Gemini vision, DeepSeek |
| `scripts/map_exploree_experiences.py` | `Experience` records | Tix Africa, Google geocoding, DeepSeek |
| `scripts/map_exploree_organizers.py` | `Organizer` records | Tix Africa |
| `scripts/map_exploree_menus.py` | `Menu` records, one per dish | the venue's menu PDF or page |

There is also an older Instagram post ingest (`main.py`, `run_ingest.py`,
`serve.py`, `web/`) that stores posts in MongoDB and serves a dashboard. It
predates the mapping work and runs independently. See `docs/architecture.md`.

## Quick start

```bash
cp .env.example .env          # fill in the keys listed below
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

python scripts/map_exploree_restaurants.py --count 5 --primary google
```

Output lands in `docs/sample_payloads/exploree_restaurants_sample.json` as a
plain array of records. Coverage stats and per-field provenance print to
stderr so they stay out of the data.

## Keys you need

Copy `.env.example` to `.env`, then fill these. Links go to where the key is created.

| Key | Needed for | Get it from | Without it |
|---|---|---|---|
| `GOOGLE_PLACES_API_KEY` | venue discovery, hours, coordinates, attributes, geocoding | [Google Cloud credentials](https://console.cloud.google.com/apis/credentials) — enable **Places API (New)** first ([docs](https://developers.google.com/maps/documentation/places/web-service/get-api-key)) | falls back to OpenStreetMap, loses most fields |
| `SERPER_API_KEY` | Instagram handles, venue facts, interior image search | [serper.dev](https://serper.dev/) → API Key (2,500 free queries) | no handles, no dress code, no founding year |
| `GEMINI_API_KEY` | reading venue photos for lighting, coziness, seating | [Google AI Studio](https://aistudio.google.com/apikey) | those fields stay null |
| `DEEPSEEK_API_KEY` | menu and review interpretation | [DeepSeek platform](https://platform.deepseek.com/api_keys) | no cuisine, meal, or ambience from text |
| `IG_COOKIES_FILE` | logged-in Instagram session (see below) | export from a logged-in browser — steps below | no profile WhatsApp / founding / grid photos; discover handle search fails without a session (Serper can still find handles) |
| `MONGODB_URI` | writing finished records to the product database | [MongoDB Atlas](https://cloud.mongodb.com/) → Connect → Drivers, or a local `mongodb://localhost:27017` | validation still runs, nothing is written |
| `MONGODB_DB_NAME` | which Atlas/local database to write | same Atlas cluster (database name you choose) | defaults in code; set it to match Exploree |

### Instagram cookies — how to get them and what they do

Instagram has no public search/profile API for third-party apps. A **logged-in browser session** (cookies) is how this repo talks to Instagram’s private web endpoints. Cookies are **not** used for normal post ingest (that stays on Graph API or logged-out Playwright).

**What they unlock**

| Use | Code | Without cookies |
|---|---|---|
| Venue → IG handle via Instagram topsearch | `ig/logged_in_search.py`, `main.py discover` | discover cannot resolve handles that way; restaurant mapping can still use Serper (`site:instagram.com`) |
| Profile bio / link-in-bio / grid photos | `ig/logged_in_profile.py`, `map_exploree_restaurants.py` | WhatsApp, founding year from bio, and grid photos stay empty |

Required cookies: `sessionid` and `csrftoken`. Optional but useful: `ds_user_id`, `mid`, `ig_did`.

**How to export (local)**

1. Log in to [instagram.com](https://www.instagram.com/) in Chrome (a normal personal account).
2. Install [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm).
3. On an Instagram tab, open Cookie-Editor → **Export** → **Netscape**.
4. Save the file as `cookies.txt` in the repo root (gitignored).
5. Leave `IG_COOKIES_FILE=cookies.txt` in `.env` (the default).

Alternatively, DevTools → Application → Cookies → `instagram.com`, or copy the request `Cookie` header into `IG_COOKIES`. Individual vars (`IG_SESSIONID`, `IG_CSRFTOKEN`, …) also work but go stale easily; the Netscape file wins when both are set.

**On Render**

Disk is ephemeral, so do not upload `cookies.txt`. Paste the full Netscape export into the multiline secret `IG_COOKIES_NETSCAPE` (or a Cookie header into `IG_COOKIES`). At startup the app writes that into `cookies.txt` for the same code path.

**Caveats**

- Sessions expire and Instagram will `checkpoint_required` if you hit search/profile too hard from a datacenter IP — keep gaps high (`IG_SEARCH_GAP_S`, profile pacing) or use a residential `IG_PROXY_URL`.
- Never commit cookies. Rotate the account password / log out other sessions if a `sessionid` was pasted into chat or logs.

### Optional

| Key | Needed for | Get it from |
|---|---|---|
| `IG_COOKIES_NETSCAPE` | same session as `IG_COOKIES_FILE`, for Render secrets | same Cookie-Editor Netscape export, pasted as multiline text |
| `IG_COOKIES` | same session, as a raw `Cookie:` header string | DevTools → Network → any instagram.com request → Request Headers |
| `IG_PROXY_URL` | Instagram from a datacenter IP (Render) | a residential proxy provider — public free lists are not usable for this |
| `IG_GRAPH_ACCESS_TOKEN` / `IG_GRAPH_USER_ID` | Meta Graph API as primary ingest (optional) | [Meta for Developers](https://developers.facebook.com/apps/) → Instagram Graph API |
| `GEMINI_MODEL` | which Gemini model to call | listed in [AI Studio](https://aistudio.google.com/); default `gemini-flash-latest` |

Gemini's free tier is around 200 requests a day, and free requests are
deprioritised under load, which shows up as "this model is currently
experiencing high demand". The script tries fallback models before giving up,
but a 500 venue run needs billing enabled on the project.

Full variable list (including pacing and cron knobs) lives in `.env.example`.

## How a restaurant record is built

1. Pull every provider. Google leads discovery, the rest enrich.
2. Cluster the same venue across providers by normalised name, with
   coordinates used to veto a match rather than require one.
3. Take each field from the provider that is most trustworthy for that field.
   Google wins on coordinates, hours and price range. FlavorQueste wins on
   ambience ratings. See `FIELD_PRECEDENCE` in the script.
4. Fill the gaps:
   - Instagram handle from the venue website, then Serper search
   - Coordinates from Google geocoding when no provider has them
   - Menu URL from the site, a link aggregator, or the Instagram bio link,
     followed through to the actual document rather than stopping at the
     aggregator
   - Emails from the venue's own domain, WhatsApp from its Linktree
   - Cuisine and meal service by reading the menu itself, falling back to the
     description, categories and reviews when a venue publishes no menu
   - Lighting, coziness, seating, decor by reading venue photos
   - Dress code, founding year, WhatsApp, bathroom from search results
5. Validate every enum value against the Exploree schema before emitting.

DeepSeek is the fallback wherever the preferred source is missing or
unavailable. Vision reads photos for lighting, coziness and seating; when
there are no usable photos, or Gemini is rate limited, DeepSeek answers the
same questions from review text instead. Menus are the preferred evidence for
cuisine and meal service, but plenty of small cafes publish none anywhere, so
it falls back to what the venue and its reviewers say. Fallback values are
weaker evidence and the prompts are told to return null rather than stretch.

Anything that cannot be established stays null. The script never guesses.

## Useful flags

```
--count N            how many venues to emit
--per-source N       how deep to pull from each provider
--primary google     lead provider, also restricts the sample to its venues
--max-reviews 300    only sample venues under this review count
--exclude FILE       skip venues already in a previous output file
--no-vision          skip the photo pass
--no-llm             skip DeepSeek
--no-ig-profile      skip Instagram
--seed N             reproduce an exact sample
```

## Things worth knowing

**Nightclubs are not restaurants.** The Google sweep asks only for food
serving types and drops `night_club`, `bar` and `meal_delivery` by primary
type. Google returns no meal attributes for a nightclub because a nightclub
does not serve meals, and importing them made `meal` look 43% missing when it
was really 95% complete for actual restaurants.

**Same name collisions are the main source of wrong data.** A search for
Tiffany Amber Cafe returns the 1998 founding of a fashion house. A search for
GABY Lagos returns a dress code belonging to a restaurant in New York. Every
search derived field goes through an LLM that is told to reject facts about a
different business, and returns null rather than a plausible guess.

**Instagram bios contain template placeholders.** One venue publishes
`instagram.com/yourpage`. Handles are checked against the venue name and
domain before they are accepted.

**Menus are not only on Linktree, and often do not exist.** The menu URL is
looked for on the venue site, on any link aggregator, and in the Instagram bio
link. Many Lagos cafes have no menu published anywhere, which is why cuisine
and meal have a non menu fallback rather than depending on finding one.

**Some hosts need a real browser.** Linktree returns 403 to every plain HTTP
client regardless of headers, because it fingerprints the connection rather
than reading the user agent. Those fetches fall back to Chromium, which is
also how the Instagram profile reads work.

**Instagram pacing.** Profile reads use a 20 to 55 second gap. The session has
been checkpointed once already by automated use. A full 500 venue pass is
roughly five hours and belongs in the background, not in a normal run.

## Field coverage

Measured on Lagos venues under 300 Google reviews, which is the hard case.

Reliable: name, address, coordinates, opening times, phones, rating, photos,
description, minimum spend, meal, service, social media, menu URL, dress code.

Partial: lighting and seating, which need photos that actually show the room
and are currently limited by Gemini free tier capacity rather than by data.
Also cuisine, bathroom, WhatsApp and founding date.

Rare: emails. Small venues mostly do not publish one. Harvesting is restricted
to the venue's own domain because open web search returns a nearby hotel's
address, a directory's placeholder, or a review blog's editor.

## Menus

`map_exploree_menus.py` itemises whatever the restaurant mapper found, turning
a menu into one row per dish with a name, price, category and type.

Run it after the restaurant mapper, since it reads that output.

Coverage is limited by what venues publish. A menu URL is often a Linktree or
a Threads profile rather than a menu, and following it through only helps if
a menu is actually linked there. On the last sample one venue in five had a
real menu; that one produced 36 dishes.

Two things are dropped rather than written: dishes with no price, because the
extractor defaults those to zero and every dish would publish as free, and any
category outside the API's enum.

Prices are read as printed. Lagos menus are usually in thousands, so `51.5`
becomes 51,500 rather than fifty naira.

## Pushing to the database

```bash
python scripts/import_to_exploree.py                  # validate only
python scripts/import_to_exploree.py --mongo          # show what would be written
python scripts/import_to_exploree.py --mongo --write  # write it
```

This service is standalone, so it writes to the product database directly
using `MONGODB_URI` and `MONGODB_DB_NAME`. Nothing is written without
`--write`.

Only records that pass validation are written. A record is valid when every
required field is present and every value is one the schema accepts, which is
checked by parsing the enums out of the API's TypeScript rather than by
restating them here. A partial venue is worse than an absent one.

Order matters and is enforced. Organizers and restaurants are written first
and their ids kept, then menus resolve `restaurantRef` and experiences resolve
`ownerRef` against them. Both are Mongo ids, so the rows they point at have to
exist first. In a dry run those ids do not exist yet,
so experiences report as unresolved rather than being counted as writable.

Re-running upserts instead of duplicating. Restaurants match on
`googlePlaceId`, which is what the rows already in the database are keyed on.
Organizers and experiences have no natural key in the schema, so provenance is
stored on the document under `source` and matched on next time.

Written documents also carry the fields Mongoose would normally add:
`slug`, `createdAt`, `updatedAt`, `views`, `archived`, and `active: false`.
Active is false deliberately. Several fields are inferred from photos, reviews
or search results, and those should get a human pass before going live.

## Experiences and organizers

Both use the same pattern as restaurants: pull the source, enrich what is
missing from search, classify with an LLM, validate against the schema.

Experiences fill every required field except `owner`, which is not a data
problem. `Experience.owner` is a Mongo id, so the Organizer or Restaurant it
points at has to exist first. Each record carries an `ownerRef` block with the
provider and external id to resolve against after that import.

Tix's public discovery feed is a thin projection with no description, end date
or organizer. The full record comes from `fetchEventBySlug`, which is what
makes description, end time and the owner reference available at all.

Organizers come from the Tix accounts behind those events. About two thirds
write a bio, which becomes the required `description`. For the rest it is
derived from search results, with the same instruction to return null rather
than describe a different business with the same name.

## Layout

```
scripts/          the three mapping scripts
discover/         Google Places, FlavorQueste, Tix clients
ig/               Instagram session, search, profile reads
pipeline/         menus, OCR, DeepSeek, MongoDB writes
config/settings   all env config
docs/             architecture and sample payloads
```

## Legal

Google Places, Serper, Gemini and DeepSeek are used through their APIs under
normal terms. FlavorQueste and Tix are read through public endpoints
their own web clients use.

Instagram is the exception. Both the logged out and logged in paths are
against Instagram's Terms of Service. Set `IG_FALLBACK_ENABLED=false` and pass
`--no-ig-profile` to run without them, at the cost of handles, WhatsApp and
grid photos.
