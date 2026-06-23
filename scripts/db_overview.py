from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sanmou.db import DEFAULT_DB_PATH, connect, list_tables


def main() -> int:
    print(f"Database: {DEFAULT_DB_PATH}")
    tables = list_tables()
    if not tables:
        print("No tables found.")
        return 0

    with connect() as conn:
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            try:
                count = conn.execute(f"SELECT COUNT(*) AS count FROM {quoted}").fetchone()["count"]
            except sqlite3.DatabaseError as exc:
                print(f"- {table}: count failed ({exc})")
                continue
            print(f"- {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
