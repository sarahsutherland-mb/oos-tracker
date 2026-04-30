from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path("data.db")
DEFAULT_PRODUCTS_CSV = Path("products.csv")

MANUAL_RETAILERS = {"Target", "Walmart", "ASOS"}

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
"""


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
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


def seed_products(
    conn: sqlite3.Connection, csv_path: Path | str = DEFAULT_PRODUCTS_CSV
) -> int:
    with Path(csv_path).open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [
            (
                r["retailer"].strip(),
                r["product_name"].strip(),
                r["url"].strip(),
                r["url_quality"].strip(),
                _source_for(r["retailer"].strip()),
                (r.get("variant_match") or "").strip() or None,
            )
            for r in reader
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


def main() -> None:
    """Initialize the schema, seed from products.csv, print a summary."""
    with connect() as conn:
        init_schema(conn)
        n = seed_products(conn)
        print(f"Seeded/updated {n} rows from {DEFAULT_PRODUCTS_CSV}")

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
