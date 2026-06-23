#!/usr/bin/env python3
"""One-time deletion of a bad battle capture.

Source: user decision on 2026-06-22: battle_20260622_224447 was re-recorded.
Reason: this capture only parsed one non-battle event and has no usable battle
flow, state changes, or damage contexts.
Target tables: reports and dependent rows in participants, report_skill_details,
events, state_changes, damage_contexts, and event_search.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"

REPORT_KEY = "battle_20260622_224447"

DEPENDENT_TABLES = (
    "damage_contexts",
    "state_changes",
    "event_search",
    "events",
    "participants",
    "report_skill_details",
)


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with conn:
            report = conn.execute(
                """
                SELECT id, report_key, event_count
                FROM reports
                WHERE report_key = ?
                """,
                (REPORT_KEY,),
            ).fetchone()
            if report is None:
                print(f"No matching report found: {REPORT_KEY}")
                return 0

            report_id = int(report["id"])
            counts_before = {}
            for table in DEPENDENT_TABLES:
                counts_before[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE report_id = ?",
                    (report_id,),
                ).fetchone()[0]
            counts_before["reports"] = 1

            for table in DEPENDENT_TABLES:
                conn.execute(
                    f"DELETE FROM {table} WHERE report_id = ?",
                    (report_id,),
                )
            conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))

        print("deleted_report:")
        print(f"  {report['id']}: {report['report_key']} event_count={report['event_count']}")
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
