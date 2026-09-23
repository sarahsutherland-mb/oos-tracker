"""Boots PDP checker — deferred stub.

Recon (2026-04-30, see RETAILER_KNOWLEDGE.md) showed:
- Boots PDPs are Incapsula-blocked: default Playwright Chromium gets
  HTTP 403 with a "Pardon Our Interruption" challenge page.
- The brand page works, but its tiles don't reveal in-stock vs OOS —
  only catalog presence vs absence.

Until Incapsula bypass is decided (stealth tooling, paid service) or
Boots is folded into the manual sheet, this checker reports UNKNOWN.
Five SKUs total (per `products.csv`), all currently `url_quality=pdp`
and all currently unreachable.

It used to return IN_STOCK on the reasoning that Boots had never been
observed OOS (2026-07-23). Changed 2026-08-05: a presumption is not an
observation, and emitting IN_STOCK made five never-checked SKUs
indistinguishable from verified ones — the dashboard showed them as
confirmed stock and `po_lines` cross-referencing treated them as safe to
order against. UNKNOWN is the honest value; it keeps the gap visible
instead of laundering it into a positive signal. Note this is not the
same as ERROR, which means "a check ran and failed" — no check runs here
at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import CheckResult, Product, Status

_NOTE = (
    "Boots PDPs Incapsula-blocked; not checked — status unknown, not assumed "
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
        return CheckResult(Status.UNKNOWN, datetime.now(timezone.utc), _NOTE)
