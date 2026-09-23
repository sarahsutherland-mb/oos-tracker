"""Prices, and what counts as a discount.

Two different signals, deliberately kept apart:

- **On sale** — the retailer says so itself, by publishing a "was" price
  above the current one (Shopify's `compare_at_price` and equivalents).
  Unambiguous, but only some retailers publish it.
- **Price drop** — this run's price is below the last one we recorded. Works
  everywhere a price is readable, and catches the case the first signal
  misses: a retailer quietly discounting without marking it as a sale. That
  is the one worth watching for MAP, so it is not folded into "on sale".

Both are per-retailer and per-currency. These retailers quote CAD, EUR and
USD, so prices are never compared across retailers, and a currency change on
the same product is treated as "no comparison available" rather than as a
99% discount.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


def to_cents(value: object, *, currency: str | None = None) -> int | None:
    """Parse a retailer's price into integer minor units.

    Accepts what the sources actually return: Shopify's decimal strings
    ("20.00"), JSON-LD's bare numbers (13.8, 22). Returns None for anything
    unparseable, absent, or negative -- a price we can't read is not a price
    of zero, and storing it as one would invent a 100% discount.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0 or amount != amount:  # negative or NaN
        return None
    return round(amount * 100)


def format_price(cents: int | None, currency: str | None) -> str:
    """For display. `1380, "EUR"` -> `"EUR 13.80"`."""
    if cents is None:
        return "—"
    return f"{currency or ''} {cents / 100:,.2f}".strip()


@dataclass(frozen=True)
class Discount:
    product_name: str
    retailer: str
    price_cents: int
    currency: str
    # The price this is being compared against: the retailer's own "was"
    # price for an on-sale item, or our previous observation for a drop.
    was_cents: int
    kind: str  # "on_sale" | "price_drop"

    @property
    def percent_off(self) -> int:
        return round((1 - self.price_cents / self.was_cents) * 100)

    @property
    def is_retailer_advertised(self) -> bool:
        return self.kind == "on_sale"


def _rows_to_discounts(rows: list[sqlite3.Row]) -> list[Discount]:
    out: list[Discount] = []
    for r in rows:
        price, was = r["price_cents"], r["was_cents"]
        if price is None or was is None or was <= 0 or price >= was:
            continue
        out.append(
            Discount(
                product_name=r["product_name"],
                retailer=r["retailer"],
                price_cents=price,
                currency=r["currency"] or "",
                was_cents=was,
                kind=r["kind"],
            )
        )
    return sorted(out, key=lambda d: (-d.percent_off, d.retailer, d.product_name))


def current_discounts(conn: sqlite3.Connection) -> list[Discount]:
    """Everything currently discounted, by either signal.

    Reads each product's most recent priced check. A product the retailer
    advertises as on sale is reported that way even if the price also fell
    since last run -- the retailer's own claim is the stronger statement, so
    it wins rather than producing two rows for one product.
    """
    rows = conn.execute(
        """
        WITH priced AS (
          SELECT c.product_id, c.price_cents, c.list_price_cents, c.currency,
                 c.checked_at,
                 ROW_NUMBER() OVER (
                   PARTITION BY c.product_id ORDER BY c.checked_at DESC
                 ) AS rn
          FROM checks c
          WHERE c.price_cents IS NOT NULL
        ),
        latest AS (SELECT * FROM priced WHERE rn = 1),
        previous AS (SELECT * FROM priced WHERE rn = 2)
        SELECT p.product_name, p.retailer, l.price_cents, l.currency,
               CASE
                 WHEN l.list_price_cents IS NOT NULL
                      AND l.list_price_cents > l.price_cents
                 THEN l.list_price_cents
                 ELSE pr.price_cents
               END AS was_cents,
               CASE
                 WHEN l.list_price_cents IS NOT NULL
                      AND l.list_price_cents > l.price_cents
                 THEN 'on_sale'
                 ELSE 'price_drop'
               END AS kind
        FROM latest l
        JOIN products p ON p.id = l.product_id
        LEFT JOIN previous pr
          ON pr.product_id = l.product_id
         -- Same currency only. A product that switched currency has no
         -- comparable previous price, and pretending otherwise would read
         -- as an enormous discount.
         AND pr.currency IS l.currency
        """
    ).fetchall()
    return _rows_to_discounts(rows)


def new_discounts_since(conn: sqlite3.Connection, since_iso: str) -> list[Discount]:
    """Discounts whose price was observed in this run -- what Slack announces.

    Scoped to the run so a standing sale isn't re-announced every Monday.
    """
    latest_at = conn.execute(
        "SELECT MAX(checked_at) AS m FROM checks WHERE price_cents IS NOT NULL"
    ).fetchone()
    if not latest_at or not latest_at["m"] or latest_at["m"] < since_iso:
        return []
    return [d for d in current_discounts(conn)]
