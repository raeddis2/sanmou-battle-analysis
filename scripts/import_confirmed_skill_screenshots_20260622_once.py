#!/usr/bin/env python3
"""Import user-confirmed skill screenshot data into SQLite.

Source: screenshots supplied by the user in Codex thread on 2026-06-22 and
transcribed to docs/skill_screenshot_confirmations_20260622.md.

Target tables:
- game_skills
- game_skill_versions

Run timing: one-time skill knowledge-base repair after screenshot confirmation.
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
SOURCE_PATH = ROOT / "docs" / "skill_screenshot_confirmations_20260622.md"

ENTRIES = [
    {
        "skill_name": "骁勇无前",
        "red_level_text": "1红",
        "red_level": 1,
        "skill_level": "10级，满级",
        "skill_type": "主动",
        "skill_feature": "兵刃",
        "probability_text": "41%",
        "probability_pct": 41.0,
        "description_raw": "与敌军全体相互进行 1 次普攻（互相普攻过程中自身免疫缴械状态）。若自身武力值高于目标，则额外造成 100% 兵刃伤害",
    },
    {
        "skill_name": "红妆缭乱",
        "red_level_text": "3红",
        "red_level": 3,
        "skill_level": "10级，满级",
        "skill_type": "追击",
        "skill_feature": "兵刃",
        "probability_text": "70%",
        "probability_pct": 70.0,
        "description_raw": "普通攻击后，对攻击目标施加畏惧，持续 2 回合，然后造成 239.8% 兵刃伤害，有 87.2% 概率额外造成 239.8% 兵刃伤害",
    },
    {
        "skill_name": "三军夺气",
        "red_level_text": "3红",
        "red_level": 3,
        "skill_level": "10级，满级",
        "skill_type": "追击",
        "skill_feature": "辅助",
        "probability_text": "60%",
        "probability_pct": 60.0,
        "description_raw": "普通攻击后，使攻击目标武力、智力、统率降低 32.7 点，持续 3 回合，最多可叠加 5 次",
    },
    {
        "skill_name": "七进七出",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "被动",
        "skill_feature": "兵刃",
        "probability_text": "100%",
        "probability_pct": 100.0,
        "description_raw": "提升自身 35% 规避率，成功规避后触发龙胆：立刻对敌军随机两人造成 90% 兵刃伤害，当前回合下一次龙胆的伤害系数降低 10%。龙胆每个回合可触发 7 次",
    },
    {
        "skill_name": "辕门射戟",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "追击",
        "skill_feature": "兵刃",
        "probability_text": "70%",
        "probability_pct": 70.0,
        "description_raw": "普通攻击后，对目标造成 220% 兵刃伤害。若自身为后排，则有 75% 概率提升自身 10% 主动战法发动率，持续 2 回合，可叠加 3 次",
    },
    {
        "skill_name": "清风驱疾",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "主动",
        "skill_feature": "治疗",
        "probability_text": "60%",
        "probability_pct": 60.0,
        "description_raw": "驱散我军随机两人 1 种负面状态，并恢复其兵力（治疗率 180%，受智力影响）",
    },
    {
        "skill_name": "万人之敌",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "主动",
        "skill_feature": "兵刃",
        "probability_text": "55%",
        "probability_pct": 55.0,
        "description_raw": "对敌军全体造成 140% 兵刃伤害，并施加畏惧，持续 2 回合。若目标已持有畏惧状态，则有 30% 概率造成震慑，持续 1 回合",
    },
    {
        "skill_name": "水淹七军",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "主动",
        "skill_feature": "兵刃",
        "probability_text": "40%",
        "probability_pct": 40.0,
        "description_raw": "准备 1 回合，对敌军全体施加洪水，持续 2 回合，并造成 260% 兵刃伤害，并有 65% 概率施加技穷、缴械状态，每种状态独立判断，持续 1 回合",
    },
    {
        "skill_name": "趁火打劫",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "主动",
        "skill_feature": "文武",
        "probability_text": "50%",
        "probability_pct": 50.0,
        "description_raw": "准备 1 回合，对敌军随机两人造成 220% 谋略和兵刃伤害，若目标处于火攻状态，则额外施加混乱状态，持续 2 回合",
    },
    {
        "skill_name": "纵马横枪",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "被动",
        "skill_feature": "兵刃",
        "probability_text": "100%",
        "probability_pct": 100.0,
        "description_raw": "提升自身 45% 会心几率，造成会心伤害后对目标发动追伤：造成 1 次 60% 无视统率的兵刃伤害（无法触发会心）。若目标持有负面状态追伤伤害提升 20%，追伤每个回合可触发 5 次",
    },
    {
        "skill_name": "锐不可当",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "被动",
        "skill_feature": "辅助",
        "probability_text": "100%",
        "probability_pct": 100.0,
        "description_raw": "提升自身 16% 破甲和 35% 造成伤害",
    },
    {
        "skill_name": "以静制动",
        "red_level_text": "0红",
        "red_level": 0,
        "skill_level": "10级，满级",
        "skill_type": "被动",
        "skill_feature": "防御",
        "probability_text": "100%",
        "probability_pct": 100.0,
        "description_raw": "奇数回合，自身无法进行普通攻击，受到兵刃伤害、普通攻击伤害、追击战法伤害减少 35%；偶数回合，自身无法发动主动战法，受到谋略伤害、主动战法伤害减少 35%",
    },
]


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def extract_numbers(text: str) -> list[dict[str, Any]]:
    numbers: list[dict[str, Any]] = []
    pattern = re.compile(r"(?P<num>-?\d+(?:\.\d+)?)(?P<unit>\s*(?:%|点|层|次|回合|人|个|级))?")
    for match in pattern.finditer(text):
        raw = match.group(0).strip()
        unit = (match.group("unit") or "").strip()
        numbers.append(
            {
                "raw": raw,
                "value": float(match.group("num")),
                "unit": unit,
            }
        )
    return numbers


def infer_tags(entry: dict[str, Any]) -> list[str]:
    tags: set[str] = set()
    for value in (entry["skill_type"], entry["skill_feature"], entry["description_raw"]):
        text = str(value)
        for term in (
            "主动",
            "追击",
            "指挥",
            "被动",
            "兵刃",
            "谋略",
            "治疗",
            "文武",
            "防御",
            "辅助",
            "普攻",
            "规避",
            "会心",
            "破甲",
            "缴械",
            "畏惧",
            "震慑",
            "洪水",
            "技穷",
            "混乱",
            "火攻",
            "降低属性",
            "叠加",
        ):
            if term in text:
                tags.add(term)
        if "降低" in text and any(prop in text for prop in ("武力", "智力", "统率")):
            tags.add("降低属性")
    return sorted(tags)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def upsert_entry(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
    name = entry["skill_name"]
    conn.execute(
        """
        INSERT INTO game_skills (name, source_path, first_seen_order, notes, payload_json)
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            source_path = excluded.source_path,
            notes = excluded.notes,
            payload_json = excluded.payload_json
        """,
        (
            name,
            str(SOURCE_PATH),
            "用户截图确认",
            json_dumps({"source": "user screenshot confirmation", "date": "2026-06-22"}),
        ),
    )
    skill_id = int(conn.execute("SELECT id FROM game_skills WHERE name = ?", (name,)).fetchone()["id"])
    payload = {
        **entry,
        "source": "user screenshot confirmation",
        "source_path": str(SOURCE_PATH),
        "unit_types": "盾兵、弓兵、枪兵、骑兵",
    }
    conn.execute(
        """
        INSERT INTO game_skill_versions (
            skill_id, skill_name, red_level_text, red_level, skill_level,
            skill_type, skill_feature, probability_text, probability_pct,
            description_raw, numbers_json, tags_json, source_path,
            payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(skill_name, red_level_text) DO UPDATE SET
            skill_id = excluded.skill_id,
            red_level = excluded.red_level,
            skill_level = excluded.skill_level,
            skill_type = excluded.skill_type,
            skill_feature = excluded.skill_feature,
            probability_text = excluded.probability_text,
            probability_pct = excluded.probability_pct,
            description_raw = excluded.description_raw,
            numbers_json = excluded.numbers_json,
            tags_json = excluded.tags_json,
            source_path = excluded.source_path,
            payload_json = excluded.payload_json
        """,
        (
            skill_id,
            name,
            entry["red_level_text"],
            entry["red_level"],
            entry["skill_level"],
            entry["skill_type"],
            entry["skill_feature"],
            entry["probability_text"],
            entry["probability_pct"],
            entry["description_raw"],
            json_dumps(extract_numbers(entry["description_raw"])),
            json_dumps(infer_tags(entry)),
            str(SOURCE_PATH),
            json_dumps(payload),
        ),
    )


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")
    conn = connect()
    try:
        with conn:
            for entry in ENTRIES:
                upsert_entry(conn, entry)
        print(f"skill_versions_upserted: {len(ENTRIES)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
