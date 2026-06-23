#!/usr/bin/env python3
"""One-time cleanup for deprecated report_skill_details.grade.

Source: user clarification in Codex thread on 2026-06-22:
Sanmou tactics only have red-level variants; "战法品级" is not a valid tactic
attribute. Hero 品级 belongs to participants and is handled separately.

Target table/field:
- report_skill_details.grade: clear all values/placeholders.

Run timing: one-time schema-compatibility cleanup. The column is retained for
old SQLite compatibility but should not be used by analysis.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"


def main() -> int:
    conn = sqlite3.connect(DB)
    try:
        with conn:
            updated = conn.execute(
                """
                UPDATE report_skill_details
                SET grade = NULL
                WHERE grade IS NOT NULL
                  AND TRIM(grade) != ''
                """
            ).rowcount
        print(f"report_skill_details_grade_cleared: {updated}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
