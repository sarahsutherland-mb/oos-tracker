# Where we left off — 2026-04-29

## Build order progress
Steps 1–5 of the 11-step plan in CLAUDE.md are done. Resume at step 6 or
step 8 (see "Next time" below).

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
      Offline-tested across all 6 paths (valid status, blank, missing
      row, invalid value, fetch failure with DB fallback, fetch failure
      without DB history).
- [ ] 6. First Playwright checker (Cult Beauty)
- [ ] 7. Remaining Playwright checkers
- [ ] 8. Orchestrator (`run_check.py`)
- [ ] 9. Dashboard generator (`build_dashboard.py`)
- [ ] 10. Slack notifier
- [ ] 11. GitHub Actions workflow

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
headers. The Shopify checker correctly returns ERROR for it. **Plan:**
move Goop to the Playwright bucket in step 7. Only 1 PDP affected.

## Open questions from the brief, still outstanding

These are listed under "Things to ask me before assuming" in CLAUDE.md
and need answers before steps 10–11:

1. **Slack webhook access** — yes/no? If no, swap to email via smtplib.
2. **Dashboard public or private?** — affects whether `docs/` is
   GitHub Pages or kept private behind GitHub auth.
3. **Per-variant tracking for Cult Beauty / Beauty Bay** — same
   question we just answered for Gee Beauty. Likely yes given how it
   went, but worth confirming when we get to step 6.

## Current file map
```
oos-tracker/
├── CLAUDE.md
├── NOTES.md                      # this file
├── .env.example
├── .gitignore
├── pyproject.toml
├── products.csv                  # now has variant_match column
├── manual_status_template.csv
├── data.db                       # 175 products seeded, 0 check rows
├── src/
│   ├── __init__.py
│   ├── db.py
│   └── checkers/
│       ├── __init__.py
│       ├── base.py
│       ├── shopify.py
│       └── manual_sheet.py
└── .venv/                        # uv-managed
```

## How to verify everything still works tomorrow
```bash
# Re-seed DB (idempotent)
.venv/bin/python -m src.db

# Live-test Shopify against all Gee Beauty PDPs
.venv/bin/python -c "
from src.db import connect, all_products
from src.checkers.base import Product
from src.checkers.shopify import ShopifyChecker
with connect() as conn:
    rows = [Product.from_row(r) for r in all_products(conn)
            if r['retailer'] == 'Gee Beauty' and r['url_quality'] == 'pdp']
with ShopifyChecker('Gee Beauty') as ck:
    for p in rows:
        print(f'{p.name:<25} {p.variant_match or \"\":<10} {ck.check(p).status.value}')
"
```

## Next time — pick one
- **Step 6 (Cult Beauty Playwright)** — adds 23 SKUs but introduces
  the heavy dependency and slow dev loop. Higher risk of selectors
  needing iteration.
- **Step 8 → 9 (orchestrator + dashboard)** against just the 74 SKUs
  we already cover. Lower risk, gets you a real `docs/index.html`
  end-to-end. Recommended next move — catches design issues with the
  orchestrator/dashboard while the surface area is small. ~2–2.5 hr.
