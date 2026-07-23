from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from .base import CheckResult, Checker, Product, Status

# Pretend to be a real browser. Shopify's storefront JSON is public, but some
# stores (and CDN frontends) reject default httpx UA strings.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/javascript,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# geebeauty.ca's storefront edge (Cloudflare-fronted) returns 429 partway
# through a burst of ~20 back-to-back requests (one per SKU), with a
# `Retry-After` that's observed to be a flat 60s IP-level cooldown — not a
# per-request token bucket that refills quickly. So: space requests out to
# avoid tripping it, and on a 429 treat the cooldown as *shared* across every
# remaining product in this run (one wait, not one wait per product) rather
# than retrying each product in a loop, which would multiply the wait time
# and hammer an already-rate-limiting host.
_MIN_REQUEST_INTERVAL = 1.5  # seconds between requests to the same checker
_MAX_RETRY_WAIT = 90.0  # cap on how long we honor a Retry-After value
_DEFAULT_BACKOFF = 5.0  # seconds; used if 429 has no Retry-After header


def _to_storefront_js_url(url: str) -> str:
    """Drop query/fragment and append `.js` (Shopify storefront JSON)."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith(".js"):
        path = f"{path}.js"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class ShopifyChecker:
    """Checker for Shopify storefronts that expose `/products/<handle>.js`.

    Treats each PDP as one row — if any variant is available, the product is
    IN_STOCK. Per-variant tracking is deliberately out of scope for MVP.
    """

    retailer: str

    def __init__(self, retailer: str, client: httpx.Client | None = None) -> None:
        self.retailer = retailer
        self._client = client or httpx.Client(
            headers=_HEADERS, timeout=15.0, follow_redirects=True
        )
        self._owns_client = client is None
        self._last_request_at: float | None = None
        self._blocked_until: float | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ShopifyChecker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = 0.0
        if self._blocked_until is not None:
            wait = max(wait, self._blocked_until - now)
        if self._last_request_at is not None:
            wait = max(wait, _MIN_REQUEST_INTERVAL - (now - self._last_request_at))
        if wait > 0:
            time.sleep(wait)

    def _request(self, endpoint: str) -> httpx.Response:
        self._throttle()
        r = self._client.get(endpoint)
        self._last_request_at = time.monotonic()
        return r

    def _get_with_retry(self, endpoint: str) -> httpx.Response:
        r = self._request(endpoint)
        if r.status_code != 429:
            self._blocked_until = None
            return r

        retry_after = r.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else _DEFAULT_BACKOFF
        except ValueError:
            delay = _DEFAULT_BACKOFF
        # Shared cooldown: every subsequent product's _throttle() will wait
        # this out before its own first attempt, instead of each product
        # retrying independently.
        self._blocked_until = time.monotonic() + min(delay, _MAX_RETRY_WAIT)

        r2 = self._request(endpoint)  # single retry, after the cooldown
        if r2.status_code != 429:
            self._blocked_until = None
        return r2

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)
        endpoint = _to_storefront_js_url(product.url)

        try:
            r = self._get_with_retry(endpoint)
        except httpx.HTTPError as e:
            return CheckResult(Status.ERROR, now, f"request failed: {e}")

        if r.status_code != 200:
            return CheckResult(
                Status.ERROR, now, f"HTTP {r.status_code} from {endpoint}"
            )

        try:
            data = r.json()
        except ValueError:
            return CheckResult(Status.ERROR, now, "non-JSON response")

        variants = data.get("variants")
        if not isinstance(variants, list):
            return CheckResult(Status.ERROR, now, "no variants[] in response")
        if not variants:
            return CheckResult(Status.UNKNOWN, now, "empty variants list")

        if product.variant_match:
            target = product.variant_match.casefold()
            matches = [v for v in variants if (v.get("title") or "").casefold() == target]
            if not matches:
                titles = ", ".join(v.get("title", "?") for v in variants)
                return CheckResult(
                    Status.ERROR,
                    now,
                    f"variant_match {product.variant_match!r} not in [{titles}]",
                )
            return CheckResult(
                Status.IN_STOCK if matches[0].get("available") else Status.OOS,
                now,
            )

        if any(v.get("available") for v in variants):
            return CheckResult(Status.IN_STOCK, now)
        return CheckResult(Status.OOS, now)
