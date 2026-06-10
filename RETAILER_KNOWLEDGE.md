# Retailer-Specific Knowledge

User-provided knowledge about how each retailer signals OOS, what their brand
pages look like, and known edge cases. Reference this file when building or
debugging any retailer-specific checker.

## Brand page URLs (use for reconciliation step)

| Retailer | Brand page URL |
|---|---|
| Target | https://www.target.com/b/megababe/-/N-8sqxfZfwtfr?moveTo=product-list-grid |
| Walmart | https://www.walmart.com/c/brand/megababe |
| Gee Beauty | https://geebeauty.ca/collections/megababe |
| Anthropologie | https://www.anthropologie.com/brands/megababe |
| ASOS | https://www.asos.com/search/?q=megababe |
| Cult Beauty | https://www.cultbeauty.com/c/brands/megababe/shop-all/ |
| Goop | https://goop.com/megababe/c/?country=USA&sort=recommended |
| Nordstrom | https://www.nordstrom.com/brands/megababe--20023 |
| Boots | https://www.boots.com/megababe |
| CVS | https://www.cvs.com/search?searchTerm=Megababe |

## OOS detection per retailer

### Target (manual)
- Brand page must be filtered with "Include out of stock" toggle to show all products
- A "Check stores" gray button on a product means OOS — when it appears, pickup/delivery/shipping are typically all unavailable
- Status logged in user's manual Google Sheet

### Walmart (manual)
- A product is OOS when its PDP no longer appears on the brand page
- Brand page is the source of truth for which products Walmart still lists
- Status logged in user's manual Google Sheet

### Gee Beauty (Shopify, automated)
- "Notify me when available" button replaces "Add to cart" when OOS
- Per-variant: a single PDP may have Original in stock and Mini OOS (or vice versa)
- The Shopify `.json` endpoint returns `available: true/false` per variant — match
  the variant by size (e.g., 2.6oz = Original, 1.0oz = Mini)

### Anthropologie (MANUAL — PerimeterX block, sheet route adopted 2026-04-30)

Anthropologie was moved into the manual Google Sheet flow alongside
Target/Walmart/ASOS/CVS on 2026-04-30. Anti-bot bypass tooling
(stealth plugins, paid services) was deemed not worth the cost at
current cadence. The recon notes below are kept for the day we revisit.

---

**Recon findings (2026-04-30):**


**Recon performed 2026-04-30** — both httpx and default Playwright
Chromium are denied site-wide. PDPs and brand page return HTTP 403 with
title "Access to this page has been denied" and ~9 KB challenge body
containing the markers `captcha`, `press & hold`, `px-captcha` (the
PerimeterX/HUMAN bot-protection product). No content is reachable.

**Build options when we revisit:**
- **Defer + manual sheet:** add Anthropologie to `MANUAL_RETAILERS` and
  the Google Sheet, like Target/Walmart/ASOS. Zero engineering cost,
  consistent with how the other "anti-bot heavy" retailers are handled.
  Recommended starting point.
- **Stealth bypass:** try Patchright (Python Playwright fork with
  built-in stealth patches), `playwright-extra` + stealth plugin, or
  switching to `channel="chrome"` (real Chrome instead of Chromium).
  PerimeterX is moderately defeatable by these but no guarantee.
- **Paid bypass service:** ScrapingBee, Bright Data Web Unlocker,
  ScrapeOps. Recurring cost.

**OOS signal (confirmed by user):**
- Anthropologie **removes the PDP entirely** when a product is OOS — the
  URL redirects to or becomes the brand page. There is no "sold out" state
  on the PDP; the page simply disappears.
- Consequence for `products.csv`: when a product is OOS, the only URL
  available is the brand page. Rows with `url_quality=brand_page` for
  Anthropologie reflect this state and should be interpreted as OOS, not
  as "URL to fix". Do **not** surface these in the dashboard's "URLs to fix"
  section — they are correct placeholders for OOS products.
- To confirm in-stock: find the actual PDP URL and update `products.csv`
  with `url_quality=pdp`.

**Additional observed signals (pre-block, for when automated checking resumes):**
- Some PDPs list multiple variants as clickable swatches; a variant may
  show "Unavailable until [date]" or be greyed out while others remain
  available — treat the specific variant as OOS.
- "Add to basket" button enabled = in stock for the currently selected
  variant.
