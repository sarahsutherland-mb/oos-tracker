"""Goop PDP checker — Playwright-based.

Recon (2026-04-30, see RETAILER_KNOWLEDGE.md) found that goop.com is
behind site-wide Cloudflare and httpx returns 403 on every URL. Default
headless Chromium via Playwright passes through cleanly (no CAPTCHA, no
interactive challenge), so this checker uses Playwright as transport.

Stock signal hierarchy:
- PRIMARY: schema.org JSON-LD `availability`. Goop's Megababe SKUs are
  all single-variant (`@type=Product`), so `variant_match` is unused.
  Parsing is shared with other JSON-LD retailers via `_json_ld.py`.
- CONFIRMATION: a visible `<button>` containing "add to waitlist"
  (lowercase, partial match). Recon showed this only appears on OOS
  PDPs; in-stock PDPs have a visible "add to bag" instead. If JSON-LD
  and waitlist visibility disagree, return ERROR with both signals in
  `notes` — that would indicate cache drift on Goop's side and is
  worth investigating, not silently picking a winner.

Browser lifecycle: Playwright + Chromium are launched lazily on the
first `check()` call so importing this module is cheap. `close()` tears
everything down; the orchestrator is responsible for calling it (or use
the checker as a context manager).
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


class GoopChecker:
    retailer: str

    def __init__(
        self,
        retailer: str = "Goop",
        browser: Browser | None = None,
    ) -> None:
        """If `browser` is provided, share it (caller owns lifecycle).
        Otherwise launch a private Playwright runtime + Chromium on the
        first check. Sharing matters because the orchestrator runs
        multiple Playwright-backed checkers in one process — Playwright's
        sync API doesn't allow two `sync_playwright()` runtimes in the
        same thread."""
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

    def __enter__(self) -> GoopChecker:
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

            # Best-effort settle. Don't fail the check if networkidle
            # never fires — third-party trackers can keep it busy forever.
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=_NETWORK_IDLE_TIMEOUT_MS
                )
            except Exception:
                pass

            html = page.content()

            low = html.lower()
            if "just a moment" in low or "checking your browser" in low:
                return CheckResult(
                    Status.ERROR,
                    now,
                    "Cloudflare interstitial not bypassed",
                )

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
                waitlist_visible = (
                    page.locator(
                        "button:has-text('add to waitlist'):visible"
                    ).count()
                    > 0
                )
            except Exception:
                waitlist_visible = False

            expects_waitlist = result.status == Status.OOS
            if waitlist_visible != expects_waitlist:
                return CheckResult(
                    Status.ERROR,
                    now,
                    f"JSON-LD/waitlist disagree: availability="
                    f"{result.raw_availability} waitlist_visible={waitlist_visible}",
                )
            return CheckResult(result.status, now)
        finally:
            try:
                page.close()
            except Exception:
                pass
