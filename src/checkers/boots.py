"""Boots PDP checker — deferred stub.

Recon (2026-04-30, see RETAILER_KNOWLEDGE.md) showed:
- Boots PDPs are Incapsula-blocked: default Playwright Chromium gets
  HTTP 403 with a "Pardon Our Interruption" challenge page.
- The brand page works, but its tiles don't reveal in-stock vs OOS —
  only catalog presence vs absence.

Until Incapsula bypass is decided (stealth tooling, paid service) or
Boots is folded into the manual sheet, this checker presumes IN_STOCK
for every check (per the user, Boots has never been observed OOS — see
2026-07-23 decision in RETAILER_KNOWLEDGE.md) rather than returning
ERROR. Five SKUs total (per `products.csv`), all currently
`url_quality=pdp` and all currently unreachable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import CheckResult, Product, Status

_NOTE = (
    "Boots PDPs Incapsula-blocked; presumed in-stock, not actually checked "
    "(see RETAILER_KNOWLEDGE.md)"
)


class BootsChecker:
    retailer: str

    def __init__(self, retailer: str = "Boots") -> None:
        self.retailer = retailer

    def close(self) -> None:
        return None

    def __enter__(self) -> BootsChecker:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def check(self, product: Product) -> CheckResult:
        return CheckResult(Status.IN_STOCK, datetime.now(timezone.utc), _NOTE)
