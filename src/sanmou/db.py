from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "sanmou_battles.sqlite"


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open the project SQLite database with row access by column name."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def list_tables(db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]
