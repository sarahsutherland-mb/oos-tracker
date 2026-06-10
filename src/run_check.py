"""Weekly stock check orchestrator.

Loads products from the DB, dispatches each to the right checker, records
results. Prints a one-line trace per product and a summary tally.

Routing:
- source == 'manual'     -> ManualSheetChecker (skipped if MANUAL_SHEET_URL unset);
                            url_quality is irrelevant here, the sheet is the
                            source of truth regardless of what the URL points to
- url_quality != 'pdp'   -> ERROR row "needs fixing" (no checker call)
- Gee Beauty             -> ShopifyChecker (Shopify storefront .js endpoint)
- Cult Beauty            -> CultBeautyChecker (httpx + JSON-LD)
- Goop                   -> GoopChecker (Playwright + JSON-LD; CF-protected)
- Nordstrom              -> NordstromChecker (Playwright + JSON-LD AggregateOffer)
- Boots                  -> BootsChecker (stub: ERROR; deferred per Incapsula)
- Other Playwright retailers -> skipped until checker exists (no row written)
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from . import brand_pages
from .checkers.base import CheckResult, Product, Status
from .checkers.boots import BootsChecker
from .checkers.cult_beauty import CultBeautyChecker
from .checkers.goop import GoopChecker
from .checkers.manual_sheet import ManualSheet, ManualSheetChecker
from .checkers.nordstrom import NordstromChecker
from .checkers.shopify import ShopifyChecker
from .db import (
    all_products,
    connect,
    init_schema,
    latest_check,
    record_check,
    sync_from_csv,
)

SHOPIFY_RETAILERS = ("Gee Beauty",)
MANUAL_RETAILERS = ("Target", "Walmart", "ASOS", "Anthropologie", "CVS")
# Retailers served by their own bespoke httpx checker (one class per
# retailer; not Shopify, not the manual sheet). Recon showed these are
# server-rendered with usable structured data.
HTTPX_RETAILERS = ("Cult Beauty",)
PLAYWRIGHT_RETAILERS = (
    "Nordstrom",
    "Boots",
    "Goop",
)


def _last_known_factory(conn):
    def fn(product_id):
        row = latest_check(conn, product_id)
        if row is None:
            return None
        try:
            dt = datetime.fromisoformat(row["checked_at"])
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            status = Status(row["status"])
        except ValueError:
            status = Status.UNKNOWN
        return CheckResult(status, dt, row["notes"])

    return fn


def _resolve_sheet_url() -> str | None:
    raw = (os.environ.get("MANUAL_SHEET_URL") or "").strip()
    if not raw or "REPLACE_ME" in raw:
        return None
    return raw


def run() -> int:
    load_dotenv()
    sheet_url = _resolve_sheet_url()
    if sheet_url is None:
        print(
            "warning: MANUAL_SHEET_URL not configured; "
            "manual retailers will be skipped",
            file=sys.stderr,
        )

    counts: Counter[str] = Counter()

    with connect() as conn:
        init_schema(conn)
        sync_from_csv(conn)
        products = [Product.from_row(r) for r in all_products(conn)]
        last_known = _last_known_factory(conn)

        sheet = ManualSheet(sheet_url) if sheet_url else None
        manual_checkers = (
            {r: ManualSheetChecker(r, sheet, last_known) for r in MANUAL_RETAILERS}
            if sheet
            else {}
        )
        shopify_checkers = {r: ShopifyChecker(r) for r in SHOPIFY_RETAILERS}
        httpx_checkers = {"Cult Beauty": CultBeautyChecker()}

        # Playwright sync API forbids two `sync_playwright()` runtimes in
        # the same thread. So the orchestrator owns one runtime + one
        # browser, and shares the browser across every Playwright-backed
        # checker. Launched only if at least one product needs it.
        needs_pw = any(
            p.retailer in PLAYWRIGHT_RETAILERS and p.retailer in ("Goop", "Nordstrom")
            for p in products
        )
        pw_runtime = None
        pw_browser = None
        if needs_pw:
            pw_runtime = sync_playwright().start()
            pw_browser = pw_runtime.chromium.launch(headless=True)
        playwright_checkers = {
            "Goop": GoopChecker(browser=pw_browser),
            "Nordstrom": NordstromChecker(browser=pw_browser),
            "Boots": BootsChecker(),  # stub — no browser needed
        }

        if sheet is not None:
            _print_reconciliation(sheet.reconcile(products))

        # Track ERROR products from this run so the brand-page
        # reconciliation pass can update their check rows in place.
        error_products: list[tuple[Product, str]] = []

        try:
            for p in products:
                outcome = _dispatch(
                    p,
                    manual_checkers,
                    shopify_checkers,
                    httpx_checkers,
                    playwright_checkers,
                )
                if outcome is None:
                    counts["skipped"] += 1
                    print(
                        f"{p.retailer:<15} {p.name:<40} SKIPPED   "
                        f"(no checker for {p.retailer})"
                    )
                    continue
                result = outcome
                checked_at_iso = result.checked_at.isoformat()
                record_check(
                    conn,
                    p.id,
                    result.status.value,
                    checked_at_iso,
                    result.notes,
                )
                counts[result.status.value] += 1
                if result.status == Status.ERROR:
                    error_products.append((p, checked_at_iso))
                note = f"  {result.notes}" if result.notes else ""
                print(
                    f"{p.retailer:<15} {p.name:<40} "
                    f"{result.status.value:<9}{note}"
                )

            # Brand-page reconciliation runs while the shared Playwright
            # browser is still alive (closed in the finally block below).
            print()
            recon = brand_pages.reconcile(
                conn=conn,
                products=products,
                error_products=error_products,
                browser=pw_browser,
            )
            brand_pages.print_summary(recon)
            # Adjust the counters so the final summary line reflects
            # post-reconciliation state. The `checks` table itself was
            # updated in place by `brand_pages.reconcile`.
            counts["ERROR"] -= recon.downgraded
            counts["OOS"] += recon.downgraded
        finally:
            for ck in shopify_checkers.values():
                ck.close()
            for ck in httpx_checkers.values():
                ck.close()
            for ck in playwright_checkers.values():
                ck.close()
            if pw_browser is not None:
                pw_browser.close()
            if pw_runtime is not None:
                pw_runtime.stop()
            if sheet is not None:
                sheet.close()

    print()
    print("Summary:")
    for k in ("IN_STOCK", "OOS", "ERROR", "UNKNOWN", "skipped"):
        if counts[k]:
            print(f"  {k:<10} {counts[k]}")
    return 0


def _print_reconciliation(diff: dict) -> None:
    """Print which (retailer, name) pairs are in the sheet but not products.csv,
    and which are in products.csv but not the sheet. Restricted to the manual
    retailers — the sheet doesn't claim to cover anything else."""
    if diff.get("fetch_error"):
        print(f"Sheet fetch failed: {diff['fetch_error']}")
        print("Reconciliation skipped; per-product checks will fall back to DB.")
        print()
        return

    extra = diff["unmatched_sheet"]
    missing = diff["unmatched_csv"]
    print("Manual sheet reconciliation:")
    print(f"  in sheet but not products.csv: {len(extra)}")
    for r, n in extra:
        print(f"    {r:<10} {n}")
    print(f"  in products.csv but not sheet: {len(missing)}")
    for r, n in missing:
        print(f"    {r:<10} {n}")
    print()


def _dispatch(
    p,
    manual_checkers,
    shopify_checkers,
    httpx_checkers,
    playwright_checkers,
) -> CheckResult | None:
    now = datetime.now(timezone.utc)

    if p.source == "manual":
        ck = manual_checkers.get(p.retailer)
        if ck is None:
            return None
        return ck.check(p)

    if p.url_quality != "pdp":
        return CheckResult(
            Status.ERROR, now, f"url_quality={p.url_quality}; needs fixing"
        )

    if p.retailer in SHOPIFY_RETAILERS:
        return shopify_checkers[p.retailer].check(p)

    if p.retailer in HTTPX_RETAILERS:
        return httpx_checkers[p.retailer].check(p)

    if p.retailer in PLAYWRIGHT_RETAILERS:
        ck = playwright_checkers.get(p.retailer)
        if ck is None:
            return None  # Playwright retailer with no checker yet — skip
        return ck.check(p)

    return CheckResult(Status.UNKNOWN, now, f"no checker for retailer {p.retailer}")


if __name__ == "__main__":
    raise SystemExit(run())
