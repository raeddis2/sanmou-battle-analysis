#!/usr/bin/env python3
"""One-time participant progression normalization.

Source: user clarification in Codex thread on 2026-06-22:
In 三国谋定天下, 品级 = 金印数 = 武将自带战法等级.

Target tables/fields:
- participants: grade, gold_seals, innate_skill_redness, payload_json.
- damage_contexts: embedded participant snapshots for touched heroes/reports.

Run timing: one-time manual data repair after clarifying the project field
definition. This does not change battle events or parsed battle flow.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"
SOURCE_NOTE_SUFFIX = "；2026-06-22 按三谋口径同步品级=金印数=自带战法等级"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def json_loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def count_value(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)\s*(?:印|红)?", text)
    return int(match.group(1)) if match else None


def derive_count(row: sqlite3.Row) -> int | None:
    for field in ("gold_seals", "innate_skill_redness", "grade"):
        count = count_value(row[field])
        if count is not None:
            return count
    return None


def source_note_with_suffix(note: str | None) -> str:
    text = (note or "").strip()
    if "按三谋口径同步品级=金印数=自带战法等级" in text:
        return text
    return f"{text}{SOURCE_NOTE_SUFFIX}" if text else SOURCE_NOTE_SUFFIX.lstrip("；")


def participant_payload(row: sqlite3.Row, grade: str, gold_seals: str, innate_red: str) -> str:
    payload = json_loads(row["payload_json"])
    payload["grade"] = grade
    payload["gold_seals"] = gold_seals
    payload["innate_skill_redness"] = innate_red
    payload["progression_rule"] = "品级=金印数=武将自带战法等级"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_participant(conn: sqlite3.Connection, participant_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Missing participant id: {participant_id}")
    return {
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


def update_context_text(text: str | None, participant: dict[str, Any]) -> str:
    data = json_loads(text)
    if data.get("hero") == participant["hero"]:
        data["participant"] = participant
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def update_damage_contexts(
    conn: sqlite3.Connection,
    touched: dict[tuple[int, str], int],
) -> int:
    participants = {
        key: load_participant(conn, participant_id)
        for key, participant_id in touched.items()
    }
    updated = 0
    report_ids = sorted({report_id for report_id, _hero in touched})
    for report_id in report_ids:
        rows = conn.execute(
            """
            SELECT id, report_id, source, target, source_context_json, target_context_json
            FROM damage_contexts
            WHERE report_id = ?
            """,
            (report_id,),
        ).fetchall()
        for row in rows:
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
        touched: dict[tuple[int, str], int] = {}
        with conn:
            rows = conn.execute(
                """
                SELECT *
                FROM participants
                WHERE grade IS NULL
                   OR TRIM(grade) = ''
                   OR gold_seals GLOB '[0-9]'
                   OR gold_seals GLOB '[0-9][0-9]'
                   OR innate_skill_redness GLOB '[0-9]'
                   OR innate_skill_redness GLOB '[0-9][0-9]'
                ORDER BY report_id, hero
                """
            ).fetchall()
            for row in rows:
                count = derive_count(row)
                if count is None:
                    continue
                grade = str(count)
                gold_seals = f"{count}印"
                innate_red = f"{count}红"
                source_note = source_note_with_suffix(row["source_note"])
                conn.execute(
                    """
                    UPDATE participants
                    SET grade = ?,
                        gold_seals = ?,
                        innate_skill_redness = ?,
                        source_note = ?,
                        payload_json = ?
                    WHERE id = ?
                    """,
                    (
                        grade,
                        gold_seals,
                        innate_red,
                        source_note,
                        participant_payload(row, grade, gold_seals, innate_red),
                        row["id"],
                    ),
                )
                touched[(int(row["report_id"]), str(row["hero"]))] = int(row["id"])
            contexts_updated = update_damage_contexts(conn, touched)
        print(f"participants_updated: {len(touched)}")
        print(f"damage_contexts_updated: {contexts_updated}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
