"""Generate the static dashboard at docs/index.html.

OOS-first layout per the brief: header, currently OOS grouped by product
name, stale manual entries, URLs to fix, recent transitions, per-retailer
summary at the bottom. Inline CSS, no JS, mobile-friendly.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment

from .db import connect, po_lines_for_dashboard, unconfirmed_new_products

OUT_PATH = Path("docs/index.html")
STALE_DAYS = 10
TRANSITION_WINDOW_DAYS = 28


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Megababe OOS Tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 920px; margin: 0 auto; padding: 0 18px 64px; color: #1a1a1a;
         background: #f7f6f4; line-height: 1.5; }

  /* Header */
  .site-header {
    background: #fff; border-bottom: 1px solid #e8e4e0;
    padding: 20px 0 16px; margin: 0 -18px 28px; padding-left: 18px; padding-right: 18px;
  }
  .header-top { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 20px; font-weight: 700; margin: 0; letter-spacing: -0.3px; color: #1a1a1a; }
  .header-pill {
    display: inline-flex; align-items: center; gap: 5px;
    background: #fde8e8; color: #b83232; font-size: 13px; font-weight: 600;
    padding: 3px 10px; border-radius: 20px;
  }
  .header-pill.all-good { background: #e6f4ec; color: #2a7a47; }
  .header-meta { margin-top: 5px; color: #999; font-size: 12px; }

  /* Sections */
  h2 { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px;
       color: #888; margin: 28px 0 12px; }
  .card { background: #fff; border: 1px solid #e8e4e0; border-radius: 10px;
          padding: 0; margin: 0 0 10px; overflow: hidden; }

  /* Typography helpers */
  .muted { color: #999; font-size: 13px; }
  .subtle { color: #aaa; font-size: 12px; }
  .src-tag { font-size: 10px; font-weight: 600; text-transform: uppercase;
             letter-spacing: 0.6px; color: #bbb; }
  .empty { color: #aaa; font-style: italic; font-size: 14px; padding: 14px 16px; }

  /* Status badges */
  .badge {
    display: inline-block; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.4px;
    padding: 2px 7px; border-radius: 4px;
  }
  .badge-OOS { background: #fde8e8; color: #b83232; }
  .badge-IN_STOCK { background: #e6f4ec; color: #2a7a47; }
  .badge-ERROR { background: #fff3cd; color: #856404; }
  .badge-UNKNOWN { background: #f0f0f0; color: #777; }
  .badge-NO_CHECK { background: #f0f0f0; color: #999; }

  /* Inline status colours (for summary tables) */
  .status-OOS { color: #b83232; font-weight: 600; }
  .status-IN_STOCK { color: #2a7a47; }
  .status-ERROR, .status-UNKNOWN { color: #856404; }
  .status-NO_CHECK { color: #aaa; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 9px 14px; border-bottom: 1px solid #f0ece8; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  th { font-weight: 600; color: #888; font-size: 12px; text-transform: uppercase;
       letter-spacing: 0.4px; background: #faf9f7; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  a { color: #c05a78; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .url-cell { word-break: break-all; font-size: 12px; }

  /* View toggle — CSS-only, no JS */
  input[name="view"] { position: absolute; left: -9999px; }
  .view-tabs {
    display: flex; flex-wrap: wrap; gap: 2px; margin: 0 0 20px;
    background: #ede9e5; padding: 3px; border-radius: 8px;
    width: fit-content; max-width: 100%;
  }
  .view-tabs label {
    padding: 6px 14px; cursor: pointer; user-select: none; border-radius: 6px;
    color: #888; font-size: 13px; font-weight: 600; transition: background 0.1s;
    white-space: nowrap;
  }
  .view-tabs label:hover { color: #555; }
  .view-content { display: none; }
  #view-retailer:checked ~ #content-retailer,
  #view-sku:checked ~ #content-sku,
  #view-summary:checked ~ #content-summary,
  #view-changes:checked ~ #content-changes,
  #view-new-products:checked ~ #content-new-products,
  #view-urls:checked ~ #content-urls,
  #view-po-check:checked ~ #content-po-check { display: block; }
  #view-retailer:checked ~ .view-tabs label[for="view-retailer"],
  #view-sku:checked ~ .view-tabs label[for="view-sku"],
  #view-summary:checked ~ .view-tabs label[for="view-summary"],
  #view-changes:checked ~ .view-tabs label[for="view-changes"],
  #view-new-products:checked ~ .view-tabs label[for="view-new-products"],
  #view-urls:checked ~ .view-tabs label[for="view-urls"],
  #view-po-check:checked ~ .view-tabs label[for="view-po-check"] {
    background: #fff; color: #1a1a1a; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  /* By-SKU view */
  .product-block { padding: 12px 16px; border-bottom: 1px solid #f0ece8; }
  .product-block:last-child { border-bottom: none; }
  .product-name { font-weight: 600; font-size: 15px; margin-bottom: 6px; }
  .retailer-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 3px; }
  .retailer-list li { font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .retailer-name { font-weight: 500; min-width: 130px; }

  /* By-retailer view */
  .retailer-block { padding: 16px; border-bottom: 1px solid #f0ece8; }
  .retailer-block:last-child { border-bottom: none; }
  .retailer-header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
  .retailer-h3 { font-size: 15px; font-weight: 700; margin: 0; }
  .retailer-counts { font-size: 13px; color: #aaa; display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }
  .retailer-counts span { display: inline-flex; align-items: center; gap: 4px; }
  .retailer-oos-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 5px; }
  .retailer-oos-list li { font-size: 14px; display: flex; align-items: center; gap: 8px; }
  details.other-products { margin-top: 12px; }
  details.other-products summary {
    cursor: pointer; outline: none; color: #999; font-size: 13px;
    user-select: none; list-style: none; display: inline-flex; align-items: center; gap: 5px;
  }
  details.other-products summary::-webkit-details-marker { display: none; }
  details.other-products summary::before {
    content: "▸"; font-size: 10px; transition: transform 0.15s;
  }
  details.other-products[open] summary::before { transform: rotate(90deg); }
  details.other-products summary:hover { color: #666; }
  details.other-products[open] summary { margin-bottom: 8px; }

  /* History matrix */
  .matrix-table th.matrix-col { text-align: center; width: 72px; }
  .matrix-table td.matrix-cell { text-align: center; padding: 7px 6px; }
  .matrix-table .badge { font-size: 10px; padding: 2px 5px; }
  .matrix-retailer-row td {
    background: #f5f2ef; font-weight: 700; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.6px; color: #555;
    padding: 8px 14px; border-bottom: 1px solid #e8e4e0;
  }
  .matrix-expand-row > td { padding: 0; border-bottom: 1px solid #e8e4e0; }
  .matrix-expand-row details.other-products { padding: 8px 14px; }
  .matrix-expand-row details.other-products summary { font-size: 12px; }
  .matrix-inner { margin-top: 6px; }
  .matrix-inner td { border-bottom: 1px solid #f7f4f1; font-size: 13px; }
  .matrix-inner tr:last-child td { border-bottom: none; }

  .footer { color: #bbb; font-size: 11px; margin-top: 40px; text-align: center; }

  @media (max-width: 600px) {
    .retailer-name { min-width: 0; }
    .header-top { flex-direction: column; gap: 6px; }
  }
</style>
</head>
<body>

<div class="site-header">
  <div class="header-top">
    <h1>Megababe OOS Tracker</h1>
    {% if oos_count > 0 %}
    <span class="header-pill">{{ oos_count }} out of stock</span>
    {% else %}
    <span class="header-pill all-good">All in stock</span>
    {% endif %}
  </div>
  <div class="header-meta">
    {% if last_run %}Last run {{ last_run }} · {% endif %}{{ total_skus }} SKUs tracked
  </div>
</div>

<input type="radio" name="view" id="view-retailer" checked>
<input type="radio" name="view" id="view-sku">
<input type="radio" name="view" id="view-summary">
<input type="radio" name="view" id="view-changes">
<input type="radio" name="view" id="view-new-products">
<input type="radio" name="view" id="view-urls">
<input type="radio" name="view" id="view-po-check">
<div class="view-tabs">
  <label for="view-retailer">By Retailer</label>
  <label for="view-sku">By SKU</label>
  <label for="view-changes">Stock History</label>
  <label for="view-summary">Per Retailer Summary</label>
  <label for="view-new-products">New Products</label>
  <label for="view-urls">URLs to Fix</label>
  <label for="view-po-check">PO Orders vs Stock</label>
</div>

<section class="view-content" id="content-sku">

<h2>Currently out of stock</h2>
{% if oos_groups %}
<div class="card">
{% for g in oos_groups %}
<div class="product-block">
  <div class="product-name">
    {{ g.name }}
    <span class="muted" style="font-weight:400; font-size:13px;">— {{ g.entries|length }} retailer{{ "s" if g.entries|length != 1 else "" }}</span>
  </div>
  <ul class="retailer-list">
  {% for e in g.entries %}
    <li>
      <span class="retailer-name">{{ e.retailer }}</span>
      <span class="subtle">since {{ e.went_oos_human }}</span>
      <span class="src-tag">{{ e.source }}</span>
    </li>
  {% endfor %}
  </ul>
</div>
{% endfor %}
</div>
{% else %}
<div class="card"><div class="empty">Nothing currently out of stock.</div></div>
{% endif %}

{% if stale_manual %}
<h2>Stale manual entries</h2>
<div class="card">
<table>
  <tr><th>Retailer</th><th>Product</th><th>Last checked</th><th>Status</th></tr>
  {% for r in stale_manual %}
  <tr>
    <td>{{ r.retailer }}</td>
    <td>{{ r.name }}</td>
    <td class="subtle">{{ r.checked_at_human }}</td>
    <td><span class="badge badge-{{ r.status }}">{{ r.status.replace("_"," ") }}</span></td>
  </tr>
  {% endfor %}
</table>
</div>
{% endif %}

</section><!-- /content-sku -->

<section class="view-content" id="content-summary">

<h2>Per-retailer summary</h2>
<div class="card">
<table>
  <tr>
    <th>Retailer</th><th class="num">SKUs</th>
    <th class="num">In stock</th><th class="num">OOS</th>
    <th class="num">Error</th><th class="num">Unknown</th>
    <th class="num">No check</th>
  </tr>
  {% for r in retailer_summary %}
  <tr>
    <td>{{ r.retailer }} <span class="src-tag">{{ r.source }}</span></td>
    <td class="num">{{ r.total }}</td>
    <td class="num {% if r.in_stock %}status-IN_STOCK{% endif %}">{{ r.in_stock }}</td>
    <td class="num {% if r.oos %}status-OOS{% endif %}">{{ r.oos if r.oos else "—" }}</td>
    <td class="num {% if r.error %}status-ERROR{% endif %}">{{ r.error if r.error else "—" }}</td>
    <td class="num">{{ r.unknown if r.unknown else "—" }}</td>
    <td class="num">{{ r.no_check if r.no_check else "—" }}</td>
  </tr>
  {% endfor %}
</table>
</div>

</section><!-- /content-summary -->

<section class="view-content" id="content-changes">

<h2>Stock history — last {{ history_matrix.run_dates|length }} checks</h2>
<div class="muted" style="margin: -4px 0 12px;">
  Products currently OOS or with a status change in the last {{ transition_days }} days. Oldest &rarr; newest.
</div>
{% if history_matrix.groups %}
<div class="card">
<table class="matrix-table">
  <tr>
    <th></th>
    {% for d in history_matrix.run_dates %}<th class="matrix-col">{{ d }}</th>{% endfor %}
  </tr>
  {% for group in history_matrix.groups %}
  <tr class="matrix-retailer-row">
    <td colspan="{{ history_matrix.run_dates|length + 1 }}">{{ group.retailer }}</td>
  </tr>
  {% for row in group.oos_products %}
  <tr>
    <td>{{ row.name }}</td>
    {% for s in row.statuses %}
    <td class="matrix-cell">
      {% if s == "IN_STOCK" %}<span class="badge badge-IN_STOCK">IN</span>
      {% elif s == "OOS" %}<span class="badge badge-OOS">OOS</span>
      {% elif s == "ERROR" %}<span class="badge badge-ERROR">ERR</span>
      {% elif s == "UNKNOWN" %}<span class="badge badge-UNKNOWN">?</span>
      {% else %}<span class="subtle">—</span>{% endif %}
    </td>
    {% endfor %}
  </tr>
  {% endfor %}
  {% if group.other_products %}
  <tr class="matrix-expand-row">
    <td colspan="{{ history_matrix.run_dates|length + 1 }}">
      <details class="other-products">
        <summary>Show all {{ group.other_products|length }} other product{{ "s" if group.other_products|length != 1 else "" }} at {{ group.retailer }}</summary>
        <table class="matrix-table matrix-inner">
          {% for row in group.other_products %}
          <tr>
            <td>{{ row.name }}</td>
            {% for s in row.statuses %}
            <td class="matrix-cell">
              {% if s == "IN_STOCK" %}<span class="badge badge-IN_STOCK">IN</span>
              {% elif s == "OOS" %}<span class="badge badge-OOS">OOS</span>
              {% elif s == "ERROR" %}<span class="badge badge-ERROR">ERR</span>
              {% elif s == "UNKNOWN" %}<span class="badge badge-UNKNOWN">?</span>
              {% else %}<span class="subtle">—</span>{% endif %}
            </td>
            {% endfor %}
          </tr>
          {% endfor %}
        </table>
      </details>
    </td>
  </tr>
  {% endif %}
  {% endfor %}
</table>
</div>
{% else %}
<div class="card"><div class="empty">No OOS products or recent changes to show.</div></div>
{% endif %}

</section><!-- /content-changes -->

<section class="view-content" id="content-new-products">

<h2>New products detected</h2>
<div class="muted" style="margin: -4px 0 12px;">Products on a retailer's brand page not yet in <code>products.csv</code>.</div>
{% if new_products %}
<div class="card">
<table>
  <tr><th>Retailer</th><th>Product</th><th>First seen</th><th>URL</th></tr>
  {% for r in new_products %}
  <tr>
    <td>{{ r.retailer }}</td>
    <td>{{ r.name }}</td>
    <td class="subtle">{{ r.first_seen_human }}</td>
    <td class="url-cell">
      {% if r.url -%}
        <a href="{{ r.url }}" target="_blank" rel="noopener">{{ r.url }}</a>
      {%- else -%}
        <span class="subtle">—</span>
      {%- endif %}
    </td>
  </tr>
  {% endfor %}
</table>
</div>
{% else %}
<div class="card"><div class="empty">No new products detected.</div></div>
{% endif %}

</section><!-- /content-new-products -->

<section class="view-content" id="content-urls">

<h2>URLs to fix</h2>
<div class="muted" style="margin: -4px 0 12px;">Products whose URL doesn't point to a PDP — auto-marked ERROR until <code>products.csv</code> is updated.</div>
{% if urls_to_fix %}
<div class="card">
<table>
  <tr><th>Retailer</th><th>Product</th><th>Quality</th><th>URL</th></tr>
  {% for r in urls_to_fix %}
  <tr>
    <td>{{ r.retailer }}</td>
    <td>{{ r.name }}</td>
    <td class="subtle">{{ r.url_quality }}</td>
    <td class="url-cell"><a href="{{ r.url }}" target="_blank" rel="noopener">{{ r.url }}</a></td>
  </tr>
  {% endfor %}
</table>
</div>
{% else %}
<div class="card"><div class="empty">All URLs point to product pages.</div></div>
{% endif %}

</section><!-- /content-urls -->

<section class="view-content" id="content-retailer">

<div class="card">
{% for r in by_retailer %}
<div class="retailer-block">
  <div class="retailer-header">
    <div class="retailer-h3">{{ r.retailer }}</div>
    <span class="src-tag">{{ r.source }}</span>
  </div>
  <div class="retailer-counts">
    <span>{{ r.total }} SKU{{ "s" if r.total != 1 else "" }}</span>
    <span class="status-IN_STOCK">{{ r.in_stock }} in stock</span>
    {% if r.oos %}<span class="status-OOS">{{ r.oos }} OOS</span>{% endif %}
    {% if r.error %}<span class="status-ERROR">{{ r.error }} error</span>{% endif %}
    {% if r.unknown %}<span class="status-UNKNOWN">{{ r.unknown }} unknown</span>{% endif %}
    {% if r.no_check %}<span class="status-NO_CHECK">{{ r.no_check }} no check</span>{% endif %}
    <span class="subtle">checked {{ r.last_checked_human }}</span>
  </div>

  {% if r.oos_products %}
  <ul class="retailer-oos-list">
    {% for p in r.oos_products %}
    <li>
      <span class="badge badge-OOS">OOS</span>
      {{ p.name }}
      {% if p.went_oos_human %}<span class="subtle">since {{ p.went_oos_human }}</span>{% endif %}
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <div style="font-size:13px; color:#aaa; padding: 4px 0;">All products in stock.</div>
  {% endif %}

  {% if r.other_products %}
  <details class="other-products">
    <summary>{{ r.other_products|length }} other product{{ "s" if r.other_products|length != 1 else "" }}</summary>
    <table>
      <tr><th>Product</th><th>Status</th><th>Last checked</th></tr>
      {% for p in r.other_products %}
      <tr>
        <td>{{ p.name }}</td>
        <td><span class="badge badge-{{ p.status }}">{{ p.status.replace("_", " ") }}</span></td>
        <td class="subtle">{{ p.checked_at_human or "—" }}</td>
      </tr>
      {% endfor %}
    </table>
  </details>
  {% endif %}
</div>
{% endfor %}
</div>

</section><!-- /content-retailer -->

<section class="view-content" id="content-po-check">

<h2>PO Orders vs Stock</h2>
<div class="muted" style="margin: -4px 0 12px;">
  Line items from imported POs (via <code>pogen oos-check</code>), cross-referenced against current stock status.
</div>

{% if po_ordered_while_oos %}
<div class="card">
  <div style="padding: 12px 16px;">
    <strong>⚠ Ordered while OOS</strong>
    <ul class="retailer-list" style="margin-top:8px;">
    {% for r in po_ordered_while_oos %}
      <li>
        <span class="badge badge-OOS">OOS</span>
        {{ r.product_name }}
        <span class="subtle">— {{ r.po_number }} ({{ r.retailer }}), qty {{ r.quantity }}</span>
      </li>
    {% endfor %}
    </ul>
  </div>
</div>
{% endif %}

{% if po_orders %}
{% for po in po_orders %}
<div class="card">
  <div class="retailer-header" style="padding: 14px 16px 0;">
    <div class="retailer-h3">{{ po.po_number }} <span class="muted" style="font-weight:400;">— {{ po.retailer }}</span></div>
    <span class="subtle">imported {{ po.imported_human }}</span>
  </div>
  <table>
    <tr><th>Product</th><th class="num">Qty</th><th>Status</th></tr>
    {% for line in po.lines %}
    <tr>
      <td>
        {% if line.product_name %}{{ line.product_name }}
        {% else %}{{ line.description }}<div class="subtle">unmatched</div>
        {% endif %}
      </td>
      <td class="num">{{ line.quantity }}</td>
      <td>
        {% if line.status %}<span class="badge badge-{{ line.status }}">{{ line.status.replace("_"," ") }}</span>
        {% else %}<span class="subtle">—</span>{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endfor %}
{% else %}
<div class="card"><div class="empty">No POs imported yet. Run <code>pogen oos-check &lt;po_file&gt;</code> from po-tool.</div></div>
{% endif %}

</section><!-- /content-po-check -->

<div class="footer">Generated {{ generated_at }}</div>

</body>
</html>
"""


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _humanize_relative(dt: datetime, now: datetime) -> str:
    """Concise relative time: 'today', 'yesterday', 'N days ago',
    'N weeks ago', or absolute date for anything older than 4 weeks."""
    delta = now - dt
    days = delta.days
    if days < 0:
        return dt.strftime("%a %b %-d")
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 28:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    return dt.strftime("%a %b %-d")


