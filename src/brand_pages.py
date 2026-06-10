"""Brand-page reconciliation pass.

Runs after per-PDP checks. For each retailer with a usable brand page:
1. Scrape the page to extract a list of currently-listed Megababe products.
2. Downgrade this run's `ERROR` rows whose product is *not* on the brand
   page to `OOS` with note `"reconciled-via-brand-page; PDP not found"`.
   Several retailers (Anthropologie, ASOS, Cult Beauty, Nordstrom)
   delist OOS PDPs entirely instead of showing a "sold out" message,
   so a 404'd / category-page-quality URL ERROR very often actually
   represents OOS.
3. Detect tiles on the brand page that match no `products.csv` row —
   surface as `new_products` rows for the user to triage.

Skipped: retailers without a usable brand page (Boots — brand page
loads but tile state doesn't reveal in-stock vs OOS) and retailers
whose brand pages we can't fetch (Anthropologie's PerimeterX block,
ASOS's Akamai block — these are still attempted, just so the failure
shows up in the run summary).

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
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

from .checkers.base import Product
from .db import update_check, upsert_new_product

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

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class BrandTile:
    """One product as it appears on a retailer's brand page."""
    raw_title: str        # full marketing-y name, e.g. "Megababe Bidet Bar 127g"
    url: str | None       # PDP url linked from the tile, if extractable


@dataclass
class ScrapeResult:
    retailer: str
    tiles: list[BrandTile] = field(default_factory=list)
    error: str | None = None  # set when the brand page fetch / parse failed


@dataclass
class ReconcileSummary:
    downgraded: int = 0       # ERROR -> OOS rows
    unchanged: int = 0        # ERROR rows whose product still appears on brand page
    skipped_no_scraper: int = 0   # ERROR rows for retailers without a brand-page scraper
    new_products: int = 0
    fetch_errors: dict[str, str] = field(default_factory=dict)
    per_retailer_tiles: dict[str, int] = field(default_factory=dict)


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


def _is_on_brand_page(product_name: str, normalized_brand_names: set[str]) -> bool:
    """The CSV row's product is considered "still on the brand page" if
    its normalized name OR its Mini-stripped form appears as a substring
    of any normalized brand-page name."""
    norm = _normalize_name(product_name)
    if not norm:
        return False
    candidates = {norm, _strip_mini(norm)}
    return any(c and any(c in bn for bn in normalized_brand_names) for c in candidates)


def _is_in_csv(tile_norm: str, csv_norms: set[str]) -> bool:
    """A brand-page tile is considered already-known if any normalized
    products.csv name (for the same retailer) is a substring of the
    tile's normalized name."""
    return any(c and c in tile_norm for c in csv_norms)


# ---------- scrapers ----------


def scrape_cult_beauty(client: httpx.Client) -> ScrapeResult:
    """Cult Beauty: brand page returns full HTML to httpx (recon-confirmed,
    no anti-bot). Each product card has a `title=` or `alt=` attribute
    containing the full marketing name 'Megababe ...'."""
    res = ScrapeResult(retailer="Cult Beauty")
    try:
        r = client.get(BRAND_PAGE_URLS["Cult Beauty"])
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
        r = client.get(url)
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


_ANTHRO_EXTRACT_JS = """() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/shop/"]')) {
    const href = a.getAttribute('href') || '';
    if (!href.includes('/shop/megababe-')) continue;
    const card = a.closest('article') || a.closest('[class*="product"]') || a;
    const heading = card.querySelector('h2, h3, [class*="name"], [class*="title"]');
    const text = ((heading ? heading.innerText : a.innerText) || '').replace(/\\s+/g, ' ').trim();
    if (!text || text.length > 200) continue;
    if (seen.has(text.toLowerCase())) continue;
    seen.add(text.toLowerCase());
    const absUrl = a.href || ('https://www.anthropologie.com' + href);
    out.push({ raw_title: text, url: absUrl.split('?')[0] });
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


def scrape_anthropologie(browser) -> ScrapeResult:
    """Best-effort: PerimeterX is expected to block default Chromium.
    Logs the failure cleanly so the run summary shows it."""
    return _scrape_via_playwright(
        "Anthropologie",
        BRAND_PAGE_URLS["Anthropologie"],
        _ANTHRO_EXTRACT_JS,
        browser,
    )


def scrape_asos(client: httpx.Client) -> ScrapeResult:
    """Best-effort httpx fetch of ASOS search page. Akamai may 403."""
    res = ScrapeResult(retailer="ASOS")
    try:
        r = client.get(BRAND_PAGE_URLS["ASOS"])
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
    "Cult Beauty": scrape_cult_beauty,
    "Gee Beauty":  scrape_gee_beauty,
    "ASOS":        scrape_asos,
}
PLAYWRIGHT_SCRAPERS: dict[str, Callable[[object], ScrapeResult]] = {
    "Nordstrom":     scrape_nordstrom,
    "Anthropologie": scrape_anthropologie,
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
        headers={"User-Agent": _UA, "Accept": "text/html,*/*"},
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
