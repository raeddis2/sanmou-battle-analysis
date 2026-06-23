#!/usr/bin/env python3
"""One-time sync for battle_20260620_160724.

Source: user confirmation in Codex thread on 2026-06-22:
- 守方按张飞/赵云/马超组填。
- 攻方确认是吕蒙。
Target tables/fields:
- participants: country, level, grade, unit_type, initial_troops, redness,
  gold_seals, innate_skill, innate_skill_redness, skills_text, tactics_text,
  source_note, payload_json for this report.
- damage_contexts: source_context_json and target_context_json participant
  snapshots for the same heroes/report.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"
REPORT_KEY = "battle_20260620_160724"
SOURCE_NOTE = "用户于 2026-06-22 确认 battle_20260620_160724 配置"

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
    "吕蒙": {
        "country": "吴",
        "level": 50,
        "grade": "2",
        "unit_type": "枪兵",
        "initial_troops": 10000,
        "redness": "5红",
        "gold_seals": "2印",
        "innate_skill": "白衣渡江",
        "innate_skill_redness": "2红",
        "skills_text": "白衣渡江（2红）、百战不殆（2红）、普攻",
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


def update_participants(conn: sqlite3.Connection) -> tuple[int, int]:
    report = conn.execute(
        "SELECT id, report_key FROM reports WHERE report_key = ?",
        (REPORT_KEY,),
    ).fetchone()
    if report is None:
        raise RuntimeError(f"Missing report key: {REPORT_KEY}")
    report_id = int(report["id"])

    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id = ?
          AND hero IN ({placeholders(len(CONFIRMED))})
        ORDER BY hero
        """,
        (report_id, *CONFIRMED.keys()),
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

    if updated != len(CONFIRMED):
        raise RuntimeError(f"Expected to update {len(CONFIRMED)} participant rows, updated {updated}")
    return report_id, updated


def load_participants(conn: sqlite3.Connection, report_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id = ?
          AND hero IN ({placeholders(len(CONFIRMED))})
        """,
        (report_id, *CONFIRMED.keys()),
    ).fetchall()
    participants: dict[str, dict[str, Any]] = {}
    for row in rows:
        participants[str(row["hero"])] = {
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


def update_damage_contexts(conn: sqlite3.Connection, report_id: int) -> int:
    participants = load_participants(conn, report_id)
    updated = 0
    for row in conn.execute(
        f"""
        SELECT id, source, target, source_context_json, target_context_json
        FROM damage_contexts
        WHERE report_id = ?
          AND (source IN ({placeholders(len(CONFIRMED))})
               OR target IN ({placeholders(len(CONFIRMED))}))
        """,
        (report_id, *CONFIRMED.keys(), *CONFIRMED.keys()),
    ):
        source_context = row["source_context_json"]
        target_context = row["target_context_json"]
        changed = False
        if str(row["source"]) in participants:
            source_context = update_context_text(source_context, participants[str(row["source"])])
            changed = True
        if str(row["target"]) in participants:
            target_context = update_context_text(target_context, participants[str(row["target"])])
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
            report_id, participants_updated = update_participants(conn)
            contexts_updated = update_damage_contexts(conn, report_id)
        print(f"report_id: {report_id}")
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