def _fmt_run_ts(dt: datetime) -> str:
    return dt.strftime("%a %b %-d, %Y · %-I:%M %p UTC")


_RETAILER_ORDER = [
    "Target", "Walmart", "CVS", "Anthropologie", "ASOS",
    "Nordstrom", "Gee Beauty", "Cult Beauty", "Boots", "Goop",
]


def _build_history_matrix(
    conn: sqlite3.Connection,
    products: list,
    latest_by_product: dict,
) -> dict:
    date_rows = conn.execute(
        "SELECT DISTINCT date(checked_at) AS d FROM checks ORDER BY d DESC LIMIT 4"
    ).fetchall()
    run_dates_raw = [r["d"] for r in date_rows]  # newest first

    if not run_dates_raw:
        return {"run_dates": [], "groups": []}

    placeholders = ",".join("?" * len(run_dates_raw))
    history_rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT product_id, status,
                 date(checked_at) AS run_date,
                 ROW_NUMBER() OVER (
                   PARTITION BY product_id, date(checked_at)
                   ORDER BY checked_at DESC
                 ) AS rn
          FROM checks
          WHERE date(checked_at) IN ({placeholders})
        )
        SELECT product_id, status, run_date FROM ranked WHERE rn = 1
        """,
        run_dates_raw,
    ).fetchall()

    history: dict[int, dict[str, str]] = defaultdict(dict)
    for r in history_rows:
        history[r["product_id"]][r["run_date"]] = r["status"]

    oos_pids = {pid for pid, info in latest_by_product.items() if info["status"] == "OOS"}

    retailer_buckets: dict[str, dict] = {}
    for p in products:
        pid = p["id"]
        retailer = p["retailer"]
        if retailer not in retailer_buckets:
            retailer_buckets[retailer] = {"retailer": retailer, "oos_products": [], "other_products": []}
        item = {
            "name": p["product_name"],
            "statuses": [history[pid].get(d) for d in reversed(run_dates_raw)],
        }
        if pid in oos_pids:
            retailer_buckets[retailer]["oos_products"].append(item)
        else:
            retailer_buckets[retailer]["other_products"].append(item)

    for bucket in retailer_buckets.values():
        bucket["oos_products"].sort(key=lambda x: x["name"])
        bucket["other_products"].sort(key=lambda x: x["name"])

    # Only show retailers with at least one OOS product, in the preferred order
    groups = [
        retailer_buckets[r]
        for r in _RETAILER_ORDER
        if r in retailer_buckets and retailer_buckets[r]["oos_products"]
    ]
    for r, bucket in retailer_buckets.items():
        if r not in _RETAILER_ORDER and bucket["oos_products"]:
            groups.append(bucket)

    def _fmt_col(d: str) -> str:
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d")
        except ValueError:
            return d

    return {
        "run_dates": [_fmt_col(d) for d in reversed(run_dates_raw)],
        "groups": groups,
    }


def _gather(conn: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc)

    products = conn.execute(
        "SELECT id, retailer, product_name, url, url_quality, source, variant_match "
        "FROM products"
    ).fetchall()

    latest_rows = conn.execute(
        """
        WITH ranked AS (
          SELECT product_id, status, checked_at, notes,
                 ROW_NUMBER() OVER (
                   PARTITION BY product_id ORDER BY checked_at DESC
                 ) AS rn
          FROM checks
        )
        SELECT product_id, status, checked_at, notes FROM ranked WHERE rn = 1
        """
    ).fetchall()
    latest_by_product = {
        r["product_id"]: {
            "status": r["status"],
            "checked_at": _parse_dt(r["checked_at"]),
            "notes": r["notes"],
        }
        for r in latest_rows
    }

    went_oos_rows = conn.execute(
        """
        WITH ordered AS (
          SELECT product_id, status, checked_at,
                 LAG(status) OVER (
                   PARTITION BY product_id ORDER BY checked_at
                 ) AS prev
          FROM checks
        )
        SELECT product_id, MAX(checked_at) AS went_oos_at
        FROM ordered
        WHERE status = 'OOS' AND (prev IS NULL OR prev != 'OOS')
        GROUP BY product_id
        """
    ).fetchall()
    went_oos_by_product = {
        r["product_id"]: _parse_dt(r["went_oos_at"]) for r in went_oos_rows
    }

    cutoff = (now - timedelta(days=TRANSITION_WINDOW_DAYS)).isoformat()
    transition_rows = conn.execute(
        """
        WITH ordered AS (
          SELECT product_id, status, checked_at,
                 LAG(status) OVER (
                   PARTITION BY product_id ORDER BY checked_at
                 ) AS prev
          FROM checks
        )
        SELECT o.product_id, p.retailer, p.product_name, o.prev, o.status, o.checked_at
        FROM ordered o
        JOIN products p ON p.id = o.product_id
        WHERE o.prev IS NOT NULL AND o.prev != o.status AND o.checked_at >= ?
        ORDER BY o.checked_at DESC
        """,
        (cutoff,),
    ).fetchall()

    last_run_dt = None
    row = conn.execute("SELECT MAX(checked_at) AS m FROM checks").fetchone()
    if row and row["m"]:
        last_run_dt = _parse_dt(row["m"])

    new_products_rows = unconfirmed_new_products(conn)
    new_products = [
        {
            "retailer": r["retailer"],
            "name": r["product_name"],
            "url": r["brand_page_url"],
            "first_seen_human": _humanize_relative(
                _parse_dt(r["first_seen_at"]) or now, now
            ),
        }
        for r in new_products_rows
    ]

    oos_groups: dict[str, list[dict]] = defaultdict(list)
    stale_manual: list[dict] = []
    urls_to_fix: list[dict] = []
    retailer_buckets: dict[str, dict] = {}

    for p in products:
        rid = p["id"]
        latest = latest_by_product.get(rid)
        retailer = p["retailer"]
        bucket = retailer_buckets.setdefault(
            retailer,
            {
                "retailer": retailer,
                "source": p["source"],
                "total": 0,
                "in_stock": 0,
                "oos": 0,
                "error": 0,
                "unknown": 0,
                "no_check": 0,
                "latest_checked_at": None,
                "oos_products": [],
                "other_products": [],
            },
        )
        bucket["total"] += 1

        if latest is None:
            bucket["no_check"] += 1
            status = "NO_CHECK"
        else:
            key = {
                "IN_STOCK": "in_stock",
                "OOS": "oos",
                "ERROR": "error",
                "UNKNOWN": "unknown",
            }.get(latest["status"], "unknown")
            bucket[key] += 1
            status = latest["status"]
            if latest["checked_at"] and (
                bucket["latest_checked_at"] is None
                or latest["checked_at"] > bucket["latest_checked_at"]
            ):
                bucket["latest_checked_at"] = latest["checked_at"]

        if p["url_quality"] != "pdp":
            urls_to_fix.append(
                {
                    "retailer": retailer,
                    "name": p["product_name"],
                    "url": p["url"],
                    "url_quality": p["url_quality"],
                }
            )

        # Per-retailer view: classify each product into oos vs other,
        # carrying enough context to render rows without re-querying.
        item = {
            "name": p["product_name"],
            "status": status,
            "notes": (latest or {}).get("notes"),
            "checked_at_human": (
                _humanize_relative(latest["checked_at"], now)
                if latest and latest["checked_at"]
                else None
            ),
        }
        if status == "OOS":
            went_at = went_oos_by_product.get(rid) or latest["checked_at"]
            item["went_oos_human"] = (
                _humanize_relative(went_at, now) if went_at else None
            )
            bucket["oos_products"].append(item)
            oos_groups[p["product_name"]].append(
                {
                    "retailer": retailer,
                    "source": p["source"],
                    "went_oos_at": went_at,
                    "went_oos_human": item["went_oos_human"] or "—",
                }
            )
        else:
            bucket["other_products"].append(item)

        if p["source"] == "manual" and latest and latest["checked_at"]:
            age = now - latest["checked_at"]
            if age > timedelta(days=STALE_DAYS):
                stale_manual.append(
                    {
                        "retailer": retailer,
                        "name": p["product_name"],
                        "status": latest["status"],
                        "checked_at_human": _humanize_relative(
                            latest["checked_at"], now
                        ),
                        "checked_at": latest["checked_at"],
                    }
                )

    oos_groups_list = [
        {
            "name": name,
            "entries": sorted(entries, key=lambda e: e["retailer"]),
        }
        for name, entries in sorted(oos_groups.items())
    ]
    stale_manual.sort(key=lambda r: r["checked_at"])
    urls_to_fix.sort(key=lambda r: (r["retailer"], r["name"]))

    transitions = [
        {
            "name": t["product_name"],
            "retailer": t["retailer"],
            "prev": t["prev"],
            "curr": t["status"],
            "when_human": _humanize_relative(_parse_dt(t["checked_at"]) or now, now),
        }
        for t in transition_rows
    ]

    history_matrix = _build_history_matrix(conn, products, latest_by_product)

    retailer_summary = sorted(
        retailer_buckets.values(),
        key=lambda r: (r["source"], r["retailer"]),
    )

    # Per-retailer detailed view, ordered by OOS count descending so
    # worst-stocked retailers surface first. Within each, OOS product
    # list is sorted by name; "other" list groups ERROR/UNKNOWN/NO_CHECK
    # before IN_STOCK so the user sees what needs attention first.
    _other_order = {"ERROR": 0, "UNKNOWN": 1, "NO_CHECK": 2, "IN_STOCK": 3}
    by_retailer: list[dict] = []
    for r in retailer_buckets.values():
        entry = dict(r)
        entry["last_checked_human"] = (
            _humanize_relative(entry["latest_checked_at"], now)
            if entry["latest_checked_at"]
            else "never"
        )
        entry["oos_products"] = sorted(entry["oos_products"], key=lambda x: x["name"])
        entry["other_products"] = sorted(
            entry["other_products"],
            key=lambda x: (_other_order.get(x["status"], 9), x["name"]),
        )
        by_retailer.append(entry)
    by_retailer.sort(
        key=lambda r: (
            _RETAILER_ORDER.index(r["retailer"])
            if r["retailer"] in _RETAILER_ORDER
            else len(_RETAILER_ORDER)
        )
    )

    oos_count = sum(b["oos"] for b in retailer_buckets.values())
    total_skus = sum(b["total"] for b in retailer_buckets.values())

    po_orders = []
    po_ordered_while_oos = []
    for po in po_lines_for_dashboard(conn):
        imported_dt = _parse_dt(po["imported_at"])
        po_orders.append(
            {
                "po_number": po["po_number"],
                "customer": po["customer"],
                "retailer": po["retailer"],
                "imported_human": (
                    _humanize_relative(imported_dt, now) if imported_dt else "—"
                ),
                "lines": po["lines"],
            }
        )
        for line in po["lines"]:
            if line["quantity"] > 0 and line["status"] == "OOS":
                po_ordered_while_oos.append(
                    {
                        "product_name": line["product_name"],
                        "po_number": po["po_number"],
                        "retailer": po["retailer"],
                        "quantity": line["quantity"],
                    }
                )

    return {
        "last_run": _fmt_run_ts(last_run_dt) if last_run_dt else None,
        "generated_at": _fmt_run_ts(now),
        "total_skus": total_skus,
        "oos_count": oos_count,
        "oos_groups": oos_groups_list,
        "stale_manual": stale_manual,
        "stale_days": STALE_DAYS,
        "urls_to_fix": urls_to_fix,
        "transitions": transitions,
        "transition_days": TRANSITION_WINDOW_DAYS,
        "retailer_summary": retailer_summary,
        "by_retailer": by_retailer,
        "new_products": new_products,
        "history_matrix": history_matrix,
        "po_orders": po_orders,
        "po_ordered_while_oos": po_ordered_while_oos,
    }


def _render(ctx: dict) -> str:
    env = Environment(autoescape=True, trim_blocks=False, lstrip_blocks=False)
    return env.from_string(_TEMPLATE).render(**ctx)


def build(out_path: Path = OUT_PATH) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        ctx = _gather(conn)
    html = _render(ctx)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main() -> None:
    path = build()
    size = path.stat().st_size
    print(f"wrote {path} ({size:,} bytes)")


if __name__ == "__main__":
    main()