These are the right selectors to look for **once anti-bot is bypassed**;
the recon couldn't confirm them since no PDP loaded.

**Variant structure note from `products.csv`:** Anthropologie uses a
share-PDP-with-color-variants pattern for The Deo (Santal, Peachy, Coco
all point to `/megababe-the-deo-skincare-deodorant?color=<id>`). Once
we have access, expect a `ProductGroup` JSON-LD or per-color data.

### ASOS (manual today; reference rules for future automation)

ASOS is currently checked manually via the Google Sheet. The rules below
are reference material for when ASOS moves to an automated Playwright
checker. When that happens, run the brand-page check first (it's the
authoritative signal) and use the PDP signals as confirmation/fallback.

**PRIMARY signal — brand-page absence:**
- Brand page URL: https://www.asos.com/search/?q=megababe
- If a product in `products.csv` does **not** appear on this page,
  treat it as OOS regardless of PDP state. ASOS hides delisted /
  unavailable products from the brand search, so absence is the
  authoritative signal.
- This fits the existing step 10 brand-page reconciliation pattern —
  ASOS doesn't need a per-PDP fetch at all once reconciliation is in
  place; the brand-page diff handles it. (Until then: manual sheet.)

**SECONDARY signals — PDP state (confirmation / fallback):**

Observed on a real OOS PDP (Megababe Thigh Rescue Anti-Friction Stick —
Unscented, product code 135832343):

1. Red "OUT OF STOCK" text in the product info panel.
2. "NOTIFY ME" button replacing the add-to-bag button.

Either signal alone is sufficient to mark OOS. The button swap is more
structurally reliable than text styling — prefer it as the primary
PDP-level check.

**Status logged in user's manual Google Sheet** (until automation lands).

### Cult Beauty (originally tagged Playwright; recon shows httpx is sufficient)

**Recon performed 2026-04-30** against 5 PDPs (3 in-stock, 2 OOS) plus the
brand page. Plain httpx with a desktop-Chrome `User-Agent` returns HTTP 200
with the full server-rendered HTML — JSON-LD and `data-stock` attributes are
present on the initial DOM, so no browser, no JS hydration, no waiting. The
"Cult Beauty must be Playwright" assumption in the original brief was wrong;
Cult Beauty can ship as an httpx checker similar to the Shopify one (~10x
faster than Playwright, no Chromium dependency).

**Anti-bot:** none observed across the 6 requests in recon. No Cloudflare
challenge, no CAPTCHA, no rate limiting hint. (One spurious "cloudflare"
string match in the rendered HTML turned out to be a CDN reference inside a
JS bundle, not an interstitial.) Re-verify if we ever start seeing 403s.

**Stock signals — three independent, redundant options.** Use (a) as
primary, (b) as confirmation, ignore (c).

(a) **JSON-LD schema.org availability** — cleanest, in `<script
type="application/ld+json">` near the top of `<body>`:
- Single-variant product → `{"@type": "Product", "sku": "<id>", "offers":
  {"availability": "https://schema.org/InStock"}}`
- Multi-variant product → `{"@type": "ProductGroup", "hasVariant": [
  {"@type": "Product", "sku": "<id>", "offers": {"availability": "...",
  "url": ".../?variation=<id>"}}, ... ]}`
- Values observed: `https://schema.org/InStock` and
  `https://schema.org/OutOfStock`. Other schema.org values (BackOrder,
  PreOrder, Discontinued) are theoretically possible — treat unknown as
  UNKNOWN with the raw value in `notes` for investigation.

(b) **`data-stock="true"|"false"` HTML attribute** — present on the main
add-to-basket button AND on each size-variant button. Flips cleanly between
in-stock and OOS PDPs in recon. Useful as a sanity check against (a).

(c) **Visible button text** — `Add to basket` / `Notify Me When Available` /
`Join the Waitlist` strings appear in the HTML regardless of stock state
(the page renders all three template options and toggles visibility via
CSS). **Do not use text presence as a signal**; either parse the live
visibility (Playwright-only) or rely on (a)/(b).

**Multi-variant PDPs:**
- Multiple sizes share one PDP URL like
  `/p/megababe-thigh-rescue-various-sizes/13798812/`. A `?variation=<sku>`
  query parameter selects a specific variant; the JSON-LD lists all
  variants regardless.
