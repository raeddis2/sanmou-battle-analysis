#!/usr/bin/env python3
"""One-time fix for Huang Gai tactics in battle_20260621_235932 group.

Source: user correction on 2026-06-22: 黄盖也是无韬略。
Target tables/fields:
- participants: tactics_text, source_note, payload_json for 黄盖 in the
  battle_20260621_235932 8-report group.
- damage_contexts: source_context_json and target_context_json participant
  snapshots for 黄盖 in the same reports.
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
    "battle_20260621_235932",
    "battle_20260621_235942",
    "battle_20260621_235952",
    "battle_20260621_235958",
    "battle_20260622_000006",
    "battle_20260622_000013",
    "battle_20260622_000021",
    "battle_20260622_000030",
)

SOURCE_NOTE = "用户于 2026-06-22 更正 battle_20260621_235932 同配置组：黄盖无韬略"


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
          AND hero = '黄盖'
        ORDER BY report_id
        """,
        report_ids,
    ).fetchall()
    updated = 0
    for row in rows:
        payload = json_loads(row["payload_json"])
        payload["tactics_text"] = "无韬略"
        payload["source_note"] = SOURCE_NOTE
        conn.execute(
            """
            UPDATE participants
            SET tactics_text = ?,
                source_note = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                "无韬略",
                SOURCE_NOTE,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                row["id"],
            ),
        )
        updated += 1

    if updated != len(report_ids):
        raise RuntimeError(f"Expected to update {len(report_ids)} Huang Gai rows, updated {updated}")
    return report_ids, updated


def load_huanggai(conn: sqlite3.Connection, report_ids: list[int]) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero = '黄盖'
        """,
        report_ids,
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        result[int(row["report_id"])] = {
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
    return result


def update_context_text(text: str, participant: dict[str, Any]) -> str:
    data = json_loads(text)
    if data.get("hero") == "黄盖":
        data["participant"] = participant
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def update_damage_contexts(conn: sqlite3.Connection, report_ids: list[int]) -> int:
    participants = load_huanggai(conn, report_ids)
    updated = 0
    for row in conn.execute(
        f"""
        SELECT id, report_id, source, target, source_context_json, target_context_json
        FROM damage_contexts
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND (source = '黄盖' OR target = '黄盖')
        """,
        report_ids,
    ):
        participant = participants[int(row["report_id"])]
        source_context = row["source_context_json"]
        target_context = row["target_context_json"]
        if row["source"] == "黄盖":
            source_context = update_context_text(source_context, participant)
        if row["target"] == "黄盖":
            target_context = update_context_text(target_context, participant)
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
