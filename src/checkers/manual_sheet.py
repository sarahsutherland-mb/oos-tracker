from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Callable, Iterable

import httpx

from .base import CheckResult, Product, Status

# Retailers tracked via the manual Google Sheet. Rows for any other retailer
# in the sheet are ignored during parse.
# Anthropologie + CVS were added 2026-04-30 — both are anti-bot-locked at a
# tier default Playwright can't crack (PerimeterX and Akamai 403 respectively),
# and the manual-sheet route is cheaper than the bypass tooling we'd need.
TRACKED_RETAILERS = ("Target", "Walmart", "ASOS", "Anthropologie", "CVS")

# Sheet "Stock status" values we recognize (compared after .strip().lower()).
# Anything else becomes ERROR with the raw value preserved in notes.
# Blank → UNKNOWN.
_STATUS_MAP: dict[str, Status] = {
    "in stock": Status.IN_STOCK,
    "out of stock": Status.OOS,
}

LastKnownFn = Callable[[int], CheckResult | None]


class ManualSheet:
    """Fetches and caches the published Google Sheet CSV.

    Sheet schema: columns "Retailer", "Product Name", "Stock status"
    (other columns ignored). Rows where Retailer is not in
    TRACKED_RETAILERS are dropped during parse.
    """

    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.url = url
        self._client = client or httpx.Client(timeout=15.0, follow_redirects=True)
        self._owns_client = client is None
        self._statuses: dict[tuple[str, str], str] | None = None
        self.fetch_error: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ManualSheet:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def from_csv_text(cls, text: str) -> ManualSheet:
        """Build a sheet from raw CSV text (for tests / offline use)."""
        inst = cls.__new__(cls)
        inst.url = "<inline>"
        inst._client = None  # type: ignore[assignment]
        inst._owns_client = False
        inst.fetch_error = None
        inst._statuses = inst._parse(text)
        return inst

    def load(self) -> None:
        """Fetch + parse. Idempotent — only fetches once per instance."""
        if self._statuses is not None or self.fetch_error is not None:
            return
        try:
            r = self._client.get(self.url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            self._statuses = {}
            self.fetch_error = f"sheet fetch failed: {e}"
            return
        self._statuses = self._parse(r.text)

    @staticmethod
    def _parse(text: str) -> dict[tuple[str, str], str]:
        """Parse CSV text → {(retailer, product_name): raw_status_string}."""
        reader = csv.DictReader(io.StringIO(text))
        out: dict[tuple[str, str], str] = {}
        for row in reader:
            retailer = (row.get("Retailer") or "").strip()
            if retailer not in TRACKED_RETAILERS:
                continue
            name = (row.get("Product Name") or "").strip()
            if not name:
                continue
            status = (row.get("Stock status") or "").strip()
            out[(retailer, name)] = status
        return out

    def get_raw(self, retailer: str, product_name: str) -> str | None:
        """Raw status string for a row, or None if the row is absent.
        An empty string return value means the row exists but the cell is blank."""
        self.load()
        assert self._statuses is not None
        return self._statuses.get((retailer.strip(), product_name.strip()))

    def reconcile(self, products: Iterable[Product]) -> dict:
        """Diff sheet keys against products.csv for tracked retailers.

        Returns {
            'unmatched_sheet': sorted list of (retailer, name) present in
                sheet but not in products.csv,
            'unmatched_csv':   sorted list of (retailer, name) present in
                products.csv but not in sheet,
            'fetch_error':     error string if the sheet fetch failed
                               (both lists are empty in that case),
        }"""
        self.load()
        if self.fetch_error is not None:
            return {
                "unmatched_sheet": [],
                "unmatched_csv": [],
                "fetch_error": self.fetch_error,
            }
        assert self._statuses is not None
        sheet_keys = set(self._statuses.keys())
        csv_keys = {
            (p.retailer.strip(), p.name.strip())
            for p in products
            if p.retailer in TRACKED_RETAILERS
        }
        return {
            "unmatched_sheet": sorted(sheet_keys - csv_keys),
            "unmatched_csv": sorted(csv_keys - sheet_keys),
            "fetch_error": None,
        }


class ManualSheetChecker:
    """Resolves a manual product's status from the shared ManualSheet.

    Uses the run's wall-clock time as `checked_at` (per spec — last_checked
    is no longer read from the sheet). Falls back to the most recent DB
    status if the sheet fetch fails entirely."""

    retailer: str

    def __init__(
        self,
        retailer: str,
        sheet: ManualSheet,
        last_known: LastKnownFn | None = None,
    ) -> None:
        self.retailer = retailer
        self._sheet = sheet
        self._last_known = last_known

    def check(self, product: Product) -> CheckResult:
        now = datetime.now(timezone.utc)

        self._sheet.load()
        if self._sheet.fetch_error is not None:
            if self._last_known is not None:
                prev = self._last_known(product.id)
                if prev is not None:
                    return CheckResult(
                        prev.status,
                        prev.checked_at,
                        f"{self._sheet.fetch_error}; using last-known from DB",
                    )
            return CheckResult(Status.ERROR, now, self._sheet.fetch_error)

        raw = self._sheet.get_raw(product.retailer, product.name)
        if raw is None:
            return CheckResult(Status.UNKNOWN, now, "not in manual sheet")
        if raw == "":
            return CheckResult(Status.UNKNOWN, now, "blank in sheet")

        status = _STATUS_MAP.get(raw.lower())
        if status is None:
            return CheckResult(Status.ERROR, now, f"unrecognized status: {raw!r}")
        return CheckResult(status, now)
