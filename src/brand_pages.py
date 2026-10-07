"""Brand-page reconciliation pass.

Runs after per-PDP checks. For each retailer with a usable brand page:
0. For retailers in `BRAND_PAGE_AUTHORITATIVE`, write a status for *every*
   product from the brand page alone (listed = IN_STOCK, absent = OOS).
   These retailers have no per-PDP check to reconcile — the brand page is
   the whole signal, so this replaces both a PDP fetch and a manual sheet
   entry. Anthropologie is the only one today.
1. Scrape the page to extract a list of currently-listed Megababe products.
2. Downgrade this run's `ERROR` rows whose product is *not* on the brand
   page to `OOS` with note `"reconciled-via-brand-page; PDP not found"`.
   Several retailers (ASOS, Cult Beauty, Nordstrom) delist OOS PDPs
   entirely instead of showing a "sold out" message, so a 404'd /
   category-page-quality URL ERROR very often actually represents OOS.
3. Detect tiles on the brand page that match no `products.csv` row —
   surface as `new_products` rows for the user to triage.

Skipped: retailers without a usable brand page (Boots — brand page
loads but tile state doesn't reveal in-stock vs OOS) and retailers
whose brand pages we can't fetch (ASOS's Akamai block — still attempted,
just so the failure shows up in the run summary).

Anthropologie used to be listed as unfetchable here too. Re-tested
2026-08-05: it fetches reliably over httpx provided an `Accept-Language`
header is sent (see `_BROWSER_HEADERS`), though Playwright still 403s.

Matching strategy:
- All names are normalized via `_normalize_name` before comparison:
  strip "Megababe " prefix, strip parenthetical qualifiers (e.g.,
  "(Various Sizes)"), strip trailing size suffixes (60g, 23g, 1.7 oz),
  collapse whitespace, casefold.
- `is_on_brand_page(product_name, brand_names)`: products.csv name is
  on brand page if its normalized form is a substring of any
  normalized brand-page name. Also tries the name with "Mini" or size
  suffix stripped to handle Cult Beauty's shared-PDP "(Various
  Sizes)" tiles where neither "Thigh Rescue 60g" nor "Thigh Rescue
  Mini 23g" matches the tile literally.
- New product detection: for each brand-page tile, check if any
  products.csv row's normalized name is a substring of the tile's
  normalized name. None matching → new product candidate.

KNOWN LIMITATION — Mini variants on shared multi-size PDPs. When a
retailer's brand page shows a single tile for a multi-size product
(Cult Beauty "Megababe Thigh Rescue (Various Sizes)"), both rows in
products.csv ("Thigh Rescue 60g" + "Thigh Rescue Mini 23g") match it
via the same substring. So the brand-page check alone can't determine
*which size* is OOS — only that the product line as a whole is still
listed. Per-variant OOS detection is a job for the per-PDP checker
(via JSON-LD `hasVariant[]`), not this reconciliation pass.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

from .checkers.base import Product
from .db import record_check, update_check, upsert_new_product
from .pricing import to_cents

# Brand-page URLs by retailer (source of truth: RETAILER_KNOWLEDGE.md).
BRAND_PAGE_URLS = {
    "Cult Beauty": "https://www.cultbeauty.com/c/brands/megababe/shop-all/",
    "Nordstrom":   "https://www.nordstrom.com/brands/megababe--20023",
    "Goop":        "https://goop.com/megababe/c/?country=USA&sort=recommended",
    "Gee Beauty":  "https://geebeauty.ca/collections/megababe",
    "Anthropologie": "https://www.anthropologie.com/brands/megababe",
    "ASOS":        "https://www.asos.com/search/?q=megababe",
}

# Retailers we deliberately skip: brand page exists but provides no
# useful signal for reconciliation (Boots), or fully manual (Target,
# Walmart, CVS — handled by the manual sheet).
SKIP_RETAILERS = {"Boots", "Target", "Walmart", "CVS"}

# Retailers where the brand page is the *complete* stock signal, not just a
# tie-breaker for ERROR rows: they delist a PDP outright when it goes OOS,
# so listed == in stock and absent == OOS for every SKU we track. These get
# a status written for every product from the brand page alone — no per-PDP
# fetch and no manual sheet entry.
#
# Anthropologie qualifies (confirmed by the user: the PDP disappears rather
# than showing a sold-out state) and its brand page reports its own total,
# so we can tell a real empty catalog from a partial scrape.
#
# ASOS's notes say absence is authoritative there too, but its brand page
# is still Akamai-blocked (403/timeout as of 2026-08-05) so it stays manual.
# Empty since 2026-10-07: Anthropologie's brand page 403s from GitHub's
# runners, so it went back to the manual sheet. Re-add it here (and to
# HTTPX_SCRAPERS) if the job ever runs from somewhere it isn't blocked.
BRAND_PAGE_AUTHORITATIVE: set[str] = set()

# Guard against writing a wholesale "everything is OOS" run off a scrape
# that technically succeeded but came back suspiciously thin (edge served a
# stub, DOM changed, catalog genuinely emptied). Below this tile count we
# record nothing for an authoritative retailer and report it instead.
_MIN_AUTHORITATIVE_TILES = 1

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Header set for every httpx brand-page fetch.
#
# `Accept-Language` is LOAD-BEARING, not decoration — do not drop it.
# Anthropologie's PerimeterX edge returns a 780-byte HTTP 403 challenge to
# a request without it and the full ~850 KB page with it, on otherwise
# identical requests (measured 2026-08-05, both UA 124 and 126). A
# real browser always sends the header; requests missing it look automated.
# This is most likely why the 2026-04-30 recon recorded Anthropologie as
# hard-blocked to httpx.
_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class BrandTile:
    """One product as it appears on a retailer's brand page."""
    raw_title: str        # full marketing-y name, e.g. "Megababe Bidet Bar 127g"
    url: str | None       # PDP url linked from the tile, if extractable
    # Anthropologie's ItemList publishes `offers.price` per tile, so an
    # authoritative retailer gets its price from the same single request that
    # decides its stock — no extra fetch. None for tiles without one, and for
    # every retailer whose brand page doesn't carry prices.
    price_cents: int | None = None
    currency: str | None = None


