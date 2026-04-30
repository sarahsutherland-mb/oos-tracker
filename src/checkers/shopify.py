from __future__ import annotations

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

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ShopifyChecker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)
        endpoint = _to_storefront_js_url(product.url)

        try:
            r = self._client.get(endpoint)
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
