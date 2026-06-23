#!/usr/bin/env python3
"""One-time deletion of Cao Cao/Dian Wei/Bian Furen battle reports.

Source: user decision on 2026-06-22: 曹操/典韦/卞夫人的战报都删掉.
Reason: user chose to remove this team group instead of completing metadata.
Target tables: reports and dependent rows in participants, events,
state_changes, damage_contexts, and event_search.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"

HEROES = (
    "曹操",
    "典韦",
    "卞夫人",
)

DEPENDENT_TABLES = (
    "damage_contexts",
    "state_changes",
    "events",
    "participants",
)


def placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        reports = conn.execute(
            f"""
            SELECT id, report_key
            FROM reports
            WHERE id IN (
                SELECT DISTINCT report_id
                FROM participants
                WHERE hero IN ({placeholders(len(HEROES))})
            )
            ORDER BY report_key
            """,
            HEROES,
        ).fetchall()
        if not reports:
            print("No matching reports found.")
            return 0

        report_ids = [int(row["id"]) for row in reports]
        id_placeholders = placeholders(len(report_ids))
        counts_before = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE report_id IN ({id_placeholders})",
                report_ids,
            ).fetchone()[0]
            for table in DEPENDENT_TABLES
        }
        counts_before["event_search"] = conn.execute(
            f"SELECT COUNT(*) FROM event_search WHERE report_id IN ({id_placeholders})",
            report_ids,
        ).fetchone()[0]
        counts_before["reports"] = len(report_ids)

        for table in ("damage_contexts", "state_changes", "events", "participants"):
            with conn:
                conn.execute(
                    f"DELETE FROM {table} WHERE report_id IN ({id_placeholders})",
                    report_ids,
                )
        with conn:
            conn.execute(
                f"DELETE FROM reports WHERE id IN ({id_placeholders})",
                report_ids,
            )
        rebuild_event_search(conn)

        print("deleted_reports:")
        for row in reports:
            print(f"  {row['id']}: {row['report_key']}")
        print("deleted_rows:")
        for table, count in counts_before.items():
            print(f"  {table}: {count}")
        return 0
    finally:
        conn.close()


def rebuild_event_search(conn: sqlite3.Connection) -> None:
    """Rebuild FTS after deleting events.

    Large range deletes against this FTS5 table are slow in this database, so
    rebuilding from the remaining events is faster and easier to validate.
    """
    with conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS event_search;
            CREATE VIRTUAL TABLE event_search
            USING fts5(report_id UNINDEXED, event_id UNINDEXED, raw_text);
            """
        )
        conn.execute(
            """
            INSERT INTO event_search (report_id, event_id, raw_text)
            SELECT report_id, id, raw_text
            FROM events
            ORDER BY report_id, event_order
            """
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
