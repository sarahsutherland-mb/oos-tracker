# Megababe OOS Tracker

Weekly out-of-stock check for 161 Megababe SKUs across 10 retailers. It runs
itself every Monday, writes what it found to `data.db`, regenerates a static
dashboard at `docs/index.html`, and posts a Slack message when something moved.

- **Dashboard:** GitHub Pages, served from `docs/` on `main`.
- **Schedule:** Mondays, 14:00 UTC (`.github/workflows/weekly-check.yml`).
- **Cost:** none. No paid APIs, no hosting.

New here? Read this file, then `RETAILER_KNOWLEDGE.md` when a specific
retailer misbehaves. `CLAUDE.md` is the original build brief and `NOTES.md` is
a dated dev journal — both are history, not instructions.

---

## The one thing you have to do

**Keep the Google Sheet current.** Six retailers (Target, Walmart, ASOS, CVS,
Nordstrom, Anthropologie — 111 SKUs) block automated checking hard enough that
a person has to look. You check them by hand and log what you see; the tracker
reads your sheet. Update it before Monday's run.

The sheet is the **2026 Retail OOS** workbook, published to the web as CSV.
The tracker reads three columns and ignores the rest:

| Retailer | Product Name | Stock status |
|---|---|---|
| Walmart | Thigh Rescue | In Stock |
| Walmart | Bust Dust | Out of Stock |

- `Product Name` must match `products.csv` **exactly** — case and accents
  (Nordstrom's is "Apres Shave", Walmart's "Après Shave"). Mismatches are
  printed at the top of the run log under "Manual sheet reconciliation" and
  that product reports `UNKNOWN`.
- `Stock status` is `In Stock`, `Out of Stock`, or blank (= unknown). Any other
  text becomes `ERROR` with what you typed kept in the notes.
- Rows for other retailers (Cult Beauty, Gee Beauty, …) are ignored.

The sheet has no "last checked" date, so the tracker can't tell a row you
re-checked this week from one untouched since summer — whatever is in the
sheet on Monday is recorded as that week's status. For the same reason the
dashboard's **Stale manual entries** section won't catch a sheet you forgot to
update.

