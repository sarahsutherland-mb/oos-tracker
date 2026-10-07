from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from .base import CheckResult, Product, Status

# NOT WIRED IN: redsky returns HTTP 435 (PerimeterX) to GitHub's runners, so
# Target stays on the manual sheet. Kept because it works from a laptop.
#
# Target's own frontend reads stock from its "redsky" API, which answers
# plain httpx (probed 2026-10-07: PDP 200, API 200, no challenge). One
# `product_summary_with_fulfillment_v1` call takes every TCIN at once, so all
# 25 SKUs cost a PDP fetch (for the key) plus a single API request.
_API_URL = (
    "https://redsky.target.com/redsky_aggregations/v1/web/"
    "product_summary_with_fulfillment_v1"
)
# The key is public -- it's embedded in every PDP for the browser to use --
# and is re-read from a live PDP each run in case Target rotates it. This is
# the value seen on 2026-10-07, used only if that read fails.
_FALLBACK_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
_KEY_RE = re.compile(r'apiKey\\?"\s*:\s*\\?"([0-9a-f]{40})')
_TCIN_RE = re.compile(r"/A-(\d+)")

# Any store works: the fields we read (`shipping_options` and
# `is_out_of_stock_in_all_store_locations`) are national, not per-store.
_STORE_ID = "1206"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.target.com",
    "Referer": "https://www.target.com/",
}

# Shipping states that mean a guest can buy it online today.
_ONLINE_OK = {"IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE"}


def tcin_from_url(url: str) -> str | None:
    m = _TCIN_RE.search(url)
    return m.group(1) if m else None


def judge(summary: dict) -> tuple[Status, str]:
    """Status for one `product_summaries[]` entry, plus the raw signals.

    Online availability decides, per the user (2026-10-07): if a guest can
    buy it on target.com it's in stock, whatever the stores say. Store state
    still goes in the note -- "online in stock, every store out" is worth
    seeing even though it isn't OOS.
    """
    f = summary.get("fulfillment") or {}
    ship = (f.get("shipping_options") or {}).get("availability_status")
    stores_out = f.get("is_out_of_stock_in_all_store_locations")
    note = f"online={ship or 'none'}; stores={'all out' if stores_out else 'some in stock'}"
    if f.get("sold_out"):
        return Status.OOS, f"sold_out; {note}"
    if ship in _ONLINE_OK:
        return Status.IN_STOCK, note
    if ship is None:
        return Status.UNKNOWN, note
    return Status.OOS, note


class TargetChecker:
    """All Target SKUs from one redsky fulfillment request, loaded lazily.

    A TCIN missing from a good response is OOS: Target 404s a delisted PDP
    and the API drops it ("No product found"), which the sheet already
    counted as out of stock. A response with no summaries at all is treated
    as a failed fetch, not as 25 delistings.
    """

    retailer = "Target"

    def __init__(self, products: list[Product], client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers=_HEADERS, timeout=30.0, follow_redirects=True
        )
        self._tcins = [t for p in products if (t := tcin_from_url(p.url))]
        self._sample_url = products[0].url if products else None
        self._owns_client = client is None
        self._summaries: dict[str, dict] | None = None
        self._error: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _api_key(self) -> str:
        if self._sample_url:
            try:
                m = _KEY_RE.search(self._client.get(self._sample_url).text)
                if m:
                    return m.group(1)
            except httpx.HTTPError:
                pass
        return _FALLBACK_KEY

    def _load(self) -> dict[str, dict]:
        if self._summaries is not None:
            return self._summaries
        self._summaries = {}
        try:
            r = self._client.get(
                _API_URL,
                params={
                    "key": self._api_key(),
                    "tcins": ",".join(self._tcins),
                    "store_id": _STORE_ID,
                    "channel": "WEB",
                    "page": "/p/",
                },
            )
            if r.status_code != 200:
                self._error = f"redsky HTTP {r.status_code}"
                return self._summaries
            rows = (r.json().get("data") or {}).get("product_summaries") or []
        except (httpx.HTTPError, ValueError) as e:
            self._error = f"redsky {type(e).__name__}: {e}"
            return self._summaries
        if not rows:
            self._error = "redsky returned no products"
        self._summaries = {s["tcin"]: s for s in rows if isinstance(s, dict) and "tcin" in s}
        return self._summaries

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)
        tcin = tcin_from_url(product.url)
        if tcin is None:
            return CheckResult(Status.ERROR, now, "no TCIN (/A-<digits>) in url")
        summaries = self._load()
        if self._error:
            return CheckResult(Status.ERROR, now, self._error)
        if tcin not in summaries:
            return CheckResult(Status.OOS, now, f"TCIN {tcin} not found; delisted")
        status, note = judge(summaries[tcin])
        return CheckResult(status, now, note)
