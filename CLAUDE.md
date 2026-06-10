# Megababe OOS Tracker

## What we're building

An automated weekly stock checker for ~154 Megababe SKUs across 10 retailers.
Output is a static dashboard listing **which products are out of stock and
where** — that's the primary thing the user wants to see — plus Slack alerts
when stock changes.

Five retailers are checked automatically. Five (Target, Walmart, ASOS,
Anthropologie, CVS) have serious anti-bot protection so the user manually
checks them and logs status in a Google Sheet that this tool reads.
Anthropologie and CVS were originally planned as automated but were moved to
the manual sheet on 2026-04-30 — recon found PerimeterX (Anthropologie) and
Akamai-style 403s (CVS) that default Playwright Chromium can't bypass, and
the manual route is cheaper than investing in stealth tooling.

Runs unattended via GitHub Actions. Zero hosting cost. No paid APIs.

## Stack

- **Python 3.11+**
- **httpx** for Shopify-style endpoints (Gee Beauty), JSON-LD-rich PDPs
  (Cult Beauty), Google Sheet fetch
- **Playwright** for retailers that need a real browser to load JSON-LD
  or to bypass simple Cloudflare (Goop, Nordstrom). Boots is currently a
  stub — its PDPs are Incapsula-blocked and bypass tooling hasn't been
  decided yet.
- **Google Sheets** as the manual data source for Target, Walmart, ASOS —
  read via the public "publish to web as CSV" URL, no API auth needed
- **SQLite** for state and history (single file in repo)
- **Static HTML dashboard** generated each run, deployed via GitHub Pages
- **GitHub Actions** for the weekly cron + Slack webhook notifications

## Data source breakdown

| Source | Retailers | SKU count |
|---|---|---|
| Shopify JSON (httpx) | Gee Beauty | 20 |
| httpx + JSON-LD | Cult Beauty | 23 |
| Playwright + JSON-LD | Nordstrom, Goop | 27 |
| Playwright (deferred — Incapsula stub) | Boots | 5 |
| Manual via Google Sheet | Target, Walmart, ASOS, Anthropologie, CVS | 79 |
| **Total** | **10 retailers** | **154** |

## Repo layout

```
oos-tracker/
├── CLAUDE.md                     # this file
├── RETAILER_KNOWLEDGE.md         # user's per-retailer OOS signals + brand pages
├── README.md
├── pyproject.toml                # uv or poetry
├── .env.example
├── .gitignore
├── products.csv                  # SKU list (provided)
├── src/
│   ├── checkers/
│   │   ├── __init__.py
│   │   ├── base.py               # Result enum + Checker protocol
│   │   ├── shopify.py            # Gee Beauty + Goop
│   │   ├── manual_sheet.py       # Google Sheet fetcher for Target/Walmart/ASOS
│   │   ├── nordstrom.py          # Playwright
│   │   ├── anthropologie.py
│   │   ├── cult_beauty.py
│   │   ├── boots.py
│   │   └── cvs.py
│   ├── db.py                     # SQLite schema + queries
│   ├── run_check.py              # orchestrator, the entry point
│   ├── build_dashboard.py        # generates docs/index.html
│   └── notify.py                 # Slack webhook
├── data.db                       # SQLite (committed so history persists)
├── docs/
│   └── index.html                # GitHub Pages dashboard
└── .github/workflows/
    └── weekly-check.yml          # cron: every Monday 9am ET
```

## Stock detection logic per retailer

The table below is the original spec. The authoritative source going
forward is **`RETAILER_KNOWLEDGE.md`** — the user maintains it with
hands-on observations of how each retailer signals OOS, brand-page
URLs, multi-variant PDPs, and known edge cases. **Every Playwright
checker MUST consult `RETAILER_KNOWLEDGE.md` for that retailer's
specific signals** before relying on the generic patterns below. When
the file says "log raw signals into `notes`", do that — the user wants
visibility into unexpected page states rather than silent guesses.

