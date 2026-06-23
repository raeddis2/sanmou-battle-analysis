#!/usr/bin/env python3
"""One-time grade sync for the battle_20260621_150323 config group.

Source: user confirmation on 2026-06-22: 黄忠/黄月英/黄盖品级0，大乔/马超品级2。
Target tables/fields:
- participants: grade, source_note, payload_json for the 5-report group.
- damage_contexts: source_context_json and target_context_json participant
  snapshots for the same heroes/reports.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"

REPORT_KEYS = (
    "battle_20260621_150323",
    "battle_20260621_155233",
    "battle_20260621_160609",
    "battle_20260621_160619",
    "battle_20260621_164852",
)

GRADES = {
    "黄忠": "0",
    "黄月英": "0",
    "黄盖": "0",
    "大乔": "2",
    "马超": "2",
}

SOURCE_NOTE = "用户于 2026-06-22 确认 battle_20260621_150323 同配置组品级"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def json_loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def update_participants(conn: sqlite3.Connection) -> tuple[list[int], int]:
    report_rows = conn.execute(
        f"""
        SELECT id, report_key
        FROM reports
        WHERE report_key IN ({placeholders(len(REPORT_KEYS))})
        ORDER BY report_key
        """,
        REPORT_KEYS,
    ).fetchall()
    if len(report_rows) != len(REPORT_KEYS):
        found = {row["report_key"] for row in report_rows}
        missing = sorted(set(REPORT_KEYS) - found)
        raise RuntimeError(f"Missing report keys: {missing}")

    report_ids = [int(row["id"]) for row in report_rows]
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero IN ({placeholders(len(GRADES))})
        ORDER BY report_id, hero
        """,
        (*report_ids, *GRADES.keys()),
    ).fetchall()
    updated = 0
    for row in rows:
        grade = GRADES[row["hero"]]
        payload = json_loads(row["payload_json"])
        payload["grade"] = grade
        payload["source_note"] = SOURCE_NOTE
        conn.execute(
            """
            UPDATE participants
            SET grade = ?,
                source_note = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                grade,
                SOURCE_NOTE,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                row["id"],
            ),
        )
        updated += 1

    expected = len(report_ids) * len(GRADES)
    if updated != expected:
        raise RuntimeError(f"Expected to update {expected} participant rows, updated {updated}")
    return report_ids, updated


def load_participants(conn: sqlite3.Connection, report_ids: list[int]) -> dict[tuple[int, str], dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero IN ({placeholders(len(GRADES))})
        """,
        (*report_ids, *GRADES.keys()),
    ).fetchall()
    participants: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        participants[(int(row["report_id"]), str(row["hero"]))] = {
            "country": row["country"],
            "gold_seals": row["gold_seals"],
            "grade": row["grade"],
            "hero": row["hero"],
            "initial_troops": row["initial_troops"],
            "innate_skill": row["innate_skill"],
            "innate_skill_redness": row["innate_skill_redness"],
            "level": row["level"],
            "payload_json": json_loads(row["payload_json"]),
            "redness": row["redness"],
            "side": row["side"],
            "skills_text": row["skills_text"],
            "source_note": row["source_note"],
            "tactics_text": row["tactics_text"],
            "team_id": row["team_id"],
            "unit_type": row["unit_type"],
        }
    return participants


def update_context_text(text: str, participant: dict[str, Any]) -> str:
    data = json_loads(text)
    if data.get("hero") == participant["hero"]:
        data["participant"] = participant
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def update_damage_contexts(conn: sqlite3.Connection, report_ids: list[int]) -> int:
    participants = load_participants(conn, report_ids)
    updated = 0
    for row in conn.execute(
        f"""
        SELECT id, report_id, source, target, source_context_json, target_context_json
        FROM damage_contexts
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND (source IN ({placeholders(len(GRADES))})
               OR target IN ({placeholders(len(GRADES))}))
        """,
        (*report_ids, *GRADES.keys(), *GRADES.keys()),
    ):
        source_context = row["source_context_json"]
        target_context = row["target_context_json"]
        changed = False
        source_key = (int(row["report_id"]), str(row["source"]))
        target_key = (int(row["report_id"]), str(row["target"]))
        if source_key in participants:
            source_context = update_context_text(source_context, participants[source_key])
            changed = True
        if target_key in participants:
            target_context = update_context_text(target_context, participants[target_key])
            changed = True
        if changed:
            conn.execute(
                """
                UPDATE damage_contexts
                SET source_context_json = ?,
                    target_context_json = ?
                WHERE id = ?
                """,
                (source_context, target_context, row["id"]),
            )
            updated += 1
    return updated


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")
    conn = connect()
    try:
        with conn:
            report_ids, participants_updated = update_participants(conn)
            contexts_updated = update_damage_contexts(conn, report_ids)
        print(f"reports: {len(report_ids)}")
        print(f"participants_updated: {participants_updated}")
        print(f"damage_contexts_updated: {contexts_updated}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
