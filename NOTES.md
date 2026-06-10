# Where we left off — 2026-04-30 (step 10 brand-page reconciliation shipped)

## Build order progress
Steps 1–5 + 8–9 of the (now) 12-step plan in CLAUDE.md are done. Plan
was renumbered on 2026-04-30 to insert step 10, brand-page
reconciliation. Resume at step 6 (Playwright), step 10 (reconciliation),
or step 11 (Slack) — see "Next time" below.

- [x] **1. Scaffold** — `pyproject.toml`, `.env.example`, `.gitignore`, `src/`
- [x] **2. DB layer** — `src/db.py`. Schema, idempotent migration, seed
      from `products.csv`. `data.db` is populated with 175 products
      (123 automated + 52 manual).
- [x] **3. Base checker protocol** — `src/checkers/base.py`. `Status`
      enum, `Product` dataclass with `from_row`, `CheckResult`,
      `Checker` Protocol.
- [x] **4. Shopify checker** — `src/checkers/shopify.py`. Uses the
      storefront `.js` endpoint (not `.json`). Live-verified against
      all 20 Gee Beauty PDPs; returns real IN_STOCK/OOS.
- [x] **5. Manual sheet checker** — `src/checkers/manual_sheet.py`.
      Live-wired on 2026-04-30 against the published Google Sheet
      (`/gviz/tq?tqx=out:csv&gid=...` URL). Schema: `Retailer`,
      `Product Name`, `Stock status`. Status map: "in stock"→IN_STOCK,
      "out of stock"→OOS, blank→UNKNOWN, anything else→ERROR with raw
      value preserved in notes. Match by exact `(retailer.strip(),
      product_name.strip())`. Run timestamp is used as `checked_at`
      (last_checked column from the sheet is no longer read). Sheet
      fetch failure falls back to the most recent DB status per
      product. Reconciliation method `ManualSheet.reconcile(products)`
      returns `unmatched_sheet` / `unmatched_csv` lists; orchestrator
      prints them at the start of each run.
- [x] **6. First retailer checker (Cult Beauty) — shipped as httpx,
      not Playwright.** Recon (2026-04-30) showed Cult Beauty PDPs are
      server-rendered with schema.org JSON-LD `availability` plus
      redundant `data-stock` attributes; plain httpx works without any
      anti-bot challenge. Implementation in `src/checkers/cult_beauty.py`
      mirrors `shopify.py` shape. Stock detection: JSON-LD primary,
      `data-stock` cross-check, ERROR if they disagree.
      Multi-variant PDPs (Thigh Rescue / Rosy Pits / Smoothie Deo —
      6 CSV rows total) need the variant SKU in the `variant_match`
      column; first run after wiring populated all 6.
      Orchestrator gained an `HTTPX_RETAILERS` tuple (currently just
      "Cult Beauty") sitting alongside Shopify and the manual sheet —
      future bespoke httpx checkers go there.
- [x] **7. Remaining checkers — partially complete.** Status as of
      2026-04-30:
      - **Goop:** shipped (Playwright + JSON-LD; CF-protected).
      - **Nordstrom:** shipped (Playwright + JSON-LD AggregateOffer;
        SPA needs hydration; intermittent bot-detection redirect to
        `siteclosed.nordstrom.com`).
      - **Boots:** deferred — stub returns ERROR with explanatory note.
        PDPs are Incapsula-blocked, brand-page tiles don't expose
        stock state. See "Boots deferral" below.
      - **Anthropologie + CVS:** moved to manual Google Sheet flow
        (PerimeterX and Akamai blocks; bypass tooling not justified at
        weekly cadence). Now 5 manual retailers total.
      Recon-first principle held up: Cult Beauty/Goop transports
      (httpx vs Playwright) were both surprises, and the latest recon
      surfaced that Nordstrom needed Playwright but Boots was
      effectively unreachable.