- Size buttons live in `<ul class="product-variations-size">` with each
  `<button class="size-variations">` carrying `data-sku`, `data-stock`,
  `data-size`, `data-choice`, `aria-selected`. The `data-sku` matches the
  variant's `hasVariant[].sku` in the JSON-LD.
- **Per-variant matching strategy:** store the variant SKU in
  `products.csv`'s existing `variant_match` column (e.g. `13798813` for
  `Thigh Rescue 60g`, `13798814` for `Thigh Rescue Mini 23g`). The checker
  finds the matching `hasVariant[]` entry by SKU; falls back to top-level
  `availability` if `variant_match` is blank and the JSON-LD is a single
  Product.

**Brand page** at `https://www.cultbeauty.com/c/brands/megababe/shop-all/`:
- 19 product tiles in recon (vs 23 rows in `products.csv` — the 4-row gap
  is the 3 multi-size families that share PDPs and 1 product whose CSV row
  is a category-page URL, not a real PDP).
- Tiles for OOS products contain a "Notify Me" string in the tile text
  (saw it on Space Bar and Coco Deo); in-stock tiles do not. Brand-page
  scraping for step 10 reconciliation should look for this marker on each
  tile, not just for tile presence.
- The brand page itself does **not** expose product-level JSON-LD — only
  a `CollectionPage` and `BreadcrumbList`. So step 10 has to use the
  text-marker approach above; there's no structured fallback.
- Brand page also revealed that Cult Beauty currently has 19 distinct
  Megababe SKUs visible. If any new tile appears that doesn't match a row
  in `products.csv` (after substring matching), that's a new-product
  candidate for step 10's `new_products` table.

**Confirmed OOS examples on 2026-04-30** (use as test fixtures if/when
re-verifying signals): Space Bar 99g (`/13798836/`), Coco Deo 75g
(`/16425214/`). Both showed `availability: OutOfStock` and `data-stock="false"`.

**Recon artifacts:** raw HTML for all probed URLs is in `recon/*.html`
(gitignored — delete after the checker is built and tested).

### Goop (Playwright, automated — Cloudflare-protected)

**Recon performed 2026-04-30** against the brand page + 2 PDPs (Thigh
Rescue, Green Deo). Unlike Cult Beauty, Goop **genuinely needs Playwright**
— httpx is blocked at the network edge.

**Anti-bot — site-wide Cloudflare 403 to httpx.** Every URL on goop.com
(homepage, brand page, PDP, the legacy `/products/<handle>.json`
Shopify endpoint) returns HTTP 403 with a "Just a moment…" interstitial
to plain httpx, regardless of User-Agent. **Default headless Chromium
via Playwright passes through cleanly** (CF "passed", no CAPTCHA, no
interactive challenge). Note that this could tighten at any time — if
Playwright starts hitting visible challenges, recovery options in order
of cost: longer post-load waits, slower navigation, persistent contexts
with cookies, residential proxies, paid CF-bypass libraries. We don't
need any of that today.

**Stock signals** (priority order):

(a) **JSON-LD `<script type="application/ld+json">` schema.org `Product`
availability** — primary, server-rendered, mirrors the Cult Beauty
pattern. Goop uses the `http://` schema URL (not `https://`); the
existing `_AVAIL_TO_STATUS` map in `cult_beauty.py` already tolerates
both. Values observed: `http://schema.org/InStock`,
`http://schema.org/OutOfStock`. SKU format is Goop-internal
(e.g., `12282-7544`, `MEGA-greendeoNo Color`) — not a standard Shopify
variant ID.

(b) **Visible "add to waitlist" button** — confirmation signal when OOS.
Only renders/visible in the OOS state. The "Add to Bag" button stays in
DOM in both states but its visibility + `disabled` attribute flip
depending on stock. Cleanest visibility check: lowercase
`button:has-text("add to waitlist"):visible`.

(c) **No `data-stock` / `data-sku` attributes on buttons** — different
DOM design from Cult Beauty. No structured cross-check at the button
level.

**Variant structure:** All Megababe Goop PDPs observed are
`@type: Product` (single-variant). No `ProductGroup`s. So no
`variant_match` needed in `products.csv`.

**Brand page** at `https://goop.com/megababe/c/?country=USA&sort=recommended`:
- Only 2 product tiles (Green Deo + Thigh Rescue) — matches `products.csv`.
- Brand page JSON-LD exposes only `@type: ItemList` (no product-level
  structured data).
