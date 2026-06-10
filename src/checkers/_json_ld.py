"""Shared schema.org JSON-LD parsing for retailer PDPs.

Multiple retailers (Cult Beauty, Goop) embed `<script type="application/ld+json">`
blocks containing schema.org `Product` or `ProductGroup` data with
`offers.availability`. This module pulls that out into one place so each
retailer's checker only deals with its retailer-specific transport
(httpx vs Playwright) and confirmation signal (data-stock vs visible
waitlist button vs ...).

Per-retailer DOM cross-checks (button state, data attributes) stay in
each retailer's own checker file — those don't generalize.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .base import Status

_LDJSON_RE = re.compile(
    r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Retailers use both http:// and https:// schema.org URLs in the wild —
# Cult Beauty uses https, Goop uses http. Tolerate both.
AVAILABILITY_TO_STATUS: dict[str, Status] = {
    "https://schema.org/InStock": Status.IN_STOCK,
    "http://schema.org/InStock": Status.IN_STOCK,
    "https://schema.org/OutOfStock": Status.OOS,
    "http://schema.org/OutOfStock": Status.OOS,
}


@dataclass(frozen=True)
class JsonLdResult:
    """Outcome of parsing schema.org availability from a PDP.

    Exactly one of `status` and `error` is meaningful per call:
    - On hard failure (JSON-LD missing, ProductGroup without variant_match,
      variant_match miss, etc.): `status` is None, `error` explains why,
      callers should map to ERROR.
    - On success: `status` is IN_STOCK, OOS, or UNKNOWN. UNKNOWN means we
      found JSON-LD with an availability value we don't recognize (a new
      schema.org variant, perhaps); `raw_availability` carries the original
      string so the caller can put it in `notes`.
    """

    status: Status | None
    sku: str | None
    raw_availability: str | None
    error: str | None


def find_product_node(html: str) -> dict | None:
    """Return the first @type=Product or @type=ProductGroup dict in any
    `<script type="application/ld+json">` on the page. None if absent."""
    for raw in _LDJSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in (
                "Product",
                "ProductGroup",
            ):
                return item
    return None


def parse_availability(
    html: str, variant_match: str | None = None
) -> JsonLdResult:
    """End-to-end: find the Product/ProductGroup JSON-LD on a PDP, resolve
    the right variant's availability, and map it to a Status.

    For `@type=Product` (single-variant): returns top-level availability.
    `variant_match` is optional; if provided, sanity-checked against the
    product's `sku` and a mismatch becomes an error.

    For `@type=ProductGroup` (multi-variant): `variant_match` is required
    and matched against `hasVariant[].sku`.
    """
    node = find_product_node(html)
    if node is None:
        return JsonLdResult(None, None, None, "no Product/ProductGroup JSON-LD")

    vm = (variant_match or "").strip() or None

    if node.get("@type") == "ProductGroup":
        if not vm:
            return JsonLdResult(
                None,
                None,
                None,
                "ProductGroup PDP needs variant_match (the variant SKU); "
                "products.csv row has none",
            )
        for v in node.get("hasVariant", []) or []:
            if not isinstance(v, dict):
                continue
            if str(v.get("sku")) == vm:
                return _wrap(_offer_availability(v.get("offers")), str(v.get("sku")))
        seen = ", ".join(
            str(v.get("sku"))
            for v in node.get("hasVariant", []) or []
            if isinstance(v, dict)
        )
        return JsonLdResult(
            None,
            None,
            None,
            f"variant_match {vm!r} not in hasVariant[] (page offers [{seen}])",
        )

    # @type == "Product" — single-variant
    sku = str(node.get("sku")) if node.get("sku") is not None else None
    if vm and sku and vm != sku:
        return JsonLdResult(
            None,
            None,
            None,
            f"variant_match {vm!r} doesn't match Product sku {sku!r}",
        )
    return _wrap(_offer_availability(node.get("offers")), sku)


def _offer_availability(offers: object) -> str | None:
    """Pull `availability` out of an offers dict (or first item if a list)."""
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    return offers.get("availability")


def _wrap(avail: str | None, sku: str | None) -> JsonLdResult:
    if avail is None:
        # JSON-LD present but no availability field — shouldn't happen on
        # well-formed PDPs, but tolerate it as UNKNOWN rather than ERROR.
        return JsonLdResult(Status.UNKNOWN, sku, None, None)
    status = AVAILABILITY_TO_STATUS.get(avail)
    if status is None:
        # Recognized JSON-LD but unrecognized availability value (e.g. a new
        # schema.org variant like Discontinued / PreOrder / BackOrder).
        return JsonLdResult(Status.UNKNOWN, sku, avail, None)
    return JsonLdResult(status, sku, avail, None)
