"""Target's stock rule, and the failures that must not read as OOS.

The rule (the user's, 2026-10-07): available online means in stock, whatever
the stores say. The expensive wrong answer is a failed API call reported as
25 delistings.

Run with: .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import httpx
import pytest

from src.checkers.base import Product, Status
from src.checkers.target import TargetChecker, judge


def summary(tcin="1", ship="IN_STOCK", stores_out=False, sold_out=False):
    return {
        "tcin": tcin,
        "fulfillment": {
            "sold_out": sold_out,
            "is_out_of_stock_in_all_store_locations": stores_out,
            "shipping_options": {"availability_status": ship},
        },
    }


@pytest.mark.parametrize(
    "ship,stores_out,sold_out,expected",
    [
        ("IN_STOCK", False, False, Status.IN_STOCK),
        ("IN_STOCK", True, False, Status.IN_STOCK),      # online only
        ("OUT_OF_STOCK", False, False, Status.OOS),      # stores only still OOS
        ("OUT_OF_STOCK", True, False, Status.OOS),
        ("UNAVAILABLE", True, False, Status.OOS),
        ("OUT_OF_STOCK", True, True, Status.OOS),
        ("IN_STOCK", False, True, Status.OOS),           # sold_out wins
        (None, None, False, Status.UNKNOWN),
    ],
)
def test_judge(ship, stores_out, sold_out, expected):
    status, note = judge(summary(ship=ship, stores_out=stores_out, sold_out=sold_out))
    assert status is expected
    assert "online=" in note


def test_partial_state_is_visible_in_note():
    _, note = judge(summary(ship="IN_STOCK", stores_out=True))
    assert note == "online=IN_STOCK; stores=all out"


def product(tcin: str, pid: int = 1) -> Product:
    return Product(pid, "Target", f"P{tcin}", f"https://www.target.com/p/x/-/A-{tcin}", "pdp", "automated")


def checker_serving(api_status: int, summaries: list[dict]) -> tuple[TargetChecker, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "www.target.com":
            return httpx.Response(200, text='"apiKey\\":\\"' + "a" * 40 + '\\"')
        return httpx.Response(api_status, json={"data": {"product_summaries": summaries}})

    prods = [product("111", 1), product("222", 2)]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TargetChecker(prods, client=client), calls


def test_missing_tcin_is_delisted_oos():
    ck, _ = checker_serving(200, [summary(tcin="111")])
    assert ck.check(product("111")).status is Status.IN_STOCK
    r = ck.check(product("222"))
    assert r.status is Status.OOS and "delisted" in r.notes


def test_one_api_request_for_all_products():
    ck, calls = checker_serving(200, [summary(tcin="111"), summary(tcin="222")])
    ck.check(product("111"))
    ck.check(product("222"))
    assert calls.count("redsky.target.com") == 1


@pytest.mark.parametrize("api_status,summaries", [(403, []), (200, [])])
def test_failed_or_empty_response_is_error_not_oos(api_status, summaries):
    ck, _ = checker_serving(api_status, summaries)
    for tcin in ("111", "222"):
        assert ck.check(product(tcin)).status is Status.ERROR