| Retailer | Approach | In-stock signal | OOS signal |
|---|---|---|---|
| Gee Beauty | append `.json` to URL (it's Shopify) | any `variants[].available == true` | all variants false |
| Goop | originally Shopify; **moved to Playwright** (PDP returns 403, see `RETAILER_KNOWLEDGE.md`) | "Add to bag" present | "Add to waitlist" replaces it |
| Anthropologie | Playwright | "Add to basket" enabled | button absent/disabled, or PDP missing from brand page |
| Nordstrom | Playwright + JSON-LD | "Add to bag" visible, no "sold out" | "sold out" present, or PDP missing from brand page |
| Cult Beauty | Playwright | "Add to basket" enabled | "Notify me when available", or per-size button disabled, or PDP missing from brand page |
| Boots | Playwright | "Add to basket" present | absent (no live OOS observed yet — log raw state) |
| CVS | Playwright | any of pickup / same-day-delivery / shipping | none (no live OOS observed yet — log raw state) |
| Target | **Manual** — read from Google Sheet | user marks in_stock | "Check stores" gray button, all fulfillment unavailable |
| Walmart | **Manual** | same | PDP no longer appears on brand page |
| ASOS | **Manual** | same | PDP no longer appears in `megababe` search results |

Every checker (including the manual sheet one) returns one of:
- `IN_STOCK`
- `OOS`
- `ERROR` (404, redirect to brand page, page broken, or marked error in sheet)
- `UNKNOWN` (page loaded but signal unclear, or sheet row missing/blank)

Note: many retailer ERRORs are actually OOS in disguise (the PDP got
delisted because the product is unavailable). The brand-page
reconciliation step (build step 10) downgrades these to OOS using the
brand page URLs in `RETAILER_KNOWLEDGE.md`.

## Manual Google Sheet — setup and format

The user maintains a Google Sheet with status for the 52 manual SKUs. Tool
reads it as a public CSV (no API auth needed).

**One-time setup the user does:**

1. In the existing "2026 Retail OOS" workbook, add a new tab called
   `Manual Status` (or use a separate sheet — either works).
2. Paste in the contents of `manual_status_template.csv` (provided
   alongside this brief).
3. File → Share → Publish to web → select the `Manual Status` tab →
   format **CSV** → "Publish".
4. Copy the resulting URL (looks like
   `https://docs.google.com/spreadsheets/d/e/[long-id]/pub?gid=0&single=true&output=csv`).
5. Put that URL in `.env` as `MANUAL_SHEET_URL` and in GitHub Actions
   secrets.

**Sheet column format:**

| retailer | product_name | status | last_checked | notes |
|---|---|---|---|---|
| Target | Thigh Rescue Mini | in_stock | 2026-04-28 | |
| Target | Thigh Rescue | oos | 2026-04-28 | back-order til May |
| Walmart | Bust Dust | in_stock | 2026-04-28 | |
| ... | ... | ... | ... | ... |

`product_name` must match `products.csv` exactly (case sensitive). Status
values: `in_stock`, `oos`, `error`, or blank/unknown.

**Behavior in `manual_sheet.py`:**

- Fetch URL on each run, parse CSV
- Build a `{(retailer, product_name): row}` lookup
- For each manual SKU, look up the row:
  - Row exists with valid status → return that status
  - Row exists but blank status → return `UNKNOWN`
  - Row missing entirely → return `UNKNOWN` with note "not in manual sheet"
- Pass through `last_checked` as the timestamp instead of "now" — this way
  the dashboard accurately shows when the user last verified
- If the URL fetch fails, fall back to the most recent known status from
  the database (don't fail the whole run)

## Database schema

```sql
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    product_name TEXT NOT NULL,
    url TEXT NOT NULL,
    url_quality TEXT NOT NULL,  -- pdp | brand_page | category_page | search_page
    source TEXT NOT NULL,        -- automated | manual
    UNIQUE(retailer, product_name)
);

CREATE TABLE checks (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    status TEXT NOT NULL,        -- IN_STOCK | OOS | ERROR | UNKNOWN
    checked_at TIMESTAMP NOT NULL,
    notes TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX idx_checks_product_time ON checks(product_id, checked_at DESC);
```

`source` is derived from retailer (Target/Walmart/ASOS = manual, rest =
automated) and seeded on first run.

## Dashboard requirements

The user's #1 ask: **see what's OOS and where**. Build the dashboard around
that, not around a "everything is green" grid.

Sections, in order of importance:

1. **Header** — last-run timestamp, total SKUs, current OOS count
2. **Currently out of stock** — grouped by product name, showing which
   retailers it's OOS at:
   ```
   Rosy Pits — out at 2 retailers
     Target              · went OOS Mon Jun 3   (manual)
     Cult Beauty         · went OOS 3 weeks ago (automated)
   ```
3. **Stale manual entries** — manual rows where `last_checked` is more
   than 10 days old, so user knows what to refresh in the sheet
4. **URLs to fix** — products with `url_quality != 'pdp'`. These
   automated checks will never report accurate stock. Surface them so
   the user can update `products.csv`.
5. **Recent changes (last 4 weeks)** — went OOS / back in stock
   transitions, most recent first
6. **Per-retailer summary table** — small, at the bottom

Visual: clean, minimal, no JS framework. Just inline CSS. Should look
fine on mobile. Output to `docs/index.html`.

Subtle indicator on each row showing whether the source was automated or
manual — small text or icon, not a big badge.

## Slack alerts

After each run, send one summary message to the Slack webhook with:
- Count of new OOS this week
- Count of new back-in-stock this week
- Count of new errors
- Bullet list of each change (product · retailer · transition)

Skip the message entirely if nothing changed.

## GitHub Actions workflow

Weekly cron, Mondays 9am ET:

```yaml
on:
  schedule:
    - cron: '0 14 * * 1'  # 9am ET = 14:00 UTC
  workflow_dispatch:       # allow manual trigger
```

Steps:
1. Checkout
2. Install Python + Playwright browsers
3. Run `python -m src.run_check`
4. Run `python -m src.build_dashboard`
5. Commit `data.db` and `docs/index.html` back to main
6. (GitHub Pages auto-deploys from `docs/`)

Secrets needed in repo settings:
- `MANUAL_SHEET_URL`
- `SLACK_WEBHOOK_URL` (optional)

## Known data quality issues

19 of the 175 URLs in products.csv don't point to real product pages —
they go to brand category pages, search results, or general brand listings.
These will always read as "in stock" because the page loads fine.

The checkers should detect this where possible (URL pattern match —
`/brands/megababe`, `/c/brand/megababe`, `/search/`) and return `ERROR`
with a note. The dashboard surfaces them as "URLs to fix" so the user
can update `products.csv`.

The bad URLs are pre-marked in `products.csv` via the `url_quality`
column — anything other than `pdp` needs attention. Skip the check
entirely for these, return `ERROR` directly.

## Build order

Don't try to build everything at once. Order of operations:

1. **Scaffold the project** — `pyproject.toml`, `src/` layout,
   `.env.example`, `.gitignore`. Use `uv` if available, otherwise
   `pip` + venv.
2. **DB layer** — schema, seeding from `products.csv`, basic insert/query
   helpers. Verify with a one-off script.
3. **Base checker protocol** — `Checker` class returning `Result` enum.
4. **Shopify checker first** (Gee Beauty + Goop) — easiest, gives
   immediate working signal. ~22 SKUs handled with no setup.
5. **Manual sheet checker** — also easy, no browser automation. Fetches
   the published Google Sheet CSV. After this, 74 SKUs working.
6. **One Playwright checker** (Cult Beauty is a good pick) — get the
   browser automation working end-to-end before doing the rest.
   Consult `RETAILER_KNOWLEDGE.md` for the per-retailer signals; don't
   rely on the generic table alone.
7. **Remaining Playwright checkers** — Anthropologie, Nordstrom, Beauty
   Bay, Boots, CVS, plus Goop (moved here from Shopify). Same rule:
   each one starts by re-reading the relevant section of
   `RETAILER_KNOWLEDGE.md`. Log raw signals (button text, presence,
   variant state) into the `notes` field so unexpected page states
   stay debuggable.
8. **Orchestrator** — `run_check.py` loops products, dispatches to the
   right checker, writes results.
9. **Dashboard generator** — static HTML, OOS-first layout.
10. **Brand-page reconciliation** — a second pass that runs after PDP
    checks. For each retailer, scrape the brand page URL listed in
    `RETAILER_KNOWLEDGE.md` (Playwright for retailers that already use
    it, `/collections/megababe/products.json` for Shopify), then:
    a. Downgrade this run's `ERROR` results to `OOS` with note
       `reconciled-via-brand-page`. (Per the user, a missing PDP is
       overwhelmingly an OOS signal, not delisting.)
    b. Diff the brand-page product list against `products.csv` for that
       retailer; insert any new names into a new `new_products` table
       (schema in `RETAILER_KNOWLEDGE.md`) for the user to triage.
    Adds two dashboard sections: "New products detected" (above "URLs
    to fix") and a reconciled-OOS indicator inside "Currently out of
    stock". Name matching is substring + case-insensitive +
    accent-insensitive; ambiguous matches go to human review rather
    than auto-resolving — see `RETAILER_KNOWLEDGE.md` for the matching
    rules and edge cases (Mini vs full-size, etc.).
11. **Slack notifier**.
12. **GitHub Actions workflow**. Test with `workflow_dispatch` manual
    run before trusting the cron.

After step 5 you have something genuinely useful — Shopify retailers
plus all manual entries flowing into a system. Don't block on Playwright.

## Things to ask me before assuming

- Do I have a Slack workspace + webhook access? (If no, swap to email
  via `smtplib` + a Gmail app password.)
- Should the dashboard be public via GitHub Pages, or private? (Private
  options: keep the repo private and view `docs/index.html` via GitHub's
  raw file view, or use a password-protected hosting service.)
- Do I want to track per-variant stock for retailers with size variants
  (Cult Beauty), or treat each PDP as one row?

## Non-goals (don't build these unless I ask)

- Login-protected scraping
- Real-time monitoring
- A full webapp / admin UI
- Authentication on the dashboard
- Email digests beyond the Slack alert
- Automated scraping of Target / Walmart / ASOS (deliberately manual)