- **Tiles do not show OOS state.** No "sold out" badge, no
  struck-through prices, no waitlist tag in tile text — both tiles
  read identically as "quickshop". So brand-page reconciliation
  (step 10) can't infer OOS from tile state for Goop; we'd have to
  fall through to per-PDP probing. For Goop's 2 SKUs that's fine.

**Confirmed states on 2026-04-30:**
- Thigh Rescue (`/megababe-thigh-rescue/p/`) → OutOfStock,
  visible "add to waitlist" button.
- Green Deo (`/megababe-the-green-deo/p/`) → InStock, visible
  "add to bag" button, no waitlist.

**`products.csv` fix surfaced by recon:** the `Goop / Green Deo` row
currently uses the brand-page URL with `url_quality=category_page`. The
actual PDP at `https://goop.com/megababe-the-green-deo/p/` loads fine.
Updating that row to the PDP URL with `url_quality=pdp` would let the
checker actually read its stock state instead of always returning ERROR.

### Nordstrom (Playwright, automated — works with default Chromium)

**Recon performed 2026-04-30** against brand page + 3 PDPs (Thigh Rescue,
Après Shave, 2-Pack Space Bar). All loaded HTTP 200, ~560 KB each, no
anti-bot interference. Plain httpx is **not** sufficient — Nordstrom
serves an empty JS-skeleton (~251 KB of bundle without content) to httpx
because the page is a Next.js / SPA bootstrap that needs JS hydration
to render. Default headless Chromium hydrates correctly.

**Stock signals — two layers, both server-rendered:**

(a) **PRIMARY: JSON-LD `<script type="application/ld+json">`** with
`@type: Product`. Two `Product` blocks per page (page-level + a
duplicate likely for desktop/mobile rendering). The offer is an
`AggregateOffer`, not a flat `Offer`:
```json
{"@type": "Product", "name": "...", "offers": {
  "@type": "AggregateOffer",
  "price": "10-14",
  "lowPrice": 10, "highPrice": 14,
  "availability": "http://schema.org/InStock"
}}
```
- `availability` rolls up across all sizes — `InStock` if any size is
  available. **Caveat:** if some sizes are OOS but at least one is
  in-stock, AggregateOffer reads `InStock`, hiding the partial OOS.
- Note: Nordstrom uses `http://schema.org/...` (same as Goop). The
  shared `_json_ld.py` helper already tolerates both schemes.
- **`sku` is `None` at the top level** — Nordstrom doesn't expose a
  product SKU through JSON-LD. So a SKU-keyed cross-check (Cult Beauty
  pattern) won't fire; pick a different confirmation signal.

(b) **CONFIRMATION (when PDP is single-size or you only need top-level
state): visible "Add to Bag" button.** Two buttons matching that text
on every probed page (likely desktop + mobile variants); both visible
when in stock. Pre-block recon notes referenced "sold out" / "Notify
Me" for the OOS state — couldn't confirm DOM (all probed PDPs were in
stock today). Fall back to JSON-LD if button-state ambiguous.

(c) **PER-VARIANT detail (advanced):** Nordstrom embeds a Redux-style
state JSON inline in the page that contains:
- `relatedSkuIds` per size (e.g. `"0.81 oz" → ["AA328619"]`,
  `"2.12 oz" → ["22759968"]`)
- `soldOutSkus: { byId: {}, allIds: [] }` — list of SKUs currently OOS
- `isItemSoldOut`, `selectedSoldOutSku`, etc.

This blob lives inside a `<script>` tag (not `__NEXT_DATA__`; appears
as inline JSON). If we ever need per-size accuracy beyond what
AggregateOffer provides, parse this for the empty/non-empty
`soldOutSkus.allIds` array. **Not needed today** — `products.csv` has
only single-size Nordstrom rows (no Mini variants).

**Variant structure:** the existing brief said "Per-variant matching
applies (e.g., Thigh Rescue .81 oz vs 2.12 oz)" — confirmed by the
embedded state showing both sizes — but `products.csv` only carries the
full-size row. The Mini SKUs are reachable but currently un-tracked.

