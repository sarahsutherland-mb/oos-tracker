"""What the weekly Slack alert says, and when it stays quiet.

The quiet case is the one worth pinning: a weekly "nothing changed" ping
trains people to ignore the channel, which costs the alerts that matter.

Run with: .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import sqlite3

import pytest

from src import db, notify
from src.notify import Transition


def t(name="Thigh Rescue", retailer="Target", prev="IN_STOCK", curr="OOS"):
    return Transition(name, retailer, prev, curr)


def test_no_changes_means_no_message():
    assert notify.format_message([]) is None


def test_a_product_going_oos_is_named_with_its_retailer():
    msg = notify.format_message([t()])
    assert "1 went OOS" in msg
    assert "Thigh Rescue · Target — went out of stock" in msg


def test_counts_separate_oos_from_recoveries_and_errors():
    msg = notify.format_message([
        t(curr="OOS"),
        t(name="Bust Dust", prev="OOS", curr="IN_STOCK"),
        t(name="Rosy Pits", prev="IN_STOCK", curr="ERROR"),
    ])
    assert "1 went OOS" in msg
    assert "1 back in stock" in msg
    assert "1 now erroring" in msg


def test_a_reconciled_error_is_not_double_counted_as_newly_oos():
    """ERROR -> OOS is the brand-page pass relabelling a failure it explained.

    The product didn't just go out of stock, so it doesn't inflate the OOS
    headline -- but it still gets its line, because the status did move.
    """
    msg = notify.format_message([t(prev="ERROR", curr="OOS")])
    assert "went OOS" not in msg
    assert "now out of stock" in msg


def test_an_unmapped_transition_falls_back_to_the_raw_states():
    msg = notify.format_message([t(prev="UNKNOWN", curr="ERROR")])
    assert "UNKNOWN → ERROR" in msg


def test_the_dashboard_link_is_included_when_configured():
    assert "https://x.test/board" in notify.format_message([t()], "https://x.test/board")
    assert "|Open the dashboard>" in notify.format_message([t()], "https://x.test/board")


# ---------- the query ----------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    c.execute(
        "INSERT INTO products (id, retailer, product_name, url, url_quality, source)"
        " VALUES (1, 'Target', 'Thigh Rescue', 'http://x.test', 'pdp', 'manual')"
    )
    return c


def check(conn, status, at):
    db.record_check(conn, 1, status, at, None)


def test_only_this_run_is_announced(conn):
    """Last week's changes stay last week's -- the window is the run."""
    check(conn, "IN_STOCK", "2026-09-01T00:00:00+00:00")
    check(conn, "OOS", "2026-09-08T00:00:00+00:00")  # last week's news
    check(conn, "OOS", "2026-09-15T00:00:00+00:00")  # this run, unchanged

    assert notify.transitions_since(conn, "2026-09-15T00:00:00+00:00") == []


def test_a_change_in_this_run_is_reported_with_its_previous_status(conn):
    check(conn, "IN_STOCK", "2026-09-08T00:00:00+00:00")
    check(conn, "OOS", "2026-09-15T00:00:00+00:00")

    got = notify.transitions_since(conn, "2026-09-15T00:00:00+00:00")
    assert got == [Transition("Thigh Rescue", "Target", "IN_STOCK", "OOS")]


def test_a_products_first_ever_check_is_not_a_transition(conn):
    """A new SKU isn't 'now out of stock', it's just newly tracked."""
    check(conn, "OOS", "2026-09-15T00:00:00+00:00")
    assert notify.transitions_since(conn, "2026-09-15T00:00:00+00:00") == []


# ---------- a retailer blocking us wholesale ----------


def test_a_retailer_wide_failure_collapses_to_one_line():
    """Nordstrom blocking all 25 SKUs is one fact, not 25 bullet points."""
    msg = notify.format_message(
        [t(name=f"Product {i}", retailer="Nordstrom", curr="ERROR") for i in range(25)]
    )
    assert "*Nordstrom* — 25 checks failing across the retailer" in msg
    assert "Product 7 · Nordstrom" not in msg
    assert msg.count("\n") == 1  # headline + the single collapsed line


def test_real_stock_changes_survive_alongside_a_collapsed_retailer():
    """The point of collapsing: the news stays readable."""
    msg = notify.format_message(
        [t(name=f"P{i}", retailer="Nordstrom", curr="ERROR") for i in range(10)]
        + [t(name="Bust Dust", retailer="Gee Beauty", prev="OOS", curr="IN_STOCK")]
    )
    assert "*Nordstrom* — 10 checks failing" in msg
    assert "Bust Dust · Gee Beauty — back in stock" in msg


def test_a_few_failures_are_still_listed_individually():
    """Nordstrom's normal 1-4 flaky ERRORs stay legible as products."""
    msg = notify.format_message(
        [t(name=f"P{i}", retailer="Nordstrom", curr="ERROR") for i in range(3)]
    )
    assert "checks failing across the retailer" not in msg
    assert "P1 · Nordstrom" in msg