@dataclass
class ScrapeResult:
    retailer: str
    tiles: list[BrandTile] = field(default_factory=list)
    error: str | None = None  # set when the brand page fetch / parse failed
    # The count the page reports for itself ("6 products"), when it states one.
    # Lets the authoritative pass tell "this catalog really has 6 items" from
    # "we only managed to parse 1 of them" — the difference between a correct
    # run and silently marking two dozen SKUs OOS.
    reported_total: int | None = None


@dataclass
class ReconcileSummary:
    downgraded: int = 0       # ERROR -> OOS rows
    unchanged: int = 0        # ERROR rows whose product still appears on brand page
    skipped_no_scraper: int = 0   # ERROR rows for retailers without a brand-page scraper
    new_products: int = 0
    fetch_errors: dict[str, str] = field(default_factory=dict)
    per_retailer_tiles: dict[str, int] = field(default_factory=dict)
    # Statuses written by the BRAND_PAGE_AUTHORITATIVE pass, per status name,
    # so the caller can fold them into the run's own tally.
    authoritative_counts: Counter[str] = field(default_factory=Counter)
    # Retailers that are authoritative but whose scrape was unusable, so no
    # status was written and their last known values still stand.
    authoritative_skipped: dict[str, str] = field(default_factory=dict)


# ---------- name normalization & matching ----------

_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")
_SIZE_RE = re.compile(
    r"\s+\d+(?:\.\d+)?\s?(?:oz|ml|g|fl\s*oz)\b", re.IGNORECASE
)
_TRAILING_MINI_SIZE_RE = re.compile(
    r"\bMini\s+\d+(?:\.\d+)?\s?(?:oz|ml|g)\b", re.IGNORECASE
)


def _normalize_name(s: str) -> str:
    """Casefold + strip Megababe brand prefix, parenthetical qualifiers,
    trailing size suffixes, and combining accents (so "Après" matches
    "Apres"). Collapses whitespace. Used for both sides of substring
    matching."""
    s = (s or "").strip()
    # NFD-decompose then drop combining marks → "Après" → "Apres".
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    s = re.sub(r"^Megababe\s+", "", s, flags=re.IGNORECASE)
    # "Thigh Rescue Mini 23g" -> "Thigh Rescue Mini"
    s = _TRAILING_MINI_SIZE_RE.sub("Mini", s)
    s = _PARENS_RE.sub(" ", s)
    s = _SIZE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s


