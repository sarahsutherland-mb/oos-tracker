from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class Status(str, Enum):
    IN_STOCK = "IN_STOCK"
    OOS = "OOS"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Product:
    id: int
    retailer: str
    name: str
    url: str
    url_quality: str
    source: str
    variant_match: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Product:
        return cls(
            id=row["id"],
            retailer=row["retailer"],
            name=row["product_name"],
            url=row["url"],
            url_quality=row["url_quality"],
            source=row["source"],
            variant_match=row["variant_match"] if "variant_match" in row.keys() else None,
        )


@dataclass(frozen=True)
class CheckResult:
    """One product's state at one moment.

    Price is optional and absent for most retailers: the manual sheet has no
    price column, and a blocked retailer has no page to read one from. `None`
    means "not observed", never "free" -- so a missing price is stored as NULL
    and skipped by the discount logic rather than compared as zero.

    Money is integer minor units (cents/pence) to keep float drift out of
    equality checks, and `currency` is not optional once `price_cents` is set:
    these retailers quote CAD, EUR and USD, so a bare number is meaningless.
    """

    status: Status
    checked_at: datetime
    notes: str | None = None
    price_cents: int | None = None
    # What the retailer says the price would normally be -- Shopify's
    # `compare_at_price` and its equivalents. Set only when the retailer
    # publishes one AND it is above the current price; a compare-at that
    # matches the price is just the price, not a discount.
    list_price_cents: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.price_cents is not None and not self.currency:
            raise ValueError("price_cents requires a currency")


class Checker(Protocol):
    retailer: str

    def check(self, product: Product) -> CheckResult: ...
