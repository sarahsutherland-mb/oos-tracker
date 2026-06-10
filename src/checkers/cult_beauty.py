"""Cult Beauty PDP checker.

Recon (2026-04-30, see RETAILER_KNOWLEDGE.md) confirmed Cult Beauty PDPs are
server-rendered with schema.org JSON-LD; plain httpx with a desktop-Chrome
User-Agent works without any anti-bot challenge.

JSON-LD parsing is shared with other retailers via `_json_ld.py`. The
`data-stock` cross-check below is Cult-Beauty-specific.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from .base import CheckResult, Product, Status
from ._json_ld import parse_availability

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class CultBeautyChecker:
    retailer: str

    def __init__(
        self,
        retailer: str = "Cult Beauty",
        client: httpx.Client | None = None,
    ) -> None:
        self.retailer = retailer
        self._client = client or httpx.Client(
            headers=_HEADERS, timeout=20.0, follow_redirects=True
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CultBeautyChecker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)

        try:
            r = self._client.get(product.url)
        except httpx.HTTPError as e:
            return CheckResult(Status.ERROR, now, f"request failed: {e}")
        if r.status_code != 200:
            return CheckResult(Status.ERROR, now, f"HTTP {r.status_code}")

        result = parse_availability(r.text, product.variant_match)
        if result.error is not None:
            return CheckResult(Status.ERROR, now, result.error)
        if result.status == Status.UNKNOWN:
            return CheckResult(
                Status.UNKNOWN,
                now,
                f"unrecognized availability: {result.raw_availability!r}",
            )

        # Cult-Beauty-specific cross-check: data-stock attribute on the
        # button matching the resolved SKU should agree with JSON-LD.
        ds = _find_data_stock(r.text, result.sku)
        if ds is not None:
            expected = "true" if result.status == Status.IN_STOCK else "false"
            if ds != expected:
                return CheckResult(
                    Status.ERROR,
                    now,
                    f"JSON-LD/data-stock disagree: availability={result.raw_availability} "
                    f"data-stock={ds!r} sku={result.sku}",
                )
        return CheckResult(result.status, now)


def _find_data_stock(html: str, sku: str | None) -> str | None:
    """Locate the `data-stock` value for a given SKU on the page.

    Cult Beauty puts both `data-sku` and `data-stock` on the primary
    add-to-basket button (single-variant PDPs) and on each size-picker
    button (multi-variant PDPs). The two attrs can appear in either
    order in the source. Returns 'true'/'false' or None if not found.
    """
    if not sku:
        return None
    pat1 = rf'\bdata-sku="{re.escape(sku)}"[^>]*\bdata-stock="(true|false)"'
    pat2 = rf'\bdata-stock="(true|false)"[^>]*\bdata-sku="{re.escape(sku)}"'
    for pat in (pat1, pat2):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None
