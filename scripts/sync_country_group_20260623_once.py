#!/usr/bin/env python3
"""One-time country sync for three 2026-06-22 reports.

Source: user confirmation in Codex thread on 2026-06-23:
黄月英=蜀，黄盖=吴，黄忠=蜀，大乔=吴，马超=蜀。

Target tables/fields:
- participants: country, source_note, payload_json for the five heroes in
  battle_20260622_224501, battle_20260622_224509, battle_20260622_225005.
- damage_contexts: embedded participant snapshots in source_context_json and
  target_context_json for the same reports/heroes.

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

REPORT_KEYS = (
    "battle_20260622_224501",
    "battle_20260622_224509",
    "battle_20260622_225005",
)

COUNTRIES = {
    "黄月英": "蜀",
    "黄盖": "吴",
    "黄忠": "蜀",
    "大乔": "吴",
    "马超": "蜀",
}

SOURCE_NOTE = "用户于 2026-06-23 确认三份 20260622 晚间战报武将国家"


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


def get_report_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        f"""
        SELECT id, report_key
        FROM reports
        WHERE report_key IN ({placeholders(len(REPORT_KEYS))})
        ORDER BY report_key
        """,
        REPORT_KEYS,
    ).fetchall()
    found = {str(row["report_key"]) for row in rows}
    missing = sorted(set(REPORT_KEYS) - found)
    if missing:
        raise RuntimeError(f"Missing report keys: {missing}")
    return [int(row["id"]) for row in rows]


def participant_payload(row: sqlite3.Row, country: str) -> str:
    payload = json_loads(row["payload_json"])
    payload["country"] = country
    payload["source_note"] = SOURCE_NOTE
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def update_participants(conn: sqlite3.Connection, report_ids: list[int]) -> int:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero IN ({placeholders(len(COUNTRIES))})
        ORDER BY report_id, hero
        """,
        (*report_ids, *COUNTRIES.keys()),
    ).fetchall()
    expected = len(report_ids) * len(COUNTRIES)
    if len(rows) != expected:
        raise RuntimeError(f"Expected {expected} participant rows, found {len(rows)}")

    updated = 0
    for row in rows:
        country = COUNTRIES[str(row["hero"])]
        conn.execute(
            """
            UPDATE participants
            SET country = ?,
                source_note = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                country,
                SOURCE_NOTE,
                participant_payload(row, country),
                row["id"],
            ),
        )
        updated += 1
    return updated


def load_participants(conn: sqlite3.Connection, report_ids: list[int]) -> dict[tuple[int, str], dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM participants
        WHERE report_id IN ({placeholders(len(report_ids))})
          AND hero IN ({placeholders(len(COUNTRIES))})
        """,
        (*report_ids, *COUNTRIES.keys()),
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


def update_context_text(text: str | None, participant: dict[str, Any]) -> str:
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
          AND (source IN ({placeholders(len(COUNTRIES))})
               OR target IN ({placeholders(len(COUNTRIES))}))
        """,
        (*report_ids, *COUNTRIES.keys(), *COUNTRIES.keys()),
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


def verify(conn: sqlite3.Connection, report_ids: list[int]) -> None:
    rows = conn.execute(
        f"""
        SELECT r.report_key, p.hero, p.country
        FROM participants p
        JOIN reports r ON r.id = p.report_id
        WHERE p.report_id IN ({placeholders(len(report_ids))})
          AND p.hero IN ({placeholders(len(COUNTRIES))})
        ORDER BY r.report_key, p.hero
        """,
        (*report_ids, *COUNTRIES.keys()),
    ).fetchall()
    bad = [
        (row["report_key"], row["hero"], row["country"], COUNTRIES[str(row["hero"])])
        for row in rows
        if row["country"] != COUNTRIES[str(row["hero"])]
    ]
    if bad:
        raise RuntimeError(f"Country verification failed: {bad}")


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")
    conn = connect()
    try:
        with conn:
            report_ids = get_report_ids(conn)
            participants_updated = update_participants(conn, report_ids)
            contexts_updated = update_damage_contexts(conn, report_ids)
            verify(conn, report_ids)
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
