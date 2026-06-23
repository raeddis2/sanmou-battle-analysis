#!/usr/bin/env python3
"""One-time sync for the confirmed battle_20260620_024521 config group.

Source: user confirmation in Codex thread on 2026-06-22: 按上一组填.
Target tables/fields:
- participants: country, level, grade, unit_type, initial_troops, redness,
  gold_seals, innate_skill, innate_skill_redness, skills_text, tactics_text,
  source_note, payload_json for the 2-report group.
- damage_contexts: source_context_json and target_context_json participant
  snapshots for the same heroes/reports.

Run when: after the user confirms this group uses the same configuration as
the battle_20260620_032503 group.
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
    "battle_20260620_024521",
    "battle_20260620_030317",
)

SOURCE_NOTE = "用户于 2026-06-22 确认 battle_20260620_024521 同配置组按上一组填写"

CONFIRMED = {
    "张飞": {
        "country": "蜀",
        "level": 50,
        "grade": "0",
        "unit_type": "枪兵",
        "initial_troops": 16000,
        "redness": "0红",
        "gold_seals": "0印",
        "innate_skill": "万人之敌",
        "innate_skill_redness": "0红",
        "skills_text": "万人之敌（0红）、水淹七军（0红）、趁火打劫（0红）",
        "tactics_text": "无韬略",
    },
    "赵云": {
        "country": "蜀",
        "level": 50,
        "grade": "0",
        "unit_type": "骑兵",
        "initial_troops": 16000,
        "redness": "0红",
        "gold_seals": "0印",
        "innate_skill": "七进七出",
        "innate_skill_redness": "0红",
        "skills_text": "七进七出（0红）、辕门射戟（0红）、清风驱疾（0红）",
        "tactics_text": "无韬略",
    },
    "马超": {
        "country": "蜀",
        "level": 50,
        "grade": "0",
        "unit_type": "骑兵",
        "initial_troops": 16000,
        "redness": "0红",
        "gold_seals": "0印",
        "innate_skill": "纵马横枪",
        "innate_skill_redness": "0红",
        "skills_text": "纵马横枪（0红）、锐不可当（0红）、以静制动（0红）",
        "tactics_text": "无韬略",
    },
    "诸葛亮": {
        "country": "蜀",
        "level": 50,
        "grade": "2",
        "unit_type": "弓兵",
        "initial_troops": 10000,
        "redness": "3红",
        "gold_seals": "2印",
        "innate_skill": "草船借箭",
        "innate_skill_redness": "2红",
        "skills_text": "草船借箭（2红）、无难之志（4红）、普攻",
        "tactics_text": "无韬略",
    },
    "马云禄": {
        "country": "蜀",
        "level": 50,
        "grade": "3",
        "unit_type": "骑兵",
        "initial_troops": 10000,
        "redness": "3红",
        "gold_seals": "3印",
        "innate_skill": "红妆缭乱",
        "innate_skill_redness": "3红",
        "skills_text": "红妆缭乱（3红）、百战不殆（2红）、普攻",
        "tactics_text": "无韬略",
    },
}


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


def participant_payload(row: sqlite3.Row, values: dict[str, Any]) -> str:
    payload = json_loads(row["payload_json"])
    payload.update(values)
    payload["source_note"] = SOURCE_NOTE
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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
          AND hero IN ({placeholders(len(CONFIRMED))})
        ORDER BY report_id, hero
        """,
        (*report_ids, *CONFIRMED.keys()),
    ).fetchall()
    updated = 0
    for row in rows:
        values = CONFIRMED[row["hero"]]
        conn.execute(
            """
            UPDATE participants
            SET country = ?,
                level = ?,
                grade = ?,
                unit_type = ?,
                initial_troops = ?,
                redness = ?,
                gold_seals = ?,
                innate_skill = ?,
                innate_skill_redness = ?,
                skills_text = ?,
                tactics_text = ?,
                source_note = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                values["country"],
                values["level"],
                values["grade"],
                values["unit_type"],
                values["initial_troops"],
                values["redness"],
                values["gold_seals"],
                values["innate_skill"],
                values["innate_skill_redness"],
                values["skills_text"],
                values["tactics_text"],
                SOURCE_NOTE,
                participant_payload(row, values),
                row["id"],
            ),
        )
        updated += 1

    expected = len(report_ids) * len(CONFIRMED)
    if updated != expected:
        raise RuntimeError(f"Expected to update {expected} participant rows, updated {updated}")
    return report_ids, updated


def load_participants(conn: sqlite3.Connection, report_ids: list[int]) -> dict[tuple[int, str], dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero IN ({placeholders(len(CONFIRMED))})
        """,
        (*report_ids, *CONFIRMED.keys()),
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
          AND (source IN ({placeholders(len(CONFIRMED))})
               OR target IN ({placeholders(len(CONFIRMED))}))
        """,
        (*report_ids, *CONFIRMED.keys(), *CONFIRMED.keys()),
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
