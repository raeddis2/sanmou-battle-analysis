#!/usr/bin/env python3
"""One-time deletion of ambiguous duplicate-hero battle reports.

Source: user decision on 2026-06-22.
Reason: battle_20260620_043646 and battle_20260620_045640 contain duplicate
same-name heroes in the same report, making the battle flow ambiguous and
unsafe for analysis.
Target tables: reports and dependent rows in participants, events,
state_changes, damage_contexts, and event_search.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"

REPORT_KEYS = (
    "battle_20260620_043646",
    "battle_20260620_045640",
)

DEPENDENT_TABLES = (
    "damage_contexts",
    "state_changes",
    "event_search",
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
    try:
        with conn:
            reports = conn.execute(
                f"""
                SELECT id, report_key
                FROM reports
                WHERE report_key IN ({placeholders(len(REPORT_KEYS))})
                ORDER BY report_key
                """,
                REPORT_KEYS,
            ).fetchall()
            if len(reports) != len(REPORT_KEYS):
                found = {row["report_key"] for row in reports}
                missing = sorted(set(REPORT_KEYS) - found)
                raise RuntimeError(f"Missing report keys: {missing}")

            report_ids = [int(row["id"]) for row in reports]
            id_placeholders = placeholders(len(report_ids))
            counts_before = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE report_id IN ({id_placeholders})",
                    report_ids,
                ).fetchone()[0]
                for table in DEPENDENT_TABLES
            }
            counts_before["reports"] = len(report_ids)

            for table in DEPENDENT_TABLES:
                conn.execute(
                    f"DELETE FROM {table} WHERE report_id IN ({id_placeholders})",
                    report_ids,
                )
            conn.execute(
                f"DELETE FROM reports WHERE id IN ({id_placeholders})",
                report_ids,
            )

        print("deleted_reports:")
        for row in reports:
            print(f"  {row['id']}: {row['report_key']}")
        print("deleted_rows:")
        for table, count in counts_before.items():
            print(f"  {table}: {count}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