- [x] **8. Orchestrator** — `src/run_check.py`. Loads .env, dispatches
      by `(url_quality, source, retailer)`. Writes one `checks` row per
      checked product. `url_quality != 'pdp'` becomes ERROR; Playwright
      retailers (and manual when `MANUAL_SHEET_URL` is unset) are
      skipped without a row. Now also prints the manual-sheet
      reconciliation diff at the start of each run.
      Run history:
      - 2026-04-30 (Shopify only): 11 IN_STOCK, 9 OOS, 20 ERROR, 135 skipped → 175 ✓.
      - 2026-04-30 (Shopify + manual, first sheet load): 41 IN_STOCK,
        24 OOS, 20 ERROR, 5 UNKNOWN, 85 skipped → 175 ✓. 5 UNKNOWNs
        were name mismatches between sheet and CSV.
      - 2026-04-30 (after CSV reconciliation, +3/−2): 48 IN_STOCK,
        24 OOS, 19 ERROR, 0 UNKNOWN, 85 skipped → 176 ✓. ERROR
        dropped by 1 because Walmart Toe Deo's `url_quality=brand_page`
        row was removed.
      - 2026-04-30 (after Beauty Bay dropped, −21): 48 IN_STOCK,
        24 OOS, 11 ERROR, 0 UNKNOWN, 72 skipped → 155 ✓. Of the 21
        Beauty Bay rows, 8 were `url_quality=brand_page` ERROR (gone)
        and 13 were Playwright-skipped (gone).
      - 2026-04-30 (Cult Beauty checker shipped): 68 IN_STOCK,
        26 OOS, 11 ERROR, 0 UNKNOWN, 50 skipped → 155 ✓. Cult
        Beauty contributed +20 IN_STOCK / +2 OOS / −22 skipped.
        The 2 OOS hits (Space Bar, Coco Deo) match the recon-confirmed
        OOS PDPs. JSON-LD and `data-stock` agreed on all 22 Cult Beauty
        rows checked — no ERROR fallthroughs.
      - 2026-04-30 (Goop checker shipped + Green Deo URL fix): 69
        IN_STOCK, 27 OOS, 9 ERROR, 0 UNKNOWN, 50 skipped → 155 ✓.
        Goop's 2 PDPs both produce real states now (Thigh Rescue OOS,
        Green Deo IN_STOCK), erasing the 2 prior Goop ERRORs (one
        from the Shopify-checker 403, one from the
        `url_quality=category_page` row that was actually fixable).
        JSON-LD and waitlist-button visibility agreed on both checks.
      - 2026-04-30 (Nordstrom shipped, Boots stubbed, manual flow
        expanded to Anthro+CVS, Clean Pit Routine removed): final
        clean verification run produced **108 IN_STOCK, 29 OOS,
        13 ERROR, 4 UNKNOWN, 0 skipped → 154 ✓**. Wall-clock 225s
        (3m45s). All 24 reachable Nordstrom PDPs succeeded —
        bot-detection failure rate was 0% this run (vs 4–17% in
        earlier dev runs); see flakiness note below. No JSON-LD ↔
        Add-to-Bag disagreements. Compared to the Goop-shipped run:
        −1 row (Clean Pit Routine), Nordstrom contributed +24
        IN_STOCK, Anthropologie/CVS now produce real status from the
        sheet (+15 IN_STOCK, +1 OOS, +4 UNKNOWN from name
        mismatches), Boots stub contributes 5 ERROR. ERROR distribution:
        ASOS 1, Anthropologie 5, Boots 5, Cult Beauty 1, Nordstrom 1
        (all url_quality fixes or the Boots-deferred stub — none from
        Nordstrom flakiness this run).
- [x] **9. Dashboard generator** — `src/build_dashboard.py`. Writes
      `docs/index.html`. Sections, in order: header, currently OOS
      (grouped by product name), stale manual entries (>10d),
      URLs to fix, recent changes (28d window), per-retailer summary.
      Inline CSS, mobile-friendly. Counts in the summary table
      reconcile to total. **2026-04-30: added a "By Retailer" view**
      alongside the original "By SKU" — see "By-Retailer dashboard
      view" below.
- [x] **10. Brand-page reconciliation — shipped 2026-04-30.** Second
      pass that runs after the per-PDP loop while the shared Playwright
      browser is still alive. Implementation in `src/brand_pages.py`.
      Logic: scrape each retailer's brand page (URLs in
      `RETAILER_KNOWLEDGE.md`), and for every `ERROR` row in this run,
      check whether the product still appears on the brand page;
      downgrade to `OOS` with `notes="reconciled-via-brand-page; PDP
      not found"` if not. Tiles on the brand page that don't match any
      `products.csv` row land in a new `new_products` table for user
      triage. Dashboard renders "New products detected" above
      "URLs to fix" in the by-SKU view.
      See "Brand-page reconciliation outcome" section below for what
      worked and what didn't on the first live run.
