#!/usr/bin/env python3
"""One-time inheritance sync for battle_20260622_024136.

Source: user confirmation in Codex thread on 2026-06-22:
"之前黄忠/黄月英/黄盖 + 大乔/马超那组填".
Source report: battle_20260621_144741, a verified complete report with the same
黄忠/黄月英/黄盖 + 大乔/马超 configuration.

Target tables/fields:
- participants: configuration fields for the five confirmed heroes in
  battle_20260622_024136.
- damage_contexts: embedded participant snapshots in source_context_json and
  target_context_json for the same report/heroes.

Run timing: one-time manual data repair after user confirmation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sanmou_battles.sqlite"
SOURCE_REPORT_KEY = "battle_20260621_144741"
TARGET_REPORT_KEY = "battle_20260622_024136"
SOURCE_NOTE = (
    "用户于 2026-06-22 确认 battle_20260622_024136 "
    "沿用 battle_20260621_144741 同配置组"
)
HEROES = ("黄忠", "黄月英", "黄盖", "大乔", "马超")

CONFIG_FIELDS = (
    "country",
    "level",
    "redness",
    "grade",
    "gold_seals",
    "unit_type",
    "initial_troops",
    "innate_skill",
    "innate_skill_redness",
    "skills_text",
    "tactics_text",
)


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


def get_report_id(conn: sqlite3.Connection, report_key: str) -> int:
    row = conn.execute("SELECT id FROM reports WHERE report_key = ?", (report_key,)).fetchone()
    if row is None:
        raise RuntimeError(f"Missing report key: {report_key}")
    return int(row["id"])


def load_source_config(conn: sqlite3.Connection, source_report_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id = ?
          AND hero IN ({placeholders(len(HEROES))})
        """,
        (source_report_id, *HEROES),
    ).fetchall()
    config: dict[str, dict[str, Any]] = {}
    for row in rows:
        config[str(row["hero"])] = {field: row[field] for field in CONFIG_FIELDS}
    if set(config) != set(HEROES):
        missing = sorted(set(HEROES) - set(config))
        raise RuntimeError(f"Source report is missing heroes: {missing}")
    return config


def payload_with_config(row: sqlite3.Row, config: dict[str, Any]) -> str:
    payload = json_loads(row["payload_json"])
    payload.update({field: config[field] for field in CONFIG_FIELDS})
    payload["source_note"] = SOURCE_NOTE
    payload["source_report_key"] = SOURCE_REPORT_KEY
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def update_participants(
    conn: sqlite3.Connection,
    target_report_id: int,
    config_by_hero: dict[str, dict[str, Any]],
) -> int:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id = ?
          AND hero IN ({placeholders(len(HEROES))})
        ORDER BY hero
        """,
        (target_report_id, *HEROES),
    ).fetchall()
    updated = 0
    for row in rows:
        hero = str(row["hero"])
        config = config_by_hero[hero]
        conn.execute(
            """
            UPDATE participants
            SET country = ?,
                level = ?,
                redness = ?,
                grade = ?,
                gold_seals = ?,
                unit_type = ?,
                initial_troops = ?,
                innate_skill = ?,
                innate_skill_redness = ?,
                skills_text = ?,
                tactics_text = ?,
                source_note = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                config["country"],
                config["level"],
                config["redness"],
                config["grade"],
                config["gold_seals"],
                config["unit_type"],
                config["initial_troops"],
                config["innate_skill"],
                config["innate_skill_redness"],
                config["skills_text"],
                config["tactics_text"],
                SOURCE_NOTE,
                payload_with_config(row, config),
                row["id"],
            ),
        )
        updated += 1
    if updated != len(HEROES):
        missing = sorted(set(HEROES) - {str(row["hero"]) for row in rows})
        raise RuntimeError(f"Expected {len(HEROES)} target rows, updated {updated}; missing: {missing}")
    return updated


def load_participants(conn: sqlite3.Connection, report_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id = ?
          AND hero IN ({placeholders(len(HEROES))})
        """,
        (report_id, *HEROES),
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


def update_context_text(text: str | None, participant: dict[str, Any]) -> str:
    data = json_loads(text)
    if data.get("hero") == participant["hero"]:
        data["participant"] = participant
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def update_damage_contexts(conn: sqlite3.Connection, target_report_id: int) -> int:
    participants = load_participants(conn, target_report_id)
    updated = 0
    for row in conn.execute(
        f"""
        SELECT id, source, target, source_context_json, target_context_json
        FROM damage_contexts
        WHERE report_id = ?
          AND (source IN ({placeholders(len(HEROES))})
               OR target IN ({placeholders(len(HEROES))}))
        """,
        (target_report_id, *HEROES, *HEROES),
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
            source_report_id = get_report_id(conn, SOURCE_REPORT_KEY)
            target_report_id = get_report_id(conn, TARGET_REPORT_KEY)
            source_config = load_source_config(conn, source_report_id)
            participants_updated = update_participants(conn, target_report_id, source_config)
            contexts_updated = update_damage_contexts(conn, target_report_id)
        print(f"source_report_id: {source_report_id}")
        print(f"target_report_id: {target_report_id}")
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