**Brand page** at `https://www.nordstrom.com/brands/megababe--20023`:
- Loads cleanly. ~563 KB. No anti-bot.
- 30 product-tile entries surfaced (15 unique products × 2 because
  `#product-page-reviews` review-anchor variants double-count). Easy to
  dedupe by stripping the fragment.
- Brand page JSON-LD is `BreadcrumbList` + `ListItem` only — no
  per-product structured data.
- **Tiles do not expose stock state** — every tile in this run reads
  "in stock?" with no OOS/sold-out marker. Per existing notes, OOS at
  Nordstrom is rare and signaled mostly by the product disappearing
  from the brand page. So step 10 reconciliation can use brand-page
  presence/absence, but not per-tile stock.

**Confirmed states 2026-04-30** (all in stock; need to find a known-OOS
PDP later to verify the OOS DOM pattern): Thigh Rescue (5252056),
Après Shave (7617419), 2-Pack Space Bar (7154724) — all `InStock`.

**Recommendation:** ship Nordstrom next. Same pattern as Cult Beauty /
Goop with two adjustments: (1) don't try to cross-check by SKU since
JSON-LD's SKU is null, use visible "Add to Bag" presence instead, and
(2) acknowledge the AggregateOffer rollup limitation in `notes`.

### Boots (PARTIAL — brand page works, PDPs Incapsula-blocked)

**Recon performed 2026-04-30.** Mixed result:

- **Brand page** (`https://www.boots.com/megababe`): loads cleanly via
  default Playwright Chromium. ~1.2 MB. 5 unique product tiles
  identified, matching exactly the 5 rows in `products.csv` (Thigh
  Rescue 60g, Thigh Rescue Mini 23g, Apres Shave, Night Rescue, Le
  Tush). No anti-bot.
- **PDPs are Incapsula-blocked**: every PDP returns HTTP 403 with
  ~887-byte body containing markers `pardon our interruption`,
  `incapsula`. Default Playwright Chromium is rejected; would need
  stealth bypass to reach PDP content.

**Brand page can NOT detect in-stock vs OOS for present products.** I
checked carefully:
- Brand page JSON-LD: zero blocks (no per-product structured data).
- "out of stock" text appears 3 times in the brand HTML, but all three
  occurrences are non-tile chrome:
  1. A `<div id="outOfStockTitleDiv" style="display:none">Out of stock</div>`
     — modal title for the cart-full overlay.
  2. A filter toggle: "Hide out of stock items" (input
     `name="inStock"`, `aria-checked="false"` by default — the listing
     INCLUDES OOS products).
  3. A state JSON: `"outOfStockToggle":true`.
- So a tile being present on the brand page means **Boots still
  carries this product**, NOT that it's currently in stock.

**Implication for build strategy:** brand-page-only checking gives us
"delisted vs not delisted", not "OOS vs in stock". Per the user's note
that "Boots has never been observed OOS", a delisted-only signal might
be acceptable as a baseline — but it's substantially weaker than what
we have for other retailers.

**Build options:**
- **Option 1 (cheap, brittle):** brand-page-only checker. Returns
  IN_STOCK if the product's URL appears as a tile on the brand page,
  ERROR if absent (to flag for manual review — could be delisted or
  could be a CSS change). Never returns OOS. User has to discover OOS
  manually.
- **Option 2 (right):** stealth bypass for PDPs (Patchright,
  playwright-extra stealth, real Chrome) and parse the actual PDP DOM.
  No PDP recon was possible so we don't know what selectors to use.
  Pre-block hypotheses from existing notes:
  - "Add to basket" button missing or disabled
  - Page shows an explicit "out of stock" message
  These are still hypotheses — verify after bypass.
- **Option 3 (defer):** add Boots to `MANUAL_RETAILERS` until either of
  the above is worth the effort. Boots has only 5 SKUs.

**Recommendation:** defer (Option 3) until we ship Anthropologie/CVS
strategy — same anti-bot tier, same trade-offs. If we figure out
stealth for one of them, apply the same approach to Boots.

### CVS (MANUAL — Akamai-tier block, sheet route adopted 2026-04-30)

CVS was moved into the manual Google Sheet flow alongside
Target/Walmart/ASOS/Anthropologie on 2026-04-30. With only 2 SKUs and
"never observed OOS", the manual entry cost is negligible. The recon
notes below are kept for completeness.

---

**Recon findings (2026-04-30):**


