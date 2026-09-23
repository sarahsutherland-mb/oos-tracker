"""Slack alert for what changed in a run.

One message per run, listing the products that moved. Silence when nothing
moved -- a weekly "no changes" ping trains people to ignore the channel, and
this alert only earns attention if its presence means something happened.

The message is built from the `checks` table rather than from the run's
in-memory results, so it reflects the state that was actually written --
including the brand-page reconciliation pass, which rewrites ERROR rows to
OOS in place after the per-product checks have already run.

Nothing here raises. A missing webhook, a Slack outage or a revoked URL must
not fail a run whose data is already safely recorded.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass

# A transition worth a line in Slack, and the words for it. Anything not
# listed is a state change we don't have a story for (UNKNOWN -> ERROR, say)
# and is counted under "other" rather than given a confident label.
_HEADLINES = {
    ("IN_STOCK", "OOS"): "went out of stock",
    ("OOS", "IN_STOCK"): "back in stock",
    ("UNKNOWN", "OOS"): "now out of stock",
    ("UNKNOWN", "IN_STOCK"): "now in stock",
    ("ERROR", "OOS"): "now out of stock",
    ("ERROR", "IN_STOCK"): "back in stock",
    ("IN_STOCK", "ERROR"): "check failing",
    ("OOS", "ERROR"): "check failing",
}

_TIMEOUT = 15  # seconds

# When a retailer blocks us, every one of its SKUs fails in the same run and
# the alert becomes 25 identical lines -- which is how a channel gets muted,
# and then the one line that mattered goes unread too. At or above this many
# failures for a single retailer, they collapse into one line saying so.
# Stock changes are never collapsed: those are the news.
_RETAILER_WIDE_FAILURES = 5


@dataclass(frozen=True)
class Transition:
    product_name: str
    retailer: str
    prev: str
    curr: str


def transitions_since(conn: sqlite3.Connection, since_iso: str) -> list[Transition]:
    """Every product whose status differs from its previous check, this run.

    `since_iso` is the run's start timestamp: the window is the run, not a
    fixed number of days, so a re-run on the same day doesn't re-announce
    what the earlier one already did.
    """
    rows = conn.execute(
        """
        WITH ordered AS (
          SELECT product_id, status, checked_at,
                 LAG(status) OVER (
                   PARTITION BY product_id ORDER BY checked_at
                 ) AS prev
          FROM checks
        )
        SELECT p.product_name, p.retailer, o.prev, o.status
        FROM ordered o
        JOIN products p ON p.id = o.product_id
        WHERE o.prev IS NOT NULL AND o.prev != o.status AND o.checked_at >= ?
        ORDER BY p.product_name, p.retailer
        """,
        (since_iso,),
    ).fetchall()
    return [
        Transition(r["product_name"], r["retailer"], r["prev"], r["status"])
        for r in rows
    ]


def format_message(
    transitions: list[Transition], dashboard_url: str | None = None
) -> str | None:
    """The message, or None when there is nothing worth sending.

    Pure, so the wording can be checked without a webhook and without a run.
    """
    if not transitions:
        return None

    went_oos = [t for t in transitions if t.curr == "OOS" and t.prev != "ERROR"]
    back = [t for t in transitions if t.curr == "IN_STOCK"]
    errors = [t for t in transitions if t.curr == "ERROR"]

    headline_bits = []
    if went_oos:
        headline_bits.append(f"{len(went_oos)} went OOS")
    if back:
        headline_bits.append(f"{len(back)} back in stock")
    if errors:
        headline_bits.append(f"{len(errors)} now erroring")
    headline = ", ".join(headline_bits) or f"{len(transitions)} status changes"

    # Which retailers failed wholesale? Those get one line, not twenty-five.
    failures_by_retailer: Counter[str] = Counter(
        t.retailer for t in transitions if t.curr in ("ERROR", "UNKNOWN")
    )
    collapsed = {
        r for r, n in failures_by_retailer.items() if n >= _RETAILER_WIDE_FAILURES
    }

    lines = [f"*Megababe stock check* — {headline}"]
    for retailer in sorted(collapsed):
        n = failures_by_retailer[retailer]
        lines.append(
            f"• *{retailer}* — {n} checks failing across the retailer "
            f"(likely blocked, not {n} products going out of stock)"
        )
    for t in transitions:
        if t.retailer in collapsed and t.curr in ("ERROR", "UNKNOWN"):
            continue  # already covered by the retailer-wide line above
        what = _HEADLINES.get((t.prev, t.curr), f"{t.prev} → {t.curr}")
        lines.append(f"• {t.product_name} · {t.retailer} — {what}")
    if dashboard_url:
        lines.append(f"<{dashboard_url}|Open the dashboard>")
    return "\n".join(lines)


def post(webhook_url: str, text: str) -> None:
    """POST to a Slack incoming webhook. Raises on failure; callers catch."""
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
        if res.status != 200:
            raise RuntimeError(f"slack returned HTTP {res.status}")


def notify(conn: sqlite3.Connection, since_iso: str) -> None:
    """Announce this run's changes, if there are any and Slack is configured.

    Never raises: the run's results are already in the database by the time
    this is called, and a Slack problem must not turn a good run into a
    failed one.
    """
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    try:
        changes = transitions_since(conn, since_iso)
    except sqlite3.Error as e:
        print(f"[slack] could not read transitions: {e}")
        return

    if not changes:
        print("[slack] no status changes this run; no message sent")
        return

    text = format_message(changes, os.environ.get("DASHBOARD_URL", "").strip() or None)
    if text is None:
        return

    if not webhook:
        # Worth printing rather than silently dropping: this is exactly the
        # run someone would want to know about, and the log is the fallback.
        print(f"[slack] SLACK_WEBHOOK_URL not set; would have sent:\n{text}")
        return

    try:
        post(webhook, text)
        print(f"[slack] sent {len(changes)} change(s)")
    except (urllib.error.URLError, RuntimeError, OSError) as e:
        print(f"[slack] alert failed ({e}); run itself was unaffected")