def _strip_mini(s: str) -> str:
    """Strip a trailing ' mini' from a normalized name."""
    return re.sub(r"\s+mini\s*$", "", s, flags=re.IGNORECASE).strip()


def _tokens(s: str) -> frozenset[str]:
    """Split a normalized name into a set of alphanumeric word tokens.

    Splitting on non-alphanumerics (rather than whitespace) so that
    hyphenated marketing names decompose the same way the plainer
    products.csv names do: "after-shave" -> {after, shave}.
    """
    return frozenset(t for t in re.split(r"[^a-z0-9]+", s) if t)


def _is_on_brand_page(product_name: str, normalized_brand_names: set[str]) -> bool:
    """Is this products.csv row still listed on the retailer's brand page?

    Matches on *token subset* rather than substring: every word of the
    products.csv name must appear somewhere in the brand-page tile's name.
    Substring matching failed in both directions on real data —

    - too strict: "Apres Shave Oil" is not a contiguous substring of the
      tile "Apres Shave Soothing After-Shave Oil", so a listed product
      read as OOS;
    - too loose: "Thigh Rescue" *is* a substring of the tile "Thigh
      Rescue Mini", so a delisted full-size product read as IN_STOCK by
      matching its own Mini variant — a false in-stock, which is the
      direction that lets a PO through against stock we don't have.

    The Mini rule is deliberately **asymmetric**:

    - a tile that says Mini cannot satisfy a products.csv row that
      doesn't (fixes the false-IN_STOCK case above);
    - a products.csv row that says Mini *can* still match a non-Mini
      tile, via its Mini-stripped form. That preserves the documented
      Cult Beauty behaviour where one "(Various Sizes)" tile legitimately
      covers both "Thigh Rescue 60g" and "Thigh Rescue Mini 23g" (see
      the KNOWN LIMITATION note in this module's docstring).
    """
    norm = _normalize_name(product_name)
    if not norm:
        return False
    csv_toks = _tokens(norm)
    if not csv_toks:
        return False
    csv_is_mini = "mini" in csv_toks

    for bn in normalized_brand_names:
        tile_toks = _tokens(bn)
        if not tile_toks:
            continue
        if "mini" in tile_toks and not csv_is_mini:
            continue
        if csv_toks <= tile_toks:
            return True
        # Mini row vs non-Mini (shared multi-size) tile: retry without
        # the size qualifier.
        if csv_is_mini and "mini" not in tile_toks:
            if _tokens(_strip_mini(norm)) <= tile_toks:
                return True
    return False


def _matching_priced_tile(
    product_name: str, priced_tiles: dict[str, "BrandTile"]
) -> "BrandTile | None":
    """The tile a product matched, when that tile carried a price.

    Runs `_is_on_brand_page` against one tile at a time so the price is
    attributed by exactly the rule that decided the product was in stock --
    a looser match here could price a Mini from its full-size tile.
    """
    for norm, tile in priced_tiles.items():
        if _is_on_brand_page(product_name, {norm}):
            return tile
    return None


def _is_in_csv(tile_norm: str, csv_norms: set[str]) -> bool:
    """Is this brand-page tile already represented in products.csv?

    Same token-subset test as `_is_on_brand_page` (order-independent, so
    it catches word-order and hyphenation differences that substring
    matching missed), but without the Mini asymmetry — for "do we already
    know about this tile?" a Mini tile matching a base row is fine, and
    being permissive here just avoids spurious new-product alerts.
    """
    tile_toks = _tokens(tile_norm)
    if not tile_toks:
        return False
    return any(c and _tokens(c) <= tile_toks for c in csv_norms)


# ---------- JSON-LD helpers ----------

