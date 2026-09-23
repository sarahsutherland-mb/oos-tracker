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


def _product_handle(url: str) -> str:
    """Last path segment of a Shopify PDP url — the product handle.

    `https://geebeauty.ca/products/rosy-pits?x=1` -> `rosy-pits`
    """
    path = urlsplit(url).path.rstrip("/")
    handle = path.rsplit("/", 1)[-1]
    return handle[:-3] if handle.endswith(".js") else handle


class ShopifyChecker:
    """Checker for Shopify storefronts that expose `/products/<handle>.js`.

    Treats each PDP as one row — if any variant is available, the product is
    IN_STOCK, unless the product carries a `variant_match`, in which case that
    specific variant decides.

    Prefers ONE collection request over N per-product requests. Shopify
    exposes `/collections/<handle>/products.json?limit=250`, which returns
    every product in the collection with the same `variants[].available`
    data the per-product `.js` endpoint gives — so 21 tracked Gee Beauty
    SKUs (18 distinct PDPs, Mini variants sharing a handle) cost a single
    call instead of 18. That matters here: geebeauty.ca's Cloudflare edge
    429s partway through a burst, and the 2026-07-23 run recorded 20 ERROR
    / 1 IN_STOCK because of it. One request can't trip a rate limit.

    Per-product `.js` remains the fallback for any handle the collection
    doesn't list, and for stores with no collection configured.
    """

    retailer: str

    def __init__(
        self,
        retailer: str,
        client: httpx.Client | None = None,
        collection_url: str | None = None,
    ) -> None:
        self.retailer = retailer
        self._client = client or httpx.Client(
            headers=_HEADERS, timeout=15.0, follow_redirects=True
        )
        self._owns_client = client is None
        self._last_request_at: float | None = None
        self._blocked_until: float | None = None
        self._collection_url = collection_url
        # handle -> product dict, populated lazily on first check().
        # None = not yet attempted; {} = attempted and unusable.
        self._collection: dict[str, dict] | None = None
        self._collection_error: str | None = None

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

    def _load_collection(self) -> dict[str, dict]:
        """Fetch the collection once and index it by product handle.

        Failure is non-fatal and recorded, not raised: `check()` falls back
        to the per-product endpoint, preserving the old behaviour.
        """
        if self._collection is not None:
            return self._collection
        self._collection = {}
        if not self._collection_url:
            return self._collection
        try:
            r = self._get_with_retry(self._collection_url)
            if r.status_code != 200:
                self._collection_error = f"HTTP {r.status_code}"
                return self._collection
            payload = r.json()
        except (httpx.HTTPError, ValueError) as e:
            self._collection_error = f"{type(e).__name__}: {e}"
            return self._collection

        products = payload.get("products")
        if not isinstance(products, list):
            self._collection_error = "no products[] in collection response"
            return self._collection
        for entry in products:
            if not isinstance(entry, dict):
                continue
            handle = entry.get("handle")
            if handle:
                self._collection[handle] = entry
        return self._collection

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)

        # Preferred path: this product's handle is in the one-shot collection.
        record = self._load_collection().get(_product_handle(product.url))
        source = "collection"

        if record is None:
            source = "pdp"
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
                record = r.json()
            except ValueError:
                return CheckResult(Status.ERROR, now, "non-JSON response")

        # Only annotate the fallback path, so a normal collection-served run
        # stays quiet in the output.
        note = None
        if source == "pdp":
            note = "via per-product PDP (handle not in collection)"
            if self._collection_error:
                note += f"; collection fetch failed: {self._collection_error}"

        variants = record.get("variants")
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
                note,
            )

        if any(v.get("available") for v in variants):
            return CheckResult(Status.IN_STOCK, now, note)
        return CheckResult(Status.OOS, now, note)