Anthropologie is quick to check despite its 25 rows: it removes out-of-stock
products from its brand page entirely, so anything missing from
[the brand page](https://www.anthropologie.com/brands/megababe) is OOS.

If the sheet is unreachable on a run, the tracker falls back to each product's
last known status rather than failing — so a broken sheet URL degrades quietly.
Check the run log if manual rows look frozen.

---

## Running it yourself

```bash
uv sync --extra dev                       # once
uv run playwright install chromium        # once

uv run python -m src.run_check            # check everything, write to data.db
uv run python -m src.build_dashboard      # regenerate docs/index.html
uv run pytest tests/ -q                   # tests (no network)
```

A full run takes a few minutes — most of it Playwright on Goop.

`.env` (copy `.env.example`):

| Variable | Needed? | What it does |
|---|---|---|
| `MANUAL_SHEET_URL` | yes | Published-CSV URL for the Manual Status tab |
| `SLACK_WEBHOOK_URL` | no | Where change alerts go. Unset = alerts print to the log instead |
| `DASHBOARD_URL` | no | Adds a dashboard link to the Slack message |

In GitHub, the first two are repo **secrets**; `DASHBOARD_URL` is a repo
**variable**. Settings → Secrets and variables → Actions.

**To trigger a run without waiting for Monday:** Actions tab → Weekly stock
check → Run workflow. Do this after any change; don't trust the cron blind.

---

## How a product's status is decided

Each check returns one of `IN_STOCK`, `OOS`, `ERROR` (page broken, 404,
redirected) or `UNKNOWN` (loaded, but the signal was unclear).

| Retailer | SKUs | How |
|---|---|---|
| Cult Beauty | 22 | httpx + JSON-LD |
| Gee Beauty | 21 | One Shopify collection request |
| Goop | 2 | Playwright + JSON-LD |
| Boots | 5 | Not checked — reports `UNKNOWN` (see below) |
| Target / Walmart / ASOS / CVS / Nordstrom / Anthropologie | 111 | Your Google Sheet |

### Discounts

Two separate signals, both on the dashboard under **Discounted now**:

- **Retailer marked down** — the retailer publishes a "was" price above the
  current one (Shopify's `compare_at_price` and equivalents). Unambiguous, but
  only some retailers publish it.
- **Cheaper than last run** — this run's price is below the last one recorded.
  Catches a retailer discounting quietly without flagging a sale, which is the
  one worth watching for MAP.

Prices are never compared across retailers: Gee Beauty quotes CAD, Cult Beauty
EUR, Goop USD. A product that changes currency is treated as
having no comparable previous price rather than as a huge markdown.

The second signal needs two priced runs behind a product, so it starts working
from the second weekly run after 2026-09-23 — before that there is nothing to
compare against and the section will only show retailer-advertised sales.

After the per-product checks, a **brand-page reconciliation** pass runs. It
rescrapes each retailer's brand page and does two things: relabels this run's
`ERROR` rows as `OOS` where the brand page explains the failure (a delisted PDP
is usually an out-of-stock product), and flags products on the brand page that
aren't in `products.csv` as **New products detected** for you to triage.

---

## When something breaks

**Read the run log first.** Actions → the failed run → `run-log` artifact. It
has a line per product with the raw signal the checker saw.

| Symptom | What it usually is |
|---|---|
| A manual product shows `UNKNOWN` | Its name in the sheet doesn't match `products.csv`. The top of the run log lists both sides of the mismatch |
| Goop shows `ERROR` | Cloudflare's bot check caught it that week. Usually clears on the next run |
| Gee Beauty slow or 429 | Rate limit. The checker already throttles and backs off; it resolves itself |
| Boots always `UNKNOWN` | Correct. Boots PDPs are Incapsula-blocked and nobody has decided on a bypass. `UNKNOWN` is honest — it used to report `IN_STOCK`, which quietly laundered five unchecked SKUs into confirmed stock |
| A product shows OOS but the site says otherwise | Check `url_quality` in `products.csv`. Anything other than `pdp` never gets checked properly and is surfaced on the dashboard under **URLs to fix** |
| Everything at one retailer flips OOS at once | Suspect the scraper, not the retailer. The dashboard's reconciled-OOS marker tells you whether it came from the brand page |

**The failure mode to watch for** is silence, not noise. Most of the tracker
is now your sheet, and a sheet nobody updated looks exactly like a week where
nothing changed.

---

## Known gaps

- **Nordstrom moved to the sheet on 2026-10-07 (27 SKUs).** It checked
  cleanly through 2026-07-23; by 2026-09-23 every PDP redirected to
  `siteclosed.nordstrom.com/invitation.html`. The redirect is client-side and
  fires *after* the page loads. Tested and ruled out: throttling (5s gaps), a
  fresh browser context per product, and the pinned Chrome/124 user agent.
  Automating it again needs stealth tooling or a paid unblocker.
  `checkers/nordstrom.py` and `brand_pages.scrape_nordstrom` are kept.
- **Anthropologie moved to the sheet on 2026-10-07 (25 SKUs).** Its brand page
  fetches fine from a laptop but 403s from GitHub's runners on every retry, so
  the weekly job never recorded it. Re-enabling it means running at least that
  part of the check from somewhere that isn't a GitHub runner — see
  `brand_pages.BRAND_PAGE_AUTHORITATIVE`.
- **No automated checking for Walmart, ASOS, CVS.** Re-probed 2026-09-23:
  ASOS still times out.
- **Target is on the sheet, with a working checker parked.**
  `checkers/target.py` reads Target's own stock API (one request, all SKUs;
  available online = in stock, per the user). It works from a laptop, but the
  API returns HTTP 435 (PerimeterX) to GitHub's runners — confirmed on a real
  run 2026-10-07 — so it isn't wired into the weekly job.
- **Boots is unchecked** (5 SKUs), pending a decision on Incapsula bypass.
- **Discount tracking covers 45 of 161 SKUs** — Gee Beauty (CAD), Cult Beauty
  (EUR) and Goop (USD), the retailers whose pages publish a price. The manual sheet has no price column and the blocked retailers have
  no readable page, so the rest stay unpriced.
- **`data.db` is committed to the repo.** That's how run history survives; it
  also means the weekly job pushes to `main`. Don't rebase away its commits.