_LDJSON_RE = re.compile(
    r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _iter_jsonld(html: str):
    """Yield every parseable JSON-LD block on the page.

    Deliberately lenient: a retailer shipping one malformed block should
    not cost us the others (`checkers/_json_ld.find_product_node` takes the
    same approach, but it stops at the first Product node — here we need to
    walk whole lists).
    """
    for raw in _LDJSON_RE.findall(html):
        try:
            yield json.loads(raw.strip())
        except json.JSONDecodeError:
            continue


def _iter_nodes(block: object):
    """Flatten a JSON-LD block into the dicts it contains.

    Blocks arrive as either a bare object, a list of objects, or an
    `@graph` wrapper depending on the retailer, so normalize all three.
    """
    if isinstance(block, list):
        for entry in block:
            yield from _iter_nodes(entry)
    elif isinstance(block, dict):
        yield block
        graph = block.get("@graph")
        if isinstance(graph, list):
            for entry in graph:
                yield from _iter_nodes(entry)


# ---------- fetching ----------

# Anthropologie's PerimeterX edge challenges the *first* request of a fresh
# session with a 403 and serves the real page to everything after it —
# measured 2026-09-23: attempt 1 403, attempts 2-7 all 200. A weekly cron on
# a cold runner makes that first request the whole run, so a bare `client.get`
# turns a working scraper into 25 silently missing SKUs. Anthropologie is
# authoritative (its brand page IS the stock signal, there is no PDP fallback
# and no sheet row any more), so that gap is invisible rather than loud.
#
# Retrying on the same client is what fixes it: httpx keeps the cookie jar
# across requests, so attempt 2 carries whatever the challenge set.
#
# Deliberately short and few. This is a cold-start challenge, not a rate
# limit — the Shopify checker handles that case separately with a shared
# 60s cooldown (see checkers/shopify.py). Waiting minutes here would just
# delay a genuine outage.
_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
_RETRY_BACKOFF = (2.0, 6.0)  # seconds before attempt 2, then attempt 3


def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response:
    """GET `url`, retrying a challenge/transport failure on the same client.

    Returns the last response even if it still failed, so the caller's
    `raise_for_status()` reports the real status. Raises the last transport
    error if every attempt failed to get a response at all.
    """
    last_exc: httpx.HTTPError | None = None
    last_res: httpx.Response | None = None

    for attempt, pause in enumerate((*_RETRY_BACKOFF, None)):
        try:
            last_res = client.get(url)
            last_exc = None
            if last_res.status_code not in _RETRY_STATUSES:
                return last_res
        except httpx.HTTPError as e:
            last_exc = e
        if pause is None:
            break
        time.sleep(pause)

    if last_res is not None:
        return last_res
    assert last_exc is not None  # loop runs at least once
    raise last_exc


# ---------- scrapers ----------


def scrape_cult_beauty(client: httpx.Client) -> ScrapeResult:
    """Cult Beauty: brand page returns full HTML to httpx (recon-confirmed,
    no anti-bot). Each product card has a `title=` or `alt=` attribute
    containing the full marketing name 'Megababe ...'."""
    res = ScrapeResult(retailer="Cult Beauty")
    try:
        r = _get_with_retry(client, BRAND_PAGE_URLS["Cult Beauty"])
        r.raise_for_status()
    except httpx.HTTPError as e:
        res.error = f"fetch failed: {e}"
        return res

    titles = re.findall(r'(?:title|alt)="(Megababe[^"]+)"', r.text)
    seen: set[str] = set()
    for t in titles:
        if t in seen:
            continue
        seen.add(t)
        # PDP url near the same anchor — best-effort
        m = re.search(
            rf'href="(/p/[^"#]+/)"[^>]*>[^<]*?{re.escape(t)}',
            r.text,
            re.IGNORECASE,
        )
        url = ("https://www.cultbeauty.com" + m.group(1)) if m else None
        res.tiles.append(BrandTile(raw_title=t, url=url))
    return res


def scrape_gee_beauty(client: httpx.Client) -> ScrapeResult:
    """Gee Beauty: standard Shopify, can use the public collections JSON."""
    res = ScrapeResult(retailer="Gee Beauty")
    url = "https://geebeauty.ca/collections/megababe/products.json?limit=250"
    try:
        r = _get_with_retry(client, url)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        res.error = f"fetch failed: {e}"
        return res
    for p in data.get("products", []):
        title = p.get("title") or ""
        handle = p.get("handle") or ""
        if not title:
            continue
        full_url = f"https://geebeauty.ca/products/{handle}" if handle else None
        res.tiles.append(BrandTile(raw_title=title, url=full_url))
    return res


def _scrape_via_playwright(
    retailer: str,
    url: str,
    extract_js: str,
    browser,
    settle_ms: int = 2500,
) -> ScrapeResult:
    """Generic Playwright fetch + JS extractor. `extract_js` should
    return an array of {raw_title, url} objects."""
    res = ScrapeResult(retailer=retailer)
    if browser is None:
        res.error = "no shared Playwright browser available"
        return res
    try:
        ctx = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
    except Exception as e:
        res.error = f"new_context failed: {e}"
        return res
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(settle_ms)

        final_url = page.url
        # Anti-bot heuristics
        body_low = page.content().lower()
        if "siteclosed.nordstrom.com" in final_url:
            res.error = "Nordstrom redirected to siteclosed/invitation"
            return res
        if "px-captcha" in body_low or "press &amp; hold" in body_low or "press & hold" in body_low:
            res.error = "PerimeterX challenge"
            return res
        if "access denied" in body_low and len(body_low) < 5_000:
            res.error = "Access Denied"
            return res

        try:
            tiles_data = page.evaluate(extract_js)
        except Exception as e:
            res.error = f"extract_js failed: {e}"
            return res
        for t in tiles_data or []:
            if isinstance(t, dict):
                raw = (t.get("raw_title") or "").strip()
                if not raw:
                    continue
                res.tiles.append(BrandTile(raw_title=raw, url=t.get("url")))
    except Exception as e:
        res.error = f"navigation failed: {e}"
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    return res


_NORDSTROM_EXTRACT_JS = """() => {
  const out = [];
  const seen = new Set();
  // Nordstrom uses anchor tags pointing to /s/<slug>/<id>; product name
  // often lives in an <h3> inside the article tile.
  for (const a of document.querySelectorAll('a[href*="/s/"]')) {
    const href = a.getAttribute('href') || '';
    if (!/\\/s\\/[a-z0-9-]+\\/\\d+/i.test(href)) continue;
    // Skip anchors that target the review section
    if (href.includes('#')) continue;
    const card = a.closest('article') || a.closest('[class*="ProductCard"]') || a;
    const heading = card.querySelector('h3, h4, [class*="ProductCard__name"]');
    const text = (heading ? heading.innerText : a.innerText) || '';
    const t = text.replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 200) continue;
    const key = t.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const absUrl = a.href || ('https://www.nordstrom.com' + href);
    out.push({ raw_title: t, url: absUrl.split('#')[0].split('?')[0] });
  }
  return out;
}"""


_GOOP_EXTRACT_JS = """() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/p/"]')) {
    const href = a.getAttribute('href') || '';
    if (!href.endsWith('/p/')) continue;
    const card = a.closest('article') || a.closest('[class*="product"]') || a;
    let title = '';
    const heading = card.querySelector('h2, h3, [class*="title"], [class*="Title"]');
    if (heading) title = heading.innerText;
    if (!title) title = a.innerText || a.getAttribute('aria-label') || '';
    title = (title || '').replace(/\\s+/g, ' ').trim();
    if (!title || title.length > 200) continue;
    if (seen.has(title.toLowerCase())) continue;
    seen.add(title.toLowerCase());
    const absUrl = a.href || ('https://goop.com' + href);
    out.push({ raw_title: title, url: absUrl });
  }
  return out;
}"""


def scrape_nordstrom(browser) -> ScrapeResult:
    return _scrape_via_playwright(
        "Nordstrom", BRAND_PAGE_URLS["Nordstrom"], _NORDSTROM_EXTRACT_JS, browser
    )


def scrape_goop(browser) -> ScrapeResult:
    return _scrape_via_playwright(
        "Goop", BRAND_PAGE_URLS["Goop"], _GOOP_EXTRACT_JS, browser
    )


def scrape_anthropologie(client: httpx.Client) -> ScrapeResult:
    """Anthropologie: httpx works, Playwright does not.

    Re-tested 2026-08-05 (the 2026-04-30 recon concluded both routes were
    PerimeterX-blocked; that is no longer true, and the two routes now
    differ). Plain httpx with a desktop UA returns the full ~850 KB page,
    8/8 attempts, while default Playwright Chromium still gets HTTP 403 —
    so this deliberately does NOT go through `_scrape_via_playwright`.

    The page embeds a JSON-LD `ItemList` whose `itemListElement[].item`
    entries are `Product` objects with `name` + `url`. That is the whole
    signal we need: Anthropologie delists a PDP entirely when it goes OOS
    (see RETAILER_KNOWLEDGE.md), so presence on this list is in-stock and
    absence is OOS — no per-PDP fetch required.

    Falls back to scraping `/shop/megababe-*` anchor hrefs if the JSON-LD
    block is missing or its shape changes.
    """
    res = ScrapeResult(retailer="Anthropologie")
    try:
        r = _get_with_retry(client, BRAND_PAGE_URLS["Anthropologie"])
        r.raise_for_status()
    except httpx.HTTPError as e:
        res.error = f"fetch failed: {e}"
        return res

    seen: set[str] = set()

    def add(
        title: str,
        url: str | None,
        offers: object = None,
    ) -> None:
        title = re.sub(r"\s+", " ", title or "").strip()
        if not title:
            return
        # Deduplicate on the URL, falling back to the name only when a tile
        # has no link. Anthropologie lists two distinct products both named
        # "Megababe Daily Deodorant" (.../megababe-daily-deodorant and
        # .../megababe-daily-deodorant2). Keying on the name collapsed them
        # into one tile, leaving 18 tiles against a page that announces 19 --
        # which the partial-scrape guard in `reconcile` reads as a broken
        # scrape and refuses, silently skipping all 25 SKUs on every run.
        key = url or title.casefold()
        if key in seen:
            return
        seen.add(key)
        cents = currency = None
        if isinstance(offers, dict):
            cents = to_cents(offers.get("price"))
            raw_currency = offers.get("priceCurrency")
            if cents is not None and isinstance(raw_currency, str) and raw_currency.strip():
                currency = raw_currency.strip().upper()
            else:
                cents = None  # a price with no currency is not storable
        res.tiles.append(
            BrandTile(raw_title=title, url=url, price_cents=cents, currency=currency)
        )

    for block in _iter_jsonld(r.text):
        for node in _iter_nodes(block):
            if node.get("@type") != "ItemList":
                continue
            for el in node.get("itemListElement") or []:
                item = el.get("item") if isinstance(el, dict) else None
                if not isinstance(item, dict) or item.get("@type") != "Product":
                    continue
                url = item.get("url")
                add(
                    item.get("name") or "",
                    url.split("?")[0] if url else None,
                    item.get("offers"),
                )

    if not res.tiles:
        # Fallback: product slugs carry the name well enough to match on.
        for slug in dict.fromkeys(
            re.findall(r'/shop/(megababe-[a-z0-9\-]+)(?:\?|")', r.text, re.I)
        ):
            add(
                slug.replace("-", " "),
                f"https://www.anthropologie.com/shop/{slug}",
            )

    if not res.tiles:
        res.error = "no product tiles parsed (PerimeterX block or DOM change)"
        return res

    # The page prints its own count ("6 products"). Capture it so the
    # authoritative pass can refuse a partial scrape.
    m = re.search(r"(\d+)\s+products?\b", r.text, re.IGNORECASE)
    if m:
        res.reported_total = int(m.group(1))
    return res


def scrape_asos(client: httpx.Client) -> ScrapeResult:
    """Best-effort httpx fetch of ASOS search page. Akamai may 403."""
    res = ScrapeResult(retailer="ASOS")
    try:
        r = _get_with_retry(client, BRAND_PAGE_URLS["ASOS"])
    except httpx.HTTPError as e:
        res.error = f"fetch failed: {e}"
        return res
    if r.status_code != 200:
        res.error = f"HTTP {r.status_code}"
        return res

    # ASOS shows products in `<article>` tiles with a product title link
    # to /<slug>/prd/<id>. Extract the link text + href.
    pattern = re.compile(
        r'href="(/[^"]*?/prd/\d+)"[^>]*>([^<]+)</a>', re.IGNORECASE
    )
    seen: set[str] = set()
    for m in pattern.finditer(r.text):
        path, label = m.group(1), m.group(2)
        title = re.sub(r"\s+", " ", label).strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        res.tiles.append(
            BrandTile(raw_title=title, url=f"https://www.asos.com{path}")
        )
    if not res.tiles:
        res.error = "no product tiles parsed (likely Akamai 403 or DOM change)"
    return res


# Map retailer -> scraper callable. httpx-based ones take a client;
# Playwright-based ones take a browser.
HTTPX_SCRAPERS: dict[str, Callable[[httpx.Client], ScrapeResult]] = {
    "Cult Beauty":   scrape_cult_beauty,
    "Gee Beauty":    scrape_gee_beauty,
    "ASOS":          scrape_asos,
    # Anthropologie and Nordstrom (below) are manual since 2026-10-07 and
    # their brand pages are blocked from GitHub's runners, so they aren't
    # scraped. scrape_anthropologie / scrape_nordstrom are kept for re-use.
}
PLAYWRIGHT_SCRAPERS: dict[str, Callable[[object], ScrapeResult]] = {
    # Goop deliberately omitted: its tile DOM puts product titles outside
    # the anchor element my generic extractor handles (recon found only
    # "quickshop" text). Goop carries 2 known SKUs and rarely changes;
    # not worth a custom extractor right now.
}


# ---------- reconciliation pass ----------


def reconcile(
    conn: sqlite3.Connection,
    products: list[Product],
    error_products: list[tuple[Product, str]],  # (product, checked_at_iso)
    browser=None,
) -> ReconcileSummary:
    """Run the brand-page reconciliation pass.

    `error_products` is the list of (product, checked_at_iso) tuples
    captured during this run's main loop for products whose check
    returned ERROR. The reconciler updates the matching `checks` row
    in place when downgrading.
    """
    summary = ReconcileSummary()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Group ERROR products by retailer for cheap dispatch
    errors_by_retailer: dict[str, list[tuple[Product, str]]] = defaultdict(list)
    for p, ts in error_products:
        errors_by_retailer[p.retailer].append((p, ts))

    # Per-retailer products.csv name lookup, normalized
    csv_norms_by_retailer: dict[str, set[str]] = defaultdict(set)
    for p in products:
        csv_norms_by_retailer[p.retailer].add(_normalize_name(p.name))

    # Scrape every retailer that has a brand page (we need this for new-
    # product detection too, even if there are no ERRORs to reconcile).
    scrape_results: dict[str, ScrapeResult] = {}
    with httpx.Client(
        headers=_BROWSER_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        for retailer, scraper in HTTPX_SCRAPERS.items():
            scrape_results[retailer] = scraper(client)
        for retailer, scraper in PLAYWRIGHT_SCRAPERS.items():
            scrape_results[retailer] = scraper(browser)

    for retailer, sr in scrape_results.items():
        if sr.error:
            summary.fetch_errors[retailer] = sr.error
        summary.per_retailer_tiles[retailer] = len(sr.tiles)

    # Pass 0: for retailers where the brand page IS the stock signal, write a
    # status for every product from the scrape alone. Runs before the ERROR
    # pass so those retailers never reach it — they have no per-PDP check to
    # reconcile in the first place.
    products_by_retailer: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        products_by_retailer[p.retailer].append(p)

    for retailer in sorted(BRAND_PAGE_AUTHORITATIVE):
        retailer_products = products_by_retailer.get(retailer, [])
        if not retailer_products:
            continue
        sr = scrape_results.get(retailer)
        if sr is None:
            summary.authoritative_skipped[retailer] = "no brand-page scraper"
            continue
        if sr.error:
            summary.authoritative_skipped[retailer] = sr.error
            continue
        if len(sr.tiles) < _MIN_AUTHORITATIVE_TILES:
            # Treat as a failed scrape, not as "the whole catalog is OOS" —
            # silently marking every SKU OOS is the expensive wrong answer.
            summary.authoritative_skipped[retailer] = (
                f"only {len(sr.tiles)} tile(s) parsed; refusing to mark "
                f"{len(retailer_products)} product(s) OOS off a thin scrape"
            )
            continue
        if sr.reported_total is not None and len(sr.tiles) < sr.reported_total:
            # The page says it lists more products than we extracted, so the
            # scrape is incomplete and every missing tile would read as OOS.
            summary.authoritative_skipped[retailer] = (
                f"partial scrape: page reports {sr.reported_total} products "
                f"but only {len(sr.tiles)} parsed"
            )
            continue

        normalized_brand_names = {_normalize_name(t.raw_title) for t in sr.tiles}
        priced_tiles = {
            _normalize_name(t.raw_title): t for t in sr.tiles if t.price_cents
        }
        for p in retailer_products:
            on_page = _is_on_brand_page(p.name, normalized_brand_names)
            status = "IN_STOCK" if on_page else "OOS"
            # A delisted product has no tile and so no price. Leaving it NULL
            # is right: we don't know what it costs now, and carrying the last
            # known price forward would make a stale number look observed.
            tile = _matching_priced_tile(p.name, priced_tiles) if on_page else None
            record_check(
                conn,
                p.id,
                status,
                now_iso,
                "brand-page authoritative: "
                + ("listed" if on_page else "not listed"),
                price_cents=tile.price_cents if tile else None,
                currency=tile.currency if tile else None,
            )
            summary.authoritative_counts[status] += 1

    # Pass 1: downgrade ERRORs whose product is missing from the brand page
    for retailer, errors in errors_by_retailer.items():
        if retailer in SKIP_RETAILERS:
            summary.skipped_no_scraper += len(errors)
            continue
        sr = scrape_results.get(retailer)
        if sr is None or sr.error:
            # Couldn't fetch — leave these ERRORs alone
            summary.skipped_no_scraper += len(errors)
            continue

        normalized_brand_names = {_normalize_name(t.raw_title) for t in sr.tiles}
        for p, checked_at_iso in errors:
            if _is_on_brand_page(p.name, normalized_brand_names):
                summary.unchanged += 1
            else:
                update_check(
                    conn,
                    p.id,
                    checked_at_iso,
                    "OOS",
                    "reconciled-via-brand-page; PDP not found",
                )
                summary.downgraded += 1

    # Pass 2: detect tiles on brand pages that match no products.csv row
    for retailer, sr in scrape_results.items():
        if sr.error or not sr.tiles:
            continue
        csv_norms = csv_norms_by_retailer.get(retailer, set())
        for tile in sr.tiles:
            tile_norm = _normalize_name(tile.raw_title)
            if not tile_norm:
                continue
            if _is_in_csv(tile_norm, csv_norms):
                continue
            inserted = upsert_new_product(
                conn,
                retailer,
                tile.raw_title,
                tile.url,
                now_iso,
            )
            if inserted:
                summary.new_products += 1

    return summary


def print_summary(summary: ReconcileSummary) -> None:
    if summary.authoritative_counts:
        detail = ", ".join(
            f"{n} {status}"
            for status, n in sorted(summary.authoritative_counts.items())
        )
        print(f"Brand-page authoritative statuses written: {detail}")
    for retailer, why in summary.authoritative_skipped.items():
        print(
            f"  {retailer}: no status written ({why}) — last known "
            f"values left in place"
        )
    print(
        f"Brand-page reconciliation: {summary.downgraded} ERRORs downgraded "
        f"to OOS, {summary.new_products} new products detected, "
        f"{summary.unchanged} ERRORs unchanged"
    )
    if summary.skipped_no_scraper:
        print(
            f"  ({summary.skipped_no_scraper} ERROR rows skipped — no usable "
            f"brand-page scraper or fetch failed)"
        )
    for retailer, err in summary.fetch_errors.items():
        print(f"  brand-page fetch failed for {retailer}: {err}")
    tiles_summary = ", ".join(
        f"{r}={n}" for r, n in sorted(summary.per_retailer_tiles.items()) if n
    )
    if tiles_summary:
        print(f"  tiles seen per retailer: {tiles_summary}")
