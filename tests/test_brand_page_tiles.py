"""Brand-page tile parsing, and the guard that consumes the tile count.

Anthropologie is authoritative: its brand page alone decides stock for 25
SKUs. That makes the tile count load-bearing in a way it isn't elsewhere --
`reconcile` refuses to record anything when fewer tiles parse than the page
says it lists, so an undercount doesn't show up as a wrong answer, it shows
up as no answer at all.

That is exactly what happened between 2026-08-05 and 2026-09-23: two
distinct products share the name "Megababe Daily Deodorant", the dedupe
keyed on name, and 18 tiles against a page announcing 19 silently skipped
the retailer on every run.

Run with: .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import httpx
import pytest

from src import brand_pages as bp


def page(*products: tuple[str, str], total: int | None = None) -> str:
    """A brand page carrying an ItemList of (name, url) pairs."""
    items = ",".join(
        f'{{"@type":"ListItem","item":{{"@type":"Product","name":"{n}",'
        f'"url":"{u}","offers":{{"@type":"Offer","price":22,'
        f'"priceCurrency":"USD"}}}}}}'
        for n, u in products
    )
    count = f"<span>{total} products</span>" if total is not None else ""
    return (
        "<html><body>" + count +
        '<script type="application/ld+json">'
        f'{{"@type":"ItemList","itemListElement":[{items}]}}'
        "</script></body></html>"
    )


def client_serving(html: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))
    )


def test_two_products_sharing_a_name_are_two_tiles():
    """The real Anthropologie case. Different URLs means different products."""
    html = page(
        ("Megababe Daily Deodorant", "https://a.test/shop/megababe-daily-deodorant"),
        ("Megababe Daily Deodorant", "https://a.test/shop/megababe-daily-deodorant2"),
        total=2,
    )
    res = bp.scrape_anthropologie(client_serving(html))
    assert len(res.tiles) == 2
    assert res.reported_total == 2


def test_the_same_product_listed_twice_is_one_tile():
    """Same URL is genuinely the same listing, and should still collapse."""
    html = page(
        ("Megababe Thigh Rescue", "https://a.test/shop/megababe-thigh-rescue"),
        ("Megababe Thigh Rescue", "https://a.test/shop/megababe-thigh-rescue"),
        total=1,
    )
    res = bp.scrape_anthropologie(client_serving(html))
    assert len(res.tiles) == 1


def test_tiles_carry_the_price_the_listing_advertised():
    html = page(("Megababe Le Tush Mask", "https://a.test/shop/x"), total=1)
    (tile,) = bp.scrape_anthropologie(client_serving(html)).tiles
    assert tile.price_cents == 2200
    assert tile.currency == "USD"


def test_a_price_without_a_currency_is_not_stored():
    """An unlabelled number invites a comparison that shouldn't be made."""
    html = (
        '<html><body><span>1 products</span>'
        '<script type="application/ld+json">'
        '{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":'
        '{"@type":"Product","name":"Megababe X","url":"https://a.test/x",'
        '"offers":{"@type":"Offer","price":22}}}]}'
        "</script></body></html>"
    )
    (tile,) = bp.scrape_anthropologie(client_serving(html)).tiles
    assert tile.price_cents is None
    assert tile.currency is None


def test_a_genuinely_short_scrape_still_reports_a_shortfall():
    """The guard must keep working -- this is what protects 25 SKUs."""
    html = page(("Megababe One", "https://a.test/1"), total=19)
    res = bp.scrape_anthropologie(client_serving(html))
    assert res.reported_total == 19
    assert len(res.tiles) == 1
    assert len(res.tiles) < res.reported_total  # reconcile() will refuse
