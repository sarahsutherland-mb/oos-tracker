from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Callable

import httpx

from .base import CheckResult, Product, Status

# Map sheet status string (lowercased) -> our Status enum.
_STATUS_MAP: dict[str, Status] = {
    "in_stock": Status.IN_STOCK,
    "in stock": Status.IN_STOCK,  # tolerate user formatting
    "oos": Status.OOS,
    "out_of_stock": Status.OOS,
    "out of stock": Status.OOS,
    "error": Status.ERROR,
}

LastKnownFn = Callable[[int], CheckResult | None]


class ManualSheet:
    """Fetches and caches the published Google Sheet CSV.

    One instance is shared across the per-retailer ManualSheetCheckers so the
    HTTP fetch happens once per run.
    """

    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.url = url
        self._client = client or httpx.Client(timeout=15.0, follow_redirects=True)
        self._owns_client = client is None
        self._lookup: dict[tuple[str, str], dict[str, str]] | None = None
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
        inst._lookup = inst._parse(text)
        return inst

    def load(self) -> None:
        """Fetch and parse. Safe to call multiple times — only fetches once."""
        if self._lookup is not None or self.fetch_error is not None:
            return
        try:
            r = self._client.get(self.url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            self._lookup = {}
            self.fetch_error = f"sheet fetch failed: {e}"
            return
        self._lookup = self._parse(r.text)

    @staticmethod
    def _parse(text: str) -> dict[tuple[str, str], dict[str, str]]:
        reader = csv.DictReader(io.StringIO(text))
        out: dict[tuple[str, str], dict[str, str]] = {}
        for row in reader:
            retailer = (row.get("retailer") or "").strip()
            name = (row.get("product_name") or "").strip()
            if not retailer or not name:
                continue
            out[(retailer, name)] = row
        return out

    def get(self, retailer: str, product_name: str) -> dict[str, str] | None:
        self.load()
        assert self._lookup is not None
        return self._lookup.get((retailer, product_name))


class ManualSheetChecker:
    """Resolves a manual product's status from the shared ManualSheet."""

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

        # If the sheet fetch failed, fall back to most recent DB status so
        # one bad sheet fetch doesn't poison the whole run.
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

        row = self._sheet.get(product.retailer, product.name)
        if row is None:
            return CheckResult(Status.UNKNOWN, now, "not in manual sheet")

        notes = (row.get("notes") or "").strip() or None
        status_raw = (row.get("status") or "").strip().lower()

        if not status_raw:
            return CheckResult(Status.UNKNOWN, now, notes)

        status = _STATUS_MAP.get(status_raw)
        if status is None:
            return CheckResult(
                Status.ERROR, now, f"invalid sheet status: {status_raw!r}"
            )

        checked_at = _parse_last_checked(row.get("last_checked"), default=now)
        return CheckResult(status, checked_at, notes)


def _parse_last_checked(raw: str | None, default: datetime) -> datetime:
    s = (raw or "").strip()
    if not s:
        return default
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
