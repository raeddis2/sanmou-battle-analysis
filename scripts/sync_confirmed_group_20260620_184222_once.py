#!/usr/bin/env python3
"""One-time sync for the confirmed battle_20260620_184222 config group.

Source: user confirmation in Codex thread on 2026-06-22.
Target tables/fields:
- participants: country, unit_type, redness, gold_seals, innate_skill,
  innate_skill_redness, skills_text, tactics_text, source_note, payload_json.
- damage_contexts: source_context_json and target_context_json participant
  snapshots for the same heroes/reports.

Run when: after the user confirms this 33-report group shares the same Huang
Zhong/Huang Yueying/Huang Gai/Lu Bu configuration. This script is intentionally
scoped to those report keys and heroes.
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
    "battle_20260620_184222",
    "battle_20260620_200454",
    "battle_20260620_200655",
    "battle_20260620_200730",
    "battle_20260620_200754",
    "battle_20260621_001111",
    "battle_20260621_002001",
    "battle_20260621_002608",
    "battle_20260621_011832",
    "battle_20260621_011908",
    "battle_20260621_011928",
    "battle_20260621_011949",
    "battle_20260621_012039",
    "battle_20260621_021147",
    "battle_20260621_021208",
    "battle_20260621_021231",
    "battle_20260621_021255",
    "battle_20260621_021329",
    "battle_20260621_030544",
    "battle_20260621_030606",
    "battle_20260621_030637",
    "battle_20260621_030657",
    "battle_20260621_030726",
    "battle_20260621_132009",
    "battle_20260621_132022",
    "battle_20260621_132031",
    "battle_20260621_132036",
    "battle_20260621_132048",
    "battle_20260621_144654",
    "battle_20260621_144710",
    "battle_20260621_144715",
    "battle_20260621_144722",
    "battle_20260621_144730",
)

SOURCE_NOTE = "用户于 2026-06-22 确认 battle_20260620_184222 同配置组"

CONFIRMED = {
    "黄忠": {
        "country": "蜀",
        "unit_type": "弓兵",
        "redness": "0红",
        "gold_seals": "0印",
        "innate_skill": "定军扬威",
        "innate_skill_redness": "0红",
        "skills_text": "定军扬威（0红）、锐不可当（0红）、摧坚克难（0红）",
        "tactics_text": "无韬略",
    },
    "黄月英": {
        "country": "蜀",
        "unit_type": "弓兵",
        "redness": "0红",
        "gold_seals": "0印",
        "innate_skill": "木牛流马",
        "innate_skill_redness": "0红",
        "skills_text": "木牛流马（0红）、上兵伐谋（0红）、千里突袭（0红）",
        "tactics_text": "无韬略",
    },
    "黄盖": {
        "country": "吴",
        "unit_type": "枪兵",
        "redness": "0红",
        "gold_seals": "0印",
        "innate_skill": "苦肉计",
        "innate_skill_redness": "0红",
        "skills_text": "苦肉计（0红）、文治武功（0红）、同舟共济（0红）",
        "tactics_text": "无韬略",
    },
    "吕布": {
        "country": "群",
        "unit_type": "骑兵",
        "redness": "5红",
        "gold_seals": "1印",
        "innate_skill": "骁勇无前",
        "innate_skill_redness": "1红",
        "skills_text": "骁勇无前（1红）、三军夺气（3红）、百战不殆（2红）",
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


def participant_payload(row: sqlite3.Row, values: dict[str, str]) -> str:
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
    updated = 0
    for row in conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero IN ({placeholders(len(CONFIRMED))})
        ORDER BY report_id, hero
        """,
        (*report_ids, *CONFIRMED.keys()),
    ):
        values = CONFIRMED[row["hero"]]
        conn.execute(
            """
            UPDATE participants
            SET country = ?,
                unit_type = ?,
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
                values["unit_type"],
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
