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
    status: Status
    checked_at: datetime
    notes: str | None = None


class Checker(Protocol):
    retailer: str

    def check(self, product: Product) -> CheckResult: ...
