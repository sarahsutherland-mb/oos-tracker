"""Prices, and what counts as a discount.

The traps these pin down are the ones that would quietly invent discounts:
an unparseable price read as zero, a currency switch read as a 99% markdown,
and a `compare_at_price` equal to the price read as a 0%-off sale.

Run with: .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import sqlite3

import pytest

from src import db, pricing
from src.pricing import to_cents


# ---------- parsing ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20.00", 2000),      # Shopify's decimal strings
        (13.8, 1380),         # JSON-LD bare floats
        (22, 2200),           # ...and bare ints
        ("1,299.99", 129999), # thousands separator
        ("  20.00  ", 2000),
    ],
)
def test_real_retailer_price_shapes_parse(raw, expected):
    assert to_cents(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "free", "N/A", -5, float("nan"), True])
def test_an_unreadable_price_is_none_not_zero(raw):
    """Zero would be a 100% discount. Absent is absent."""
    assert to_cents(raw) is None


def test_a_price_needs_a_currency_to_be_stored():
    from src.checkers.base import CheckResult, Status
    from datetime import datetime, timezone

    with pytest.raises(ValueError):
        CheckResult(Status.IN_STOCK, datetime.now(timezone.utc), price_cents=2000)


# ---------- discounts ----------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    c.execute(
        "INSERT INTO products (id, retailer, product_name, url, url_quality, source)"
        " VALUES (1, 'Gee Beauty', 'Thigh Rescue', 'http://x.test', 'pdp', 'automated')"
    )
    return c


def priced(conn, at, price, list_price=None, currency="CAD", status="IN_STOCK"):
    db.record_check(conn, 1, status, at, None, price, list_price, currency)


def test_a_retailer_advertised_sale_is_reported_as_one(conn):
    priced(conn, "2026-09-15T00:00:00+00:00", 1500, list_price=2000)
    (d,) = pricing.current_discounts(conn)
    assert d.kind == "on_sale"
    assert d.is_retailer_advertised
    assert d.percent_off == 25


def test_a_compare_at_equal_to_the_price_is_not_a_sale(conn):
    """Shopify's way of saying 'not on sale' -- not a 0% discount."""
    priced(conn, "2026-09-15T00:00:00+00:00", 2000, list_price=2000)
    assert pricing.current_discounts(conn) == []


def test_a_quiet_price_drop_is_caught_without_the_retailer_saying_so(conn):
    """The MAP case: discounted, but not marked as a sale."""
    priced(conn, "2026-09-08T00:00:00+00:00", 2000)
    priced(conn, "2026-09-15T00:00:00+00:00", 1600)
    (d,) = pricing.current_discounts(conn)
    assert d.kind == "price_drop"
    assert not d.is_retailer_advertised
    assert d.percent_off == 20


def test_a_price_going_up_is_not_a_discount(conn):
    priced(conn, "2026-09-08T00:00:00+00:00", 1600)
    priced(conn, "2026-09-15T00:00:00+00:00", 2000)
    assert pricing.current_discounts(conn) == []


def test_a_steady_price_is_not_a_discount(conn):
    priced(conn, "2026-09-08T00:00:00+00:00", 2000)
    priced(conn, "2026-09-15T00:00:00+00:00", 2000)
    assert pricing.current_discounts(conn) == []


def test_a_currency_switch_is_not_a_ninety_nine_percent_discount(conn):
    """2000 CAD then 1400 EUR is a different unit, not a markdown."""
    priced(conn, "2026-09-08T00:00:00+00:00", 2000, currency="CAD")
    priced(conn, "2026-09-15T00:00:00+00:00", 1400, currency="EUR")
    assert pricing.current_discounts(conn) == []


def test_an_advertised_sale_wins_over_the_drop_it_also_caused(conn):
    """One product, one row -- the retailer's own claim is the stronger one."""
    priced(conn, "2026-09-08T00:00:00+00:00", 2000)
    priced(conn, "2026-09-15T00:00:00+00:00", 1500, list_price=2000)
    (d,) = pricing.current_discounts(conn)
    assert d.kind == "on_sale"


def test_unpriced_history_cannot_produce_a_discount(conn):
    """Every check before today's migration has a NULL price."""
    db.record_check(conn, 1, "IN_STOCK", "2026-09-08T00:00:00+00:00")
    priced(conn, "2026-09-15T00:00:00+00:00", 1600)
    assert pricing.current_discounts(conn) == []


def test_an_out_of_stock_product_can_still_be_discounted(conn):
    """Sold out and marked down is a real state, and worth seeing."""
    priced(conn, "2026-09-15T00:00:00+00:00", 1500, list_price=2000, status="OOS")
    assert len(pricing.current_discounts(conn)) == 1


def test_prices_format_with_their_currency():
    assert pricing.format_price(1380, "EUR") == "EUR 13.80"
    assert pricing.format_price(None, "EUR") == "—"
