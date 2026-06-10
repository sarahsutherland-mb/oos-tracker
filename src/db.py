from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path("data.db")
DEFAULT_PRODUCTS_CSV = Path("products.csv")

MANUAL_RETAILERS = {"Target", "Walmart", "ASOS", "Anthropologie", "CVS"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    product_name TEXT NOT NULL,
    url TEXT NOT NULL,
    url_quality TEXT NOT NULL,
    source TEXT NOT NULL,
    variant_match TEXT,
    UNIQUE(retailer, product_name)
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    checked_at TIMESTAMP NOT NULL,
    notes TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_checks_product_time
    ON checks(product_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS new_products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    product_name TEXT NOT NULL,
    brand_page_url TEXT,
    first_seen_at TIMESTAMP NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(retailer, product_name)
);
"""


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "variant_match" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN variant_match TEXT")


def _source_for(retailer: str) -> str:
    return "manual" if retailer in MANUAL_RETAILERS else "automated"


def _read_csv(csv_path: Path | str) -> dict[tuple[str, str], tuple]:
    """Parse products.csv into {(retailer, name): (url, url_quality, source, variant_match)}."""
    out: dict[tuple[str, str], tuple] = {}
    with Path(csv_path).open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            retailer = r["retailer"].strip()
            name = r["product_name"].strip()
            out[(retailer, name)] = (
                r["url"].strip(),
                r["url_quality"].strip(),
                _source_for(retailer),
                (r.get("variant_match") or "").strip() or None,
            )
    return out


def sync_from_csv(
    conn: sqlite3.Connection, csv_path: Path | str = DEFAULT_PRODUCTS_CSV
) -> dict[str, int]:
    """Reconcile the `products` table with `products.csv` (the source of truth).

    Operations, in order: INSERT rows present only in CSV, UPDATE existing
    rows where any of (url, url_quality, source, variant_match) differs,
    DELETE rows present only in DB along with their historical `checks`
    rows (no `ON DELETE CASCADE` in the schema so this is explicit).
    Idempotent: running with no CSV changes is a no-op.

    Rename limitation: the upsert key is (retailer, product_name). When a
    row's product_name changes between CSV edits, this function sees
    delete-of-old + insert-of-new — and the old product_id's check
    history is dropped along with it. If history must survive a rename,
    UPDATE `products.product_name` in place via a dedicated helper *before*
    calling this.

    Prints a one-line summary and returns the same counts as a dict.
    """
    csv_rows = _read_csv(csv_path)

    db_rows: dict[tuple[str, str], tuple] = {}
    for row in conn.execute(
        "SELECT id, retailer, product_name, url, url_quality, source, variant_match "
        "FROM products"
    ):
        db_rows[(row["retailer"], row["product_name"])] = (
            row["id"],
            row["url"],
            row["url_quality"],
            row["source"],
            row["variant_match"],
        )

    csv_keys = set(csv_rows)
    db_keys = set(db_rows)

    inserted = updated = deleted = orphan_checks = 0

    for key in csv_keys - db_keys:
        retailer, name = key
        url, url_quality, source, variant_match = csv_rows[key]
        conn.execute(
            "INSERT INTO products "
            "(retailer, product_name, url, url_quality, source, variant_match) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (retailer, name, url, url_quality, source, variant_match),
        )
        inserted += 1

    for key in csv_keys & db_keys:
        csv_fields = csv_rows[key]
        db_id, *db_fields = db_rows[key]
        if tuple(db_fields) != csv_fields:
            conn.execute(
                "UPDATE products SET url=?, url_quality=?, source=?, variant_match=? "
                "WHERE id=?",
                (*csv_fields, db_id),
            )
            updated += 1

    for key in db_keys - csv_keys:
        db_id = db_rows[key][0]
        c = conn.execute("DELETE FROM checks WHERE product_id=?", (db_id,))
        orphan_checks += c.rowcount
        conn.execute("DELETE FROM products WHERE id=?", (db_id,))
        deleted += 1

    print(
        f"sync_from_csv: {inserted} inserted, {updated} updated, "
        f"{deleted} deleted, {orphan_checks} orphan checks deleted"
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "orphan_checks": orphan_checks,
    }


def seed_products(
    conn: sqlite3.Connection, csv_path: Path | str = DEFAULT_PRODUCTS_CSV
) -> int:
    """DEPRECATED: use `sync_from_csv()` instead.

    Upserts products from the CSV but never deletes orphans, so it leaves
    stale rows behind whenever a CSV row is removed or its product_name
    changes. Kept for now only for backward compatibility — no caller in
    this codebase uses it. Will be removed in a future cleanup.
    """
    rows = [
        (retailer, name, *fields)
        for (retailer, name), fields in _read_csv(csv_path).items()
    ]
    conn.executemany(
        """
        INSERT INTO products (retailer, product_name, url, url_quality, source, variant_match)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(retailer, product_name) DO UPDATE SET
            url = excluded.url,
            url_quality = excluded.url_quality,
            source = excluded.source,
            variant_match = excluded.variant_match
        """,
        rows,
    )
    return len(rows)


def all_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, retailer, product_name, url, url_quality, source, variant_match "
        "FROM products ORDER BY retailer, product_name"
    ).fetchall()


def record_check(
    conn: sqlite3.Connection,
    product_id: int,
    status: str,
    checked_at: str,
    notes: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO checks (product_id, status, checked_at, notes) "
        "VALUES (?, ?, ?, ?)",
        (product_id, status, checked_at, notes),
    )


def latest_check(
    conn: sqlite3.Connection, product_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT status, checked_at, notes FROM checks "
        "WHERE product_id = ? ORDER BY checked_at DESC LIMIT 1",
        (product_id,),
    ).fetchone()


def update_check(
    conn: sqlite3.Connection,
    product_id: int,
    checked_at: str,
    new_status: str,
    new_notes: str | None,
) -> int:
    """Update the check row written for a specific (product_id, checked_at).
    Used by the brand-page reconciliation pass to downgrade an ERROR row
    to OOS in place. Returns affected rowcount."""
    return conn.execute(
        "UPDATE checks SET status=?, notes=? "
        "WHERE product_id=? AND checked_at=?",
        (new_status, new_notes, product_id, checked_at),
    ).rowcount


def upsert_new_product(
    conn: sqlite3.Connection,
    retailer: str,
    product_name: str,
    brand_page_url: str | None,
    first_seen_at: str,
) -> bool:
    """Insert a new-product candidate; no-op if (retailer, name) already
    seen. Returns True if a row was inserted (genuinely new), False if
    already known."""
    cur = conn.execute(
        "INSERT INTO new_products "
        "(retailer, product_name, brand_page_url, first_seen_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(retailer, product_name) DO NOTHING",
        (retailer, product_name, brand_page_url, first_seen_at),
    )
    return cur.rowcount > 0


def unconfirmed_new_products(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT retailer, product_name, brand_page_url, first_seen_at "
        "FROM new_products WHERE confirmed = 0 "
        "ORDER BY retailer, product_name"
    ).fetchall()


def main() -> None:
    """Initialize the schema, sync from products.csv, print a summary."""
    with connect() as conn:
        init_schema(conn)
        sync_from_csv(conn)

        breakdown = conn.execute(
            "SELECT retailer, source, COUNT(*) AS n FROM products "
            "GROUP BY retailer, source ORDER BY source, retailer"
        ).fetchall()
        print(f"\n{'retailer':<20} {'source':<10} {'count':>5}")
        print("-" * 38)
        for r in breakdown:
            print(f"{r['retailer']:<20} {r['source']:<10} {r['n']:>5}")

        totals = conn.execute(
            "SELECT source, COUNT(*) AS n FROM products GROUP BY source"
        ).fetchall()
        print()
        for r in totals:
            print(f"{r['source']:<10} total: {r['n']}")
        total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        print(f"{'all':<10} total: {total}")


if __name__ == "__main__":
    main()
