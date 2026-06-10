"""Nordstrom PDP checker — Playwright-based.

Recon (2026-04-30, see RETAILER_KNOWLEDGE.md) showed Nordstrom serves an
empty SPA skeleton (~251 KB of JS bundle, no rendered content) to httpx,
but default headless Chromium hydrates correctly. JSON-LD is present
once hydrated, with `@type=Product` and `offers.@type=AggregateOffer`
rolling up availability across all sizes for a product.

Stock signal hierarchy:
- PRIMARY: schema.org JSON-LD `offers.availability`. The shared
  `_json_ld.py` helper handles `AggregateOffer` transparently because
  the wrapper only cares about the top-level `availability` field, not
  the offer subtype.
- CONFIRMATION: a visible `<button>` containing "Add to Bag" — recon
  observed two such buttons on every probed PDP (likely desktop+mobile
  variants), both visible when in stock. ERROR if JSON-LD's status and
  button visibility disagree.

Browser lifecycle: Playwright + Chromium are launched lazily on the
first `check()` call (cheap import). One context shared across all
Nordstrom checks in a run; `close()` tears everything down.

KNOWN LIMITATION — AggregateOffer rollup. Nordstrom's `availability` on
multi-size PDPs is "InStock" if ANY size is in stock. So a single-size
OOS on a multi-size product is invisible to this checker. Today,
`products.csv` has only single-size Nordstrom rows, so the rollup is
indistinguishable from per-product status. If Mini variants or other
size-specific Nordstrom rows are added later, switch to parsing the
embedded Redux-style state JSON (look for `relatedSkuIds` /
`soldOutSkus.allIds` in inline `<script>` blobs — recon documented the
location).

Top-level JSON-LD `sku` is null on Nordstrom, so SKU-keyed cross-checks
(the Cult Beauty pattern) don't apply here — we rely on button
visibility instead.
"""

from __future__ import annotations

from datetime import datetime, timezone

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
    sync_playwright,
)

from .base import CheckResult, Product, Status
from ._json_ld import parse_availability

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_NAV_TIMEOUT_MS = 45_000
_NETWORK_IDLE_TIMEOUT_MS = 20_000
# Nordstrom's SPA sometimes injects the Product JSON-LD into <head>
# *after* networkidle fires. Without this wait, ~10–20% of PDPs in a
# single run came back as "no Product/ProductGroup JSON-LD" — flaky.
_LDJSON_WAIT_MS = 15_000

# JS expression that returns true once at least one <script
# type="application/ld+json"> block contains a Product schema. Picked
# over a generic ld+json wait because the BreadcrumbList block renders
# earlier and would otherwise satisfy a generic check.
_LDJSON_READY_JS = (
    '() => Array.from('
    'document.querySelectorAll(\'script[type="application/ld+json"]\')'
    ').some(s => /"@type"\\s*:\\s*"Product"/.test(s.textContent))'
)


class NordstromChecker:
    retailer: str

    def __init__(
        self,
        retailer: str = "Nordstrom",
        browser: Browser | None = None,
    ) -> None:
        """If `browser` is provided, share it (caller owns lifecycle).
        See GoopChecker docstring for why — Playwright's sync API
        forbids two `sync_playwright()` runtimes in the same thread, so
        the orchestrator hands one shared browser to all Playwright
        checkers."""
        self.retailer = retailer
        self._pw: Playwright | None = None
        self._browser: Browser | None = browser
        self._owns_browser = browser is None
        self._context: BrowserContext | None = None

    def _ensure_context(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        return self._context

    def close(self) -> None:
        ctx = self._context
        self._context = None
        try:
            if ctx is not None:
                ctx.close()
        finally:
            if self._owns_browser:
                browser, pw = self._browser, self._pw
                self._browser = self._pw = None
                try:
                    if browser is not None:
                        browser.close()
                finally:
                    if pw is not None:
                        pw.stop()

    def __enter__(self) -> NordstromChecker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)
        ctx = self._ensure_context()
        page = ctx.new_page()
        try:
            try:
                page.goto(
                    product.url,
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception as e:
                return CheckResult(Status.ERROR, now, f"navigation failed: {e}")

            try:
                page.wait_for_load_state(
                    "networkidle", timeout=_NETWORK_IDLE_TIMEOUT_MS
                )
            except Exception:
                pass

            # Bot-detection fallback: Nordstrom intermittently redirects
            # to siteclosed.nordstrom.com/invitation.html on PDPs it
            # decides are coming from a non-standard client. The
            # invitation page has no JSON-LD and no product content —
            # detecting it explicitly produces a clearer ERROR than
            # "no Product/ProductGroup JSON-LD".
            if "siteclosed.nordstrom.com" in page.url:
                return CheckResult(
                    Status.ERROR,
                    now,
                    "Nordstrom redirected to siteclosed/invitation page "
                    "(bot detection); flaky — usually clears on next run",
                )

            # Wait specifically for the Product JSON-LD block to mount.
            # Best-effort: on timeout, fall through and let the parser
            # surface the real error so it lands in `notes`. A retry
            # loop on top of this was tried and didn't help (Run C in
            # the 2026-04-30 diagnostic — same ~83% success rate as no
            # waits); the wait_for_function alone got us to ~96%.
            try:
                page.wait_for_function(_LDJSON_READY_JS, timeout=_LDJSON_WAIT_MS)
            except Exception:
                pass

            html = page.content()
            result = parse_availability(html, product.variant_match)
            if result.error is not None:
                return CheckResult(Status.ERROR, now, result.error)
            if result.status == Status.UNKNOWN:
                return CheckResult(
                    Status.UNKNOWN,
                    now,
                    f"unrecognized availability: {result.raw_availability!r}",
                )

            try:
                add_to_bag_visible = (
                    page.locator("button:has-text('Add to Bag'):visible").count()
                    > 0
                )
            except Exception:
                add_to_bag_visible = False

            expects_visible = result.status == Status.IN_STOCK
            if add_to_bag_visible != expects_visible:
                return CheckResult(
                    Status.ERROR,
                    now,
                    f"JSON-LD/Add-to-Bag disagree: availability="
                    f"{result.raw_availability} add_to_bag_visible={add_to_bag_visible}",
                )
            return CheckResult(result.status, now)
        finally:
            try:
                page.close()
            except Exception:
                pass