**Recon performed 2026-04-30** — both httpx and default Playwright
Chromium return HTTP 403 with title "Access Denied" on every URL
(brand search page and both PDPs). Body is ~300-400 bytes — pure error
page, no content. The block is site-wide and pre-content (no Megababe
detail reached at all).

**Build options:**
- **Defer + manual sheet:** add CVS to `MANUAL_RETAILERS`. With only
  2 SKUs (Thigh Rescue, Butt Stuff) and "never observed OOS", this is
  near-zero ongoing effort and the cleanest path.
- **Stealth bypass:** same options as Anthropologie. CVS often uses
  Akamai Bot Manager — somewhat harder to defeat than PerimeterX.

**Original observed signals (per the user, unchanged from pre-block):**
The original tracker checked whether any of pickup / same-day-delivery
/ shipping is available on the PDP. Hypothesis only — recon couldn't
confirm.

**Recommendation:** defer indefinitely. With 2 SKUs and a strong block,
the manual-sheet path is the clear win.

## Recon comparison table — 2026-04-30

| Retailer | Transport | Primary signal | Confirmation | Variants in PDP? | Brand page useful for OOS? |
|---|---|---|---|---|---|
| Cult Beauty | httpx | JSON-LD `Product` / `ProductGroup` `availability` | `data-stock` attr on size button | YES — `ProductGroup.hasVariant[]` keyed by SKU | tile-text "Notify Me" reliable for OOS |
| Goop | Playwright (Cloudflare blocks httpx site-wide) | JSON-LD `Product` `availability` | visible "add to waitlist" button | NO — single-variant only | NO — tiles show no stock state |
| **Nordstrom** | **Playwright** (httpx returns SPA skeleton) | JSON-LD `Product` `AggregateOffer.availability` | visible "Add to Bag" button (no SKU exposed in JSON-LD) | rolled-up via `AggregateOffer` (per-size detail in inline state JSON if needed) | NO — tiles have no stock marker; absence = delisted only |
| **Boots** | **brand page only — PDPs Incapsula-blocked** | brand-page tile presence → "still carried" | n/a (no PDP access) | n/a | LIMITED — tile presence ≠ in stock; tile absence = delisted only |
| **Anthropologie** | **BLOCKED (PerimeterX)** | — | — | likely color/scent share-PDPs (per `products.csv`) | also blocked |
| **CVS** | **BLOCKED (Akamai-style 403)** | — | — | n/a (only 2 SKUs) | also blocked |
| Gee Beauty | httpx | Shopify `.js` storefront endpoint | n/a | per-variant via Shopify variants[] | n/a (already Shopify-direct) |

**Build readiness:**
- **Nordstrom — green-light.** Same pattern as Cult Beauty / Goop with
  two tweaks: skip SKU cross-check (JSON-LD `sku` is null), and accept
  AggregateOffer rollup as the unit of measure. Build next.
- **Boots — defer.** Brand-page-only checker is feasible but produces
  weaker signals than the other retailers (delisted ≠ OOS). Cost/value
  doesn't pencil out until the broader anti-bot strategy is decided.
- **Anthropologie + CVS — defer.** Recommend manual-sheet route
  (`MANUAL_RETAILERS`) as the immediate path. Stealth bypass is a
  separate research project. With Walmart/Target/ASOS already on the
  manual sheet, adding two more is mostly a sheet-tab edit and a CSV
  source-column change.

**Surprises that affect the build plan:**
1. **Nordstrom is the simplest of the four**, despite the existing
   notes implying complex per-variant logic — the AggregateOffer
   rollup handles current `products.csv` rows cleanly.
2. **Boots brand-page tiles don't expose stock state** — this
   invalidates the "fall back to brand-page check if PDP fails"
   strategy that worked for Cult Beauty / Goop. The brand page only
   tells us if Boots still *carries* a product.
3. **Half the remaining retailers (Anthropologie + CVS) are
   anti-bot-locked at a tier that default Playwright can't crack.**
   This is a strategic decision point, not an engineering problem with
   an obvious next step. The cheapest path is folding them into the
   manual sheet flow.