- [ ] 11. Slack notifier
- [ ] 12. GitHub Actions workflow

## Things added that aren't in the original brief

### `variant_match` column on products + sheet
The brief said "treat each PDP as one row" for Shopify. In practice,
Gee Beauty has 3 products (Rosy Pits, Smoothie Deo, Thigh Rescue) where
the full-size and Mini share one Shopify product handle but appear as
separate rows in `products.csv`. Without per-variant logic, we were
silently misreporting two of them as IN_STOCK when the full size was
actually OOS.

Fix shipped: added a nullable `variant_match` column to both
`products.csv` and the `products` table. The Shopify checker, when
`variant_match` is set, looks up only the matching variant's
`available` flag. When blank, it falls back to "any variant available"
(brief's original behavior).

**For future product additions:** if a new row shares a Shopify product
handle with another row (i.e. it's a size/style variant of the same
underlying product), set `variant_match` to the variant's title
(e.g. "Original", "Mini") matching what Shopify returns. Otherwise
leave blank.

The 6 currently-populated rows are all Gee Beauty.

### Goop quirk
Goop's "Thigh Rescue" PDP is documented as Shopify in the brief, but
the URL pattern (`goop.com/<slug>/p/`) isn't standard Shopify
storefront routing and the site returns 403 to httpx regardless of
headers. **Resolved 2026-04-30** — Goop moved to its own Playwright
checker (`src/checkers/goop.py`) reusing `_json_ld.py`.

### Nordstrom — intermittent bot detection (~83–96% per-run success)

`NordstromChecker` works for the majority of PDPs but produces 1–4
ERROR rows per run from intermittent bot-detection redirects. Pattern
observed in dev runs on 2026-04-30:
- Some PDPs silently redirect to `siteclosed.nordstrom.com/invitation.html`
  (a Nordstrom geo-block / bot-detection fallback page that has no
  product content and no JSON-LD).
- Apres Shave consistently failed in 3 of 3 captured runs; other
  products (Dry Guy, Dust Puff, Thigh Rescue, Toe Deo, Ball Deo, Body
  Dust, Blade Bar, Bust Dust) flicker in and out.
- Per-run useful success rates seen: 83%, 96%, 83% (out of 24
  Nordstrom PDPs that aren't `url_quality=brand_page`).

**Mitigations shipped:**
1. Shared Playwright runtime + browser across checkers (Playwright's
   sync API forbids two `sync_playwright()` instances per thread —
   GoopChecker and NordstromChecker both accept an optional `browser`
   parameter; orchestrator launches one and passes it in).
2. `wait_for_function` for the Product-specific JSON-LD block (15s
   timeout) — got us from ~83% to ~96% in Run B of the diagnostic.
3. Explicit `siteclosed.nordstrom.com` URL detection that returns
   ERROR with `"Nordstrom redirected to siteclosed/invitation page
   (bot detection); flaky — usually clears on next run"`. Makes the
   real failure mode visible instead of misleading
   "no Product/ProductGroup JSON-LD" notes.
4. (Tried and reverted) A retry loop reading `page.content()` 3 times
   with 3s gaps — didn't help, just slowed things down.

**Acceptable for v1.** Over the weekly cron, every product gets ~52
checks per year — a 90% success rate means each product gets a real
read >47 weeks per year. Failures show up as transient ERROR entries
in the dashboard and self-correct on the next run.

**Future options if it becomes painful:**
- Stealth Playwright: Patchright (Python fork with stealth patches) or
  `playwright-extra` + stealth plugin. Patchright is pip-installable
  and a near-drop-in replacement.
- Real Chrome: `pw.chromium.launch(channel="chrome")` uses the system
  Chrome binary instead of Chromium-Headless-Shell — sometimes enough.
- Residential proxies (Bright Data etc.) — paid.
- Move Nordstrom to manual sheet if all of the above are too much.

### Boots — deferred (Incapsula on PDPs, brand page useless for stock)

`BootsChecker` is a stub at `src/checkers/boots.py` that always returns
ERROR with note `"Boots PDPs Incapsula-blocked; deferred (see
RETAILER_KNOWLEDGE.md)"`. Why:
- **PDPs**: Incapsula 403s default Playwright Chromium with a "Pardon
  Our Interruption" challenge page. Body is ~887 bytes — pure
  challenge.
- **Brand page**: loads cleanly, lists the 5 Megababe SKUs, but tile
  text **does not expose stock state**. The "out of stock" strings on
  the brand page are all in non-tile chrome (modal title, the "Hide
  out of stock items" filter toggle which defaults OFF, state JSON).
  So tile presence = "still carried" but ≠ "in stock".

**Acceptable for v1.** The user reports Boots has never been observed
OOS in practice. Five SKUs all reading ERROR is annoying but visible
and doesn't mask anything.

**Future options if it becomes painful:**
- Stealth Playwright (same as Nordstrom — Patchright, etc.).
- Real Chrome via `channel="chrome"`.
- Fold Boots into the manual sheet flow (low cost — only 5 SKUs).

### Manual sheet — name reconciliation closed (2026-04-30)

The 13 mismatches from the first live run were resolved by editing
`products.csv` only. Second live run shows reconciliation diff = 0/0.

**Renames applied to `products.csv`** (UPDATEd in place in the DB so
each row's `product_id` and check history are preserved):
- `Target Apres Shave 3oz` → `Target Après Shave - 3 oz`
- `Target Apres Shave Mini 1.7oz` → `Target Après Shave Mini - 1.7 oz`
- `Walmart Apres Shave` → `Walmart Après Shave`

**Added to `products.csv`** (new Target SKUs surfaced by sheet diff):
- `Target Blister & Heel Rescue` — `/p/.../A-94849160`
- `Target Butt Stuff` — `/p/.../A-94959721`
- `Target Thigh Rescue Gel` — `/p/.../A-94845308`

**Removed from `products.csv`** (Walmart no longer carries):
- `Walmart Bidet Bar` — products row + 2 checks deleted
- `Walmart Toe Deo` — products row + 3 checks deleted (was the
  `url_quality=brand_page` URLs-to-fix row)

**Sheet-side fix the user did between runs:**
- `Target Nigh Rescue` typo corrected to `Night Rescue`. Reconciliation
  now matches.

**Total SKU count** went 175 → 176 (−2 +3). Note: the original spec
called for 173 ("after the 2 removals + 3 additions") which appears to
have been a typo for 176.

**Schema gap surfaced by this work:** `db.seed_products()` upserts but
doesn't delete rows missing from the CSV. Renames keyed on
`(retailer, product_name)` therefore can't UPDATE in place — they
create a new row and orphan the old one. This run was handled with a
one-off migration script (rename via UPDATE; deletes via two-step
DELETE checks/DELETE products because there's no `ON DELETE CASCADE`).
**Resolved 2026-04-30** — see "sync_from_csv() added" below.

### Brand-page reconciliation outcome — 2026-04-30

First live run produced these results:

**Reconciliation summary line:**
```
Brand-page reconciliation: 2 ERRORs downgraded to OOS, 5 new products detected, 2 ERRORs unchanged
  (11 ERROR rows skipped — no usable brand-page scraper or fetch failed)
  brand-page fetch failed for ASOS: fetch failed: The read operation timed out
  brand-page fetch failed for Anthropologie: PerimeterX challenge
  tiles seen per retailer: Cult Beauty=18, Gee Beauty=17, Goop=3, Nordstrom=25
```

**Successfully downgraded (2):**
- Cult Beauty / Le Tush — was `url_quality=category_page` ERROR; not on
  Cult Beauty's brand page → OOS
- Nordstrom / Chest-o Presto — was `url_quality=brand_page` ERROR; not
  on Nordstrom's brand page → OOS

Both DB rows verified by SELECT — `notes` column reads
`"reconciled-via-brand-page; PDP not found"`.

**Unchanged (2)** — products genuinely on the brand page, ERROR was a
real failure of some other kind (Nordstrom's intermittent
`siteclosed` redirect):
- Nordstrom / Coco Deo
- Nordstrom / Pro Deo

**Unreconcilable (11)** — couldn't fetch brand page or no scraper:
- Boots: 5 (`SKIP_RETAILERS` — brand page tiles don't expose stock state)
- Anthropologie: 5 (PerimeterX blocks the brand page exactly like the
  PDPs; same anti-bot tier that put Anthropologie on the manual sheet)
- ASOS: 1 (httpx fetch timed out — likely Akamai)

This is honest and expected behavior. Documented in the run output
under `fetch_errors` so failures don't pretend to be silent successes.

**Counter wiring.** The orchestrator captures `error_products` during
the main loop, calls `brand_pages.reconcile()` after the loop,
adjusts `counts["ERROR"] -= recon.downgraded` and
`counts["OOS"] += recon.downgraded`. The DB UPDATEs land in place via
`update_check(conn, product_id, checked_at, new_status, new_notes)`.

**Known issues — `new_products` false positives.**
The first live run's 5 detected new products were ALL false positives.
Three causes, ranked by ease of fix:

1. **Accent mismatches (3 of 5).** Brand page renders "Après Shave"
   while products.csv has "Apres Shave"; my normalizer kept Unicode
   accents intact, so substring matching failed. Affected tiles:
   Gee Beauty "Après Shave Oil", Nordstrom "Après Shave...".
2. **Goop tile extractor broken (1 of 5).** Goop's product cards
   render the title in an element my generic JS selector doesn't find;
   instead the closest text my extractor grabbed was "quickshop" (a
   button label).
3. **Semantic name divergence (2 of 5).** Nordstrom labels gift sets
   like "Megababe Deo Duo Best-Smelling Deodorant Set $28 Value
   (Nordstrom Exclusive)" while `products.csv` has "Deo Duo Gift Set".
   These genuinely refer to the same product but no normalization
   strategy will match them without semantic understanding. Same for
   "Raising the Bar".

**Pre-interrupt fixes left in code (unverified):**
- `_normalize_name` now applies Unicode NFD decomposition + drops
  combining marks, so "Après" → "Apres". Should fix the 3 accent-based
  false positives. Not re-verified before the user halted iteration.
- Goop removed from `PLAYWRIGHT_SCRAPERS` (the dict orchestrating
  Playwright-based brand-page scrapers). Goop's reconcile pass and
  new-product detection now no-op for Goop. With only 2 known Goop
  SKUs and rare assortment changes, the cost is low. Not re-verified
  before the user halted iteration.

The 2 Nordstrom semantic-divergence false positives remain. These are
real CSV-vs-brand-page name disagreements; resolving them requires
either renaming the CSV rows to match Nordstrom's marketing labels or
adding aliases. **Deferred to a future session per user request.**

The first run's 5 false-positive `new_products` rows were DELETEd
before the user halted iteration, so the table is empty for the next
clean run.

### By-Retailer dashboard view — 2026-04-30

Added a second view to `docs/index.html` alongside the original
SKU-grouped layout. Tabs at the top of the page toggle between them
(CSS-only — two hidden radio inputs + sibling-selector show/hide; no
JS). The "By SKU" tab is checked by default so existing behavior is
preserved.

**By Retailer view** — one block per retailer, sorted by current OOS
count descending so worst-stocked retailers surface first. Within each
block:
- Header line: total SKUs · in-stock count · OOS count · error /
  unknown / no-check (only when non-zero) · last-checked timestamp
- The OOS list rendered prominently (the actionable thing the user
  wants to see at a glance)
- A collapsible `<details>` element revealing all other products
  (ERROR / UNKNOWN / NO_CHECK first, then IN_STOCK), ordered to
  surface what needs attention.
- Empty state ("Nothing currently out of stock at X") for retailers
  with zero OOS.

**Data layer** — only `_gather()` was extended; the SQL queries are
unchanged. New per-retailer fields built from the same `latest_rows`
+ `went_oos_rows` data: `latest_checked_at`, `oos_products`,
`other_products`. New `by_retailer` list returned in the template
context, sorted `(-oos, retailer)`.

**Visual style** — kept consistent with the existing dashboard.
Reuses `.status-OOS` / `.status-IN_STOCK` / `.status-ERROR` /
`.status-UNKNOWN` color tokens; introduces `.status-NO_CHECK` (muted
italic) for products without any check yet.

Verified against the latest run (108/29/13/4/0 = 154 SKUs): retailer
order rendered as ASOS (10 OOS) → Gee Beauty (9) → Target (3) → Cult
Beauty (2) → Walmart (2) → Anthropologie (1) → Goop (1) → Boots (0)
→ CVS (0) → Nordstrom (0). OOS lists, count breakdowns, and
collapsible "other products" tables all render cleanly.

### Goop checker + JSON-LD helper extracted — 2026-04-30

Two changes shipped together:

**1. `src/checkers/_json_ld.py`** — shared schema.org availability
parser. Before this, `cult_beauty.py` had inlined `_LDJSON_RE`,
`_AVAIL_TO_STATUS`, `_find_product_node`, and `_resolve`. Goop needed
the same logic verbatim, so I extracted into a single
`parse_availability(html, variant_match) -> JsonLdResult` entry point.
Handles both `Product` (single-variant) and `ProductGroup`
(multi-variant). Tolerates both `http://schema.org/...` and
`https://schema.org/...` URLs (Cult Beauty uses https, Goop uses http).

`cult_beauty.py` was rewritten to call the helper. The retailer-specific
`_find_data_stock` cross-check stayed local (only Cult Beauty has
`data-stock` attributes). **Regression check:** all 23 Cult Beauty
rows produce identical statuses pre- and post-refactor (totals
68/26/11/0/50 → 68/26/11/0/50).

**2. `src/checkers/goop.py`** — first Playwright-based checker. Recon
showed `goop.com` returns 403 + Cloudflare interstitial to httpx on
every URL, but default headless Chromium passes through cleanly. The
checker:

- Lazily launches Playwright + Chromium on first `check()` call (cheap
  import; `close()` tears it all down). One browser context shared
  across all Goop checks in a single run.
- Parses JSON-LD via the shared helper. Goop's Megababe SKUs are all
  single-variant, so `variant_match` is unused.
- Cross-checks via `button:has-text('add to waitlist'):visible.count() > 0`.
  The "Add to Bag" button is in the DOM in both states with toggling
  visibility, but the lowercase "add to waitlist" button only renders
  visible on OOS PDPs. ERROR if JSON-LD and waitlist-visibility
  disagree.

**Routing change in `run_check.py`:**
- `SHOPIFY_RETAILERS` lost Goop (was `("Gee Beauty", "Goop")`, now just
  `("Gee Beauty",)`). The Shopify checker had been silently ERRORing
  on every Goop URL because of the 403; that contributed 1 of the 11
  ERRORs in the prior run.
- `PLAYWRIGHT_RETAILERS` added Goop (now Anthropologie, Nordstrom,
  Boots, CVS, Goop). Dispatch routes to a `playwright_checkers` dict
  before falling through to the "skipped, no checker yet" path — same
  pattern as the existing `httpx_checkers` dict for Cult Beauty.

**`products.csv` fix:** `Goop / Green Deo` row updated from the
brand-page URL (`url_quality=category_page`) to the actual PDP at
`https://goop.com/megababe-the-green-deo/p/` (`url_quality=pdp`). The
Thigh Rescue row was already pointing at its PDP. Recon had surfaced
this fixable URL.

**Why the ERROR count dropped by 2** (11 → 9): the only two Goop CSV
rows had been contributing ERRORs (one from the Shopify 403, one from
the bad URL). Both are now real reads.

### Cult Beauty checker — 2026-04-30

Shipped as `src/checkers/cult_beauty.py`. Surprises and design notes:

**Surprise: didn't need Playwright.** The original brief tagged Cult Beauty
as a Playwright retailer. Recon found that PDPs are server-rendered with
schema.org JSON-LD; plain httpx (the same pattern as the Shopify checker)
returns the full HTML with `availability` and `data-stock` markers in the
initial DOM. No Cloudflare, no JS hydration. ~10× faster than Playwright,
no Chromium dependency for this retailer.

**Architecture: new `HTTPX_RETAILERS` tuple in `run_check.py`.** Sitting
alongside `SHOPIFY_RETAILERS` (storefront `.js` endpoint, one shared
checker class) and `MANUAL_RETAILERS` (Google Sheet). Currently just
holds "Cult Beauty"; future per-retailer httpx checkers go here too.
`PLAYWRIGHT_RETAILERS` shrank from 5 → 4 (Cult Beauty removed).

**Variant SKUs populated in `products.csv`.** 6 rows got `variant_match`:
- Thigh Rescue 60g → 13798813, Mini 23g → 13798814
- Rosy Pits 75g → 13798816, Mini 28g → 13813425
- Smoothie Deo 75g → 13798817, Mini 28g → 13813427

The checker requires `variant_match` for any PDP whose JSON-LD is
`@type=ProductGroup` and ERRORs if missing. For single-variant PDPs
(`@type=Product`), `variant_match` is optional but sanity-checked
against the page's top-level `sku` if set.

**Cross-check pattern.** Read JSON-LD `availability`, then look up the
`data-stock` attribute on the button matching the same SKU. If JSON-LD
says InStock but `data-stock="false"` (or vice versa) → ERROR with
both values in `notes`. First run on 22 Cult Beauty rows had zero
disagreements, so the cross-check fired as a no-op throughout. Worth
keeping for the day Cult Beauty's caches drift.

**Brand page (for step 10) — text-marker only.** The Megababe brand
page on Cult Beauty exposes `CollectionPage` and `BreadcrumbList`
JSON-LD but no per-product structured data. Step 10 will need to scan
each tile's text for "Notify Me" (the OOS marker) — no clean
structured fallback. Documented in `RETAILER_KNOWLEDGE.md`.

### `sync_from_csv()` added — 2026-04-30

`db.py` now has `sync_from_csv(conn)` that reconciles the `products`
table to `products.csv` exactly: INSERT new rows, UPDATE rows where
any of (url, url_quality, source, variant_match) differs, DELETE rows
missing from the CSV along with their `checks` history (no
`ON DELETE CASCADE` in the schema, so the function does the two-step
delete itself). Idempotent — running with no CSV changes is a no-op.

Wired into `src/run_check.py`'s startup (after `init_schema`, before
`all_products`) so every orchestrator run reconciles automatically.
`src/db.py:main()` (the `python -m src.db` entry point) also uses it
now — the documented "verify everything still works" command stays
useful through CSV deletions instead of leaving orphans behind.

`seed_products()` is kept in the codebase but marked deprecated in its
docstring. No callers remain in this repo.

**Run on 2026-04-30 after wiring confirms:**
- `sync_from_csv: 0 inserted, 0 updated, 0 deleted, 0 orphan checks deleted` (DB + CSV already aligned from the prior migration)
- Totals unchanged: 48 IN_STOCK, 24 OOS, 11 ERROR, 0 UNKNOWN, 72 skipped → 155 ✓
- Reconciliation diff still 0/0 ✓

**Known limitation — renames lose history.** The upsert key is
`(retailer, product_name)`. When a product_name changes between CSV
edits, `sync_from_csv()` sees it as delete-of-old + insert-of-new and
the old `product_id`'s check history is dropped. If history must
survive a rename, do the rename via an explicit
`UPDATE products SET product_name=? WHERE retailer=? AND product_name=?`
*before* `sync_from_csv` runs. Worth wrapping that in a
`rename_product()` helper if it ever becomes a recurring need (so far
it's been one-off and ad-hoc, like the Apres Shave → Après Shave
edits earlier today).

### Beauty Bay dropped — 2026-04-30

Megababe is no longer carried at Beauty Bay. Removed:
- 21 rows from `products.csv`
- "Beauty Bay" from `PLAYWRIGHT_RETAILERS` in `src/run_check.py`
- All references from `CLAUDE.md` (stack section, data source
  breakdown table, repo layout, stock detection table, open question;
  also bumped overall counts: 11 retailers → 10, ~175 SKUs → ~155)
- 21 product rows + 40 historical check rows from `data.db` (one-off
  migration script; same schema gap as above bit us a second time).

**Not changed:**
- `RETAILER_KNOWLEDGE.md` — never had a Beauty Bay section, brand-page
  URL list, or anything else to remove.
- `src/checkers/beauty_bay.py` — never existed (only ever a planned
  file in CLAUDE.md's repo layout).
- The Google Sheet — sheet rows for Beauty Bay (if any) are silently
  ignored by `ManualSheet._parse` since Beauty Bay is not in
  `TRACKED_RETAILERS`. No edit needed.

**Updated source breakdown:**
- Shopify (Gee Beauty + Goop): 22 SKUs
- Playwright (Anthropologie, Nordstrom, Cult Beauty, Boots, CVS): 80 SKUs
- Manual (Target, Walmart, ASOS): 53 SKUs
- Total: 10 retailers, 155 SKUs

### `PARSE_DECLTYPES` removed from `db.py`
Step 9 caught a bug: `connect()` originally opened SQLite with
`detect_types=sqlite3.PARSE_DECLTYPES`, which made the dashboard's
`SELECT MAX(checked_at) FROM checks` blow up because the deprecated
default timestamp converter can't parse our ISO 8601 strings (it wants
`"YYYY-MM-DD HH:MM:SS"`). We always parse timestamps manually in
`_parse_dt`, so dropping the flag is the correct fix. Single-line
change to `src/db.py`.

## Open questions from the brief, still outstanding

These are listed under "Things to ask me before assuming" in CLAUDE.md
and need answers before steps 10–11:

1. **Slack webhook access** — yes/no? If no, swap to email via smtplib.
2. **Dashboard public or private?** — affects whether `docs/` is
   GitHub Pages or kept private behind GitHub auth.
3. **Per-variant tracking for Cult Beauty** — same question we just
   answered for Gee Beauty. Likely yes given how it went, but worth
   confirming when we get to step 6. (Beauty Bay used to share this
   question but the retailer was dropped on 2026-04-30.)

## Current file map
```
oos-tracker/
├── CLAUDE.md
├── NOTES.md                      # this file
├── .env.example
├── .gitignore
├── pyproject.toml
├── products.csv                  # has variant_match column
├── manual_status_template.csv
├── .env                          # MANUAL_SHEET_URL set (gitignored)
├── data.db                       # 154 products + accumulated check rows
├── docs/
│   └── index.html                # dashboard, regenerated each run
├── src/
│   ├── __init__.py
│   ├── db.py                     # + new_products schema + helpers
│   ├── run_check.py              # orchestrator (step 8) + recon hook (step 10)
│   ├── build_dashboard.py        # dashboard generator (step 9) + new-products section
│   ├── brand_pages.py            # brand-page scrapers + reconcile() (step 10)
│   └── checkers/
│       ├── __init__.py
│       ├── base.py
│       ├── _json_ld.py           # shared schema.org availability parser
│       ├── shopify.py            # Gee Beauty (httpx)
│       ├── manual_sheet.py       # Target/Walmart/ASOS/Anthropologie/CVS
│       ├── cult_beauty.py        # httpx + JSON-LD + data-stock
│       ├── goop.py               # Playwright + JSON-LD + waitlist visibility
│       ├── nordstrom.py          # Playwright + JSON-LD AggregateOffer
│       └── boots.py              # stub — returns ERROR (deferred)
└── .venv/                        # uv-managed
```

## How to verify everything still works tomorrow
```bash
# Re-seed DB (idempotent)
.venv/bin/python -m src.db

# Run the full orchestrator + dashboard
.venv/bin/python -m src.run_check
.venv/bin/python -m src.build_dashboard
open docs/index.html
```

## Next time — pick one
- **Step 11 (Slack notifier)** — small, easy. Still blocked on the
  question of whether the user has a Slack webhook available. If no,
  we swap to email.
- **Step 12 (GitHub Actions weekly cron)** — once step 11 is done.
- **`new_products` false-positive cleanup** — see "Known issues" in
  the brand-page reconciliation section. Three buckets: accent
  normalization (already added but unverified), Goop tile extractor
  (currently disabled — needs a custom selector or stays disabled),
  semantic name divergence (Nordstrom gift-set names — needs CSV
  renames or alias support).
- **Nordstrom flakiness fix** — try Patchright. Should drop the 4–17%
  failure rate. Optional; current rate is acceptable for a weekly
  cron.
- **Add a `rename_product()` helper** — `sync_from_csv` loses check
  history on renames. Low priority until renames become routine.

Open question round-up still pending: Slack vs email, dashboard
public/private, per-variant tracking on Cult Beauty (RETAILER_KNOWLEDGE.md
leans strongly toward "yes, per-variant", but worth confirming).

## Future ideas

### Cross-reference POs with OOS history (future)

Goal: link this OOS dashboard to the separate PO parser project to answer:
- For a given PO, which ordered items were OOS at the time? Which OOS items did the retailer skip?
- What SKUs does each retailer order most? Frequency, volume, trends.
- Across all small retailers, which SKUs move fastest?

Architecture: keep both projects separate, share a SQLite database. Build a third
"analysis" tool (notebook or dashboard) that joins PO data with OOS history.

Prerequisites before starting:
1. OOS tracker stable and producing real history (a few weeks of cron runs)
2. Define a canonical SKU ID list (one row per Megababe product; Minis are separate
   canonical SKUs). Map all retailer-specific product names to canonical IDs.
3. Add canonical_sku column to products.csv
4. PO parser updated to emit the same canonical SKU IDs

Don't start until OOS tracker is fully built and running unattended.
