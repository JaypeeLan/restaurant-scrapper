# Experiences from Instagram

The main product calls these **experiences**. Ingest drafts a partial
`ExperienceType` from each qualifying caption.

The experience gate uses **caption + flyer OCR** (when an image is
available). A thin caption with a detailed flyer still qualifies.

## Name priority

1. **DeepSeek** (when `DEEPSEEK_API_KEY` is set) — refines name/schedule/prices from caption + flyer OCR
2. **On-image flyer / card text (OCR)** — primary heuristic
3. Event-like hashtags (`#ShiroSundayBrunch`)
4. Caption offering line (never a price tier)

## Setup extras

Flyer/card titles need OCR:

```bash
brew install tesseract
pip install pillow pytesseract
```

Optional DeepSeek refine (OpenAI-compatible):

```bash
# .env
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_ENABLED=true   # default on when key is set
```

Results are cached in `.cache/ocr/` and `.cache/deepseek/`.

`GET /api/events` stays fast by default: caption heuristics (+ any cached OCR).
Live flyer downloads / DeepSeek are opt-in:

```
GET /api/events?ocr_fetch=true&llm=true
```

On Render the disk cache is ephemeral, so leave these off for the dashboard.

## Deduping

Multiple IG posts for the same offering (promo, last call, reviews) collapse into
**one experience per handle + name**. Variants like `Dear Kaffy: …` and
`Dear Kaffy London` share a key. The merged draft keeps the richest fields and
lists contributing posts under `sourcePosts` / `postCount`.


## Field coverage

| Experience field | From IG? | Source |
|---|---|---|
| `name` | yes | **DeepSeek (if enabled)**, else flyer OCR, hashtag, caption |
| `slug` | yes | Derived from handle + name |
| `description` | yes | Full caption |
| `tags` | yes | `#hashtags` |
| `categories` | partial | Keyword → `ExperienceCategory` (Food, Drinks, Music, Theater, …) |
| `ageLimit` | rare | `18+`, `21 and above` |
| `dressCode` | rare | `dress code: …`, `smart casual` |
| `sourceType` | default | `Restaurant` (venue accounts); Organizer needs product link |
| `ownerName` | yes | IG profile display name / handle |
| `owner` | no | Needs product Organizer/Restaurant `_id` |
| `host` | partial | First `@mention` that is not the account |
| `appearances` | partial | Other `@mentions` (guest artists) |
| `website` | partial | URL in caption, else profile website |
| `emails` / `phones` | rare | Almost never in captions |
| `coverImage` / `imageUrl` | yes | Post `mediaUrl` (CDN expires) |
| `pricePoints[]` | partial | `₦40,000` etc. → `{ type, price }` |
| `location` | no | Needs venue address + coordinates from product DB |
| `schedule.eventType` | yes | `recurring` vs `one-time` |
| `schedule.recurrence.days` | yes | `Monday – Friday`, `every Sunday` → `DayOfWeek[]` |
| `schedule.startTime` / `endTime` | yes | `12:30 PM` → `12:30` (`HH:MM`) |
| `schedule.date` / `startDate` / `endDate` | partial | Free-text dates when present; ISO dates usually missing |
| `rating` | no | Product-only |
| `active` | yes | Default `true` for extracted drafts |
| `createdAt` / `updatedAt` | system | Set on write into the product |

## What still needs the product DB

- Link `owner` to a Restaurant or Organizer
- Fill `location` from the restaurant address
- Persist CDN images (IG URLs expire)
- Confirm/edit schedule dates and price point labels
- Choose `sourceType` Organizer when the host is not a venue

## API

`GET /api/events` returns drafts grouped by IG handle. Each item includes:

- `experience` — partial `ExperienceType`
- `filled` / `missing` — which keys were populated
- ingest provenance (`handle`, `permalink`, `score`, …)