4. **No `products.csv` URL fixes surfaced** by this recon (unlike
   Goop's Green Deo). All probed URLs that loaded resolved to the
   right product.

## Important caveat from user

The behaviors documented above are the ones the user has actually observed.
There may be additional OOS patterns that haven't surfaced yet. When building
checkers:

- Don't assume the documented patterns are exhaustive
- Log the raw signals (button text, button state, presence of "notify me",
  presence on brand page) into the database `notes` field so we can investigate
  unexpected ERROR/UNKNOWN results
- When a checker hits a state it doesn't recognize, return UNKNOWN with a
  detailed note rather than guessing

## Brand-page reconciliation step (new — runs after PDP checks)

This is a new pass that runs once per retailer at the end of each weekly run.
It addresses two needs:

1. **Reclassifying "broken URL" errors.** A PDP that 404s or redirects almost
   always means OOS, not "this URL is broken in our spreadsheet." We confirm
   by scanning the retailer's brand page for the product name.
2. **Surfacing new products.** Brand page may list products that aren't yet in
   `products.csv` (newly launched SKUs at that retailer). The user wants to
   know about these so they can add them.

### Logic

For each retailer, after PDP checks complete:

1. Fetch the brand page URL (use Playwright for retailers that already use it,
   simple HTTP for Shopify retailers via `/collections/megababe/products.json`).
2. Extract every Megababe product link + product name from the page.
3. For each product in `products.csv` for that retailer that returned ERROR
   this run:
   - Always **downgrade ERROR → OOS** with a note like "PDP unavailable;
     reconciled via brand page"
   - This applies whether or not the product appears on the brand page —
     per the user, a missing PDP is overwhelmingly an OOS signal, not a
     delisted-from-assortment signal
   - If the user later confirms a product is permanently delisted, they
     update `products.csv` (e.g., remove the row or mark it inactive).
     We don't try to detect delisting automatically.
4. For each product *on the brand page* that doesn't match any row in
   `products.csv` for that retailer:
   - Add a row to a new `new_products` table with retailer, name, URL, first
     seen timestamp
   - Surface on the dashboard under "New products detected — add to
     products.csv?"

### Name matching

Substring match, case-insensitive, accent-insensitive (NFD normalize + strip
combining marks). For each `products.csv` row name, check if it appears as a
substring of any brand-page product name.

Examples that should match:
- CSV "Rosy Pits" → brand page "Megababe Rosy Pits Daily Solid Deodorant 2.6oz" ✓
- CSV "Thigh Rescue" → brand page "Thigh Rescue Anti-Friction Stick" ✓
- CSV "Apres Shave" → brand page "Megababe Après Shave Ingrown Hair Roll-On" ✓
  (after accent-stripping "Après" → "Apres")

Edge cases where substring matching is ambiguous:
- "Thigh Rescue" matches BOTH "Thigh Rescue Anti-Friction Stick" (regular) AND
  "Thigh Rescue Mini Anti-Friction Stick" (mini). Resolve by preferring the
  match where the CSV name is followed by a word boundary not "Mini" — i.e.,
  match "Thigh Rescue" against the longer "Thigh Rescue Mini" name only as a
  fallback if there's no shorter match.
- "Body Dust" vs "Body Dust Mini": same pattern.
- For these cases, the safest approach: if a CSV name appears as a substring
  of multiple brand-page names, mark the brand-page product as a possible
  match for the *most specific* CSV row (longest CSV name that's still a
  substring of the brand-page name).

When matching is ambiguous, surface for human review on the dashboard rather
than guessing.

### Database additions

```sql
CREATE TABLE new_products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    product_name TEXT NOT NULL,    -- as it appears on the brand page
    url TEXT NOT NULL,
    first_seen TIMESTAMP NOT NULL,
    confirmed BOOLEAN DEFAULT 0,    -- user marks true after adding to products.csv
    UNIQUE(retailer, product_name)
);
```

`checks` table gets a new note pattern: when a status changes from ERROR to
OOS via brand-page reconciliation, the note is "reconciled-via-brand-page".
This makes the dashboard able to distinguish "PDP works and product is OOS"
from "PDP missing but reconciled to OOS via brand page."

### Dashboard additions

Two new sections appear on the dashboard:

1. **New products detected** (above "URLs to fix") — lists rows from
   `new_products` where `confirmed = 0`, with retailer, name, URL, "first
   seen" date. Action item for the user.
2. **Reconciled OOS** subsection within "Currently Out of Stock" — small badge
   or note indicating that the PDP itself didn't load, status was inferred
   from absence on the brand page. Useful so the user knows when to
   investigate vs. trust.
