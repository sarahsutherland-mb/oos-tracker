# Megababe OOS Tracker

Weekly out-of-stock check for 160 Megababe SKUs across 10 retailers. It runs
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

**Keep the Google Sheet current.** Four retailers (Target, Walmart, ASOS, CVS
— 59 SKUs) block automated checking hard enough that a person has to look.
You check them by hand and log what you see; the tracker reads your sheet.

The sheet is the "Manual Status" tab of the **2026 Retail OOS** workbook,
published to the web as CSV. Columns:

| retailer | product_name | status | last_checked | notes |
|---|---|---|---|---|
| Target | Thigh Rescue Mini | in_stock | 2026-09-22 | |
| Walmart | Bust Dust | oos | 2026-09-22 | back-order til Oct |

- `product_name` must match `products.csv` **exactly** (case sensitive).
- `status` is `in_stock`, `oos`, `error`, or blank (blank = unknown).
- `last_checked` is what the dashboard shows, so backdate honestly.

The dashboard's **Stale manual entries** section lists rows older than 10 days.
That list is your to-do.

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

A full run takes a few minutes — most of it Playwright on Goop and Nordstrom.

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
| Anthropologie | 25 | Brand page only. A product missing from it **is** the OOS signal |
| Nordstrom | 25 | Playwright + JSON-LD — **currently blocked, see below** |
| Cult Beauty | 23 | httpx + JSON-LD |
| Gee Beauty | 21 | One Shopify collection request |
| Goop | 2 | Playwright + JSON-LD |
| Boots | 5 | Not checked — reports `UNKNOWN` (see below) |
| Target / Walmart / ASOS / CVS | 59 | Your Google Sheet |

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
| All 25 Anthropologie SKUs missing from a run | Brand-page fetch was blocked. The run *skips* rather than marking them OOS — deliberate. One-off is fine; twice running needs a look |
| **All 25 Nordstrom SKUs ERROR** | Current known state as of 2026-09-23 — Nordstrom now redirects every PDP to its bot-detection page. Not a code bug and not fixable by slowing down; see Known gaps |
| 1–4 Nordstrom ERRORs in a run | The older, milder version of the same thing. Fine to ignore |
| Gee Beauty slow or 429 | Rate limit. The checker already throttles and backs off; it resolves itself |
| Boots always `UNKNOWN` | Correct. Boots PDPs are Incapsula-blocked and nobody has decided on a bypass. `UNKNOWN` is honest — it used to report `IN_STOCK`, which quietly laundered five unchecked SKUs into confirmed stock |
| A product shows OOS but the site says otherwise | Check `url_quality` in `products.csv`. Anything other than `pdp` never gets checked properly and is surfaced on the dashboard under **URLs to fix** |
| Everything at one retailer flips OOS at once | Suspect the scraper, not the retailer. The dashboard's reconciled-OOS marker tells you whether it came from the brand page |

**The failure mode to watch for** is silence, not noise. Anthropologie is the
only retailer where the brand page is the *sole* signal — no PDP fallback, no
sheet row. If its fetch fails, those 25 SKUs simply don't appear in the run.
There's a guard (`_MIN_AUTHORITATIVE_TILES`) that stops a thin scrape from
marking them all OOS, and a retry that handles the cold-start 403 the
PerimeterX edge throws at the first request of a session. Both are tested.

---

## Known gaps

- **Nordstrom is blocked (25 SKUs).** Re-probed 2026-09-23: 18/18 PDPs
  redirected to `siteclosed.nordstrom.com/invitation.html`. The redirect is
  client-side and fires *after* the page loads, so a request looks fine until
  it doesn't. Tested and ruled out: request throttling (5s gaps), a fresh
  browser context per product, and the pinned Chrome/124 user agent. This is
  the same "needs a real decision" tier as Anthropologie was in April —
  stealth tooling or a paid unblocker — not a quick fix. Until then these 25
  SKUs report ERROR and are honestly marked as such.
- **No automated checking for Target, Walmart, ASOS, CVS.** Re-probed
  2026-09-23: ASOS still times out, Anthropologie's technique doesn't transfer.
  Target is the most tractable — its brand page renders fine, but the tiles are
  client-side and the OOS signal is per-tile ("Check stores"), not absence.
- **Boots is unchecked** (5 SKUs), pending a decision on Incapsula bypass.
- **No price or discount tracking yet.**
- **`data.db` is committed to the repo.** That's how run history survives; it
  also means the weekly job pushes to `main`. Don't rebase away its commits.
