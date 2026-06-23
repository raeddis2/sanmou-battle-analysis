#!/usr/bin/env python3
"""Import official status notes from user-provided screenshots into SQLite.

Source: screenshots supplied by the user in Codex thread on 2026-06-22 and
transcribed to docs/status_official_notes_20260622.md.

Target tables:
- game_statuses
- game_basic_rules

Run timing: one-time game status knowledge-base repair after official screenshot
confirmation.
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
SOURCE_PATH = ROOT / "docs" / "status_official_notes_20260622.md"
SOURCE_LABEL = "official status screenshots supplied by user on 2026-06-22"

BASIC_RULES: list[dict[str, Any]] = [
    {
        "section_path": "战斗状态说明",
        "category": "战斗状态",
        "name": "状态定义",
        "description_raw": "战斗中，有很多战法的效果会施加在武将身上而不是直接造成伤害，这种持续性效果便是状态",
        "tags": ["状态", "战法"],
    },
    {
        "section_path": "战斗状态说明",
        "category": "战斗状态",
        "name": "持续回合结束时机",
        "description_raw": "武将持有的每种状态都有一定的持续回合，状态会在“持续回合变为 0 后，武将下一次行动前”结束",
        "tags": ["状态", "持续回合", "行动前"],
    },
    {
        "section_path": "战斗状态说明",
        "category": "战斗状态",
        "name": "布阵阶段驱散限制",
        "description_raw": "布阵阶段施加的状态无法被驱散",
        "tags": ["状态", "布阵", "驱散"],
    },
    {
        "section_path": "战斗状态说明",
        "category": "战斗状态",
        "name": "状态大类",
        "description_raw": "战斗中的状态可分为增益（对武将有益的状态）和负面（对武将有害的状态）两大类",
        "tags": ["状态", "增益状态", "负面状态"],
    },
    {
        "section_path": "增益状态",
        "category": "增益状态",
        "name": None,
        "description_raw": "增益状态分为常规增益状态和功能性增益状态",
        "tags": ["增益状态", "常规增益状态", "功能性增益状态"],
    },
    {
        "section_path": "增益状态 > 常规增益状态",
        "category": "增益状态",
        "name": None,
        "description_raw": "某些特殊战法产生增益状态，包括但不限于：造成伤害提升、受到伤害降低和属性提升等",
        "tags": ["增益状态", "造成伤害", "受到伤害", "属性"],
    },
    {
        "section_path": "增益状态 > 功能性增益状态",
        "category": "增益状态",
        "name": None,
        "description_raw": "某些特殊战法产生功能性增益状态，包括但不限于：布阵、整备、列阵等",
        "tags": ["功能性增益状态", "布阵", "整备", "列阵"],
    },
    {
        "section_path": "负面状态",
        "category": "负面状态",
        "name": None,
        "description_raw": "负面状态分为异常状态和常规负面状态；异常状态分为非控制状态和控制状态",
        "tags": ["负面状态", "异常状态", "常规负面状态", "非控制状态", "控制状态"],
    },
    {
        "section_path": "负面状态 > 常规负面状态",
        "category": "负面状态",
        "name": None,
        "description_raw": "某些特殊战法产生的负面状态，包括但不限于：造成伤害降低、受到伤害提升和属性降低等。装备、韬略等非战法施加的减益效果不属于常规负面状态",
        "tags": ["常规负面状态", "造成伤害", "受到伤害", "属性", "装备", "韬略"],
    },
]

STATUSES: list[dict[str, Any]] = [
    {
        "name": "会心",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "造成兵刃伤害时，有概率使该次伤害提升 50%",
        "tags": ["兵刃", "伤害提升"],
        "effect_kind": "damage_boost",
    },
    {
        "name": "奇谋",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "造成谋略伤害时，有概率使该次伤害提升 50%",
        "tags": ["谋略", "伤害提升"],
        "effect_kind": "damage_boost",
    },
    {
        "name": "破甲",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "造成兵刃伤害时，无视目标部分统率",
        "tags": ["兵刃", "统率", "无视属性"],
        "effect_kind": "ignore_defense",
    },
    {
        "name": "看破",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "造成谋略伤害时，无视目标部分智力",
        "tags": ["谋略", "智力", "无视属性"],
        "effect_kind": "ignore_defense",
    },
    {
        "name": "倒戈",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "造成兵刃伤害时，根据伤害恢复自身兵力",
        "tags": ["兵刃", "治疗"],
        "effect_kind": "lifesteal",
    },
    {
        "name": "攻心",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "造成谋略伤害时，根据伤害恢复自身兵力",
        "tags": ["谋略", "治疗"],
        "effect_kind": "lifesteal",
    },
    {
        "name": "连击",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "可额外进行 1 次普通攻击",
        "tags": ["普通攻击"],
        "effect_kind": "extra_normal_attack",
    },
    {
        "name": "反击",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "受到普通攻击后，有概率对攻击方进行 1 次强力普通攻击（不触发追击战法，每回合最多反击 5 次）",
        "tags": ["反击", "追击", "普通攻击"],
        "effect_kind": "counterattack",
    },
    {
        "name": "规避",
        "status_group": "增益状态",
        "status_subgroup": "常规增益状态",
        "description_raw": "受到伤害时，有概率使该次伤害无效",
        "tags": ["受到伤害", "规避"],
        "effect_kind": "avoid_damage",
    },
    {
        "name": "清醒",
        "status_group": "增益状态",
        "status_subgroup": "功能性增益状态",
        "description_raw": "使受到的控制状态暂时失去作用",
        "tags": ["控制状态"],
        "effect_kind": "control_suppress",
    },
    {
        "name": "抵御",
        "status_group": "增益状态",
        "status_subgroup": "功能性增益状态",
        "description_raw": "最多持有 2 层，受到伤害时，消耗 1 层抵御使该次伤害降低 70%-90%",
        "tags": ["受到伤害", "抵御", "伤害降低"],
        "effect_kind": "damage_reduction_stack",
    },
    {
        "name": "必中",
        "status_group": "增益状态",
        "status_subgroup": "功能性增益状态",
        "description_raw": "造成的伤害无法被规避",
        "tags": ["规避"],
        "effect_kind": "bypass_avoid",
    },
    {
        "name": "破御",
        "status_group": "增益状态",
        "status_subgroup": "功能性增益状态",
        "description_raw": "造成的伤害无法被抵御",
        "tags": ["抵御"],
        "effect_kind": "bypass_resist",
    },
    {
        "name": "伏兵",
        "status_group": "增益状态",
        "status_subgroup": "功能性增益状态",
        "description_raw": "受到伤害时，消耗伏兵抵挡部分伤害，并对伤害来源造成逃兵",
        "tags": ["受到伤害", "逃兵"],
        "effect_kind": "damage_block_and_rout",
    },
    {
        "name": "洪水",
        "status_group": "负面状态",
        "status_subgroup": "非控制状态",
        "description_raw": "统率降低 20 点",
        "tags": ["统率", "属性降低"],
        "effect_kind": "attribute_down",
    },
    {
        "name": "火攻",
        "status_group": "负面状态",
        "status_subgroup": "非控制状态",
        "description_raw": "智力降低 15 点",
        "tags": ["智力", "属性降低"],
        "effect_kind": "attribute_down",
    },
    {
        "name": "风暴",
        "status_group": "负面状态",
        "status_subgroup": "非控制状态",
        "description_raw": "先攻降低 30 点",
        "tags": ["先攻", "属性降低"],
        "effect_kind": "attribute_down",
    },
    {
        "name": "畏惧",
        "status_group": "负面状态",
        "status_subgroup": "非控制状态",
        "description_raw": "受到伤害提升 10%",
        "tags": ["受到伤害"],
        "effect_kind": "damage_taken_up",
    },
    {
        "name": "妖术",
        "status_group": "负面状态",
        "status_subgroup": "非控制状态",
        "description_raw": "会心和奇谋伤害降低 15%",
        "tags": ["会心", "奇谋", "伤害降低"],
        "effect_kind": "critical_damage_down",
    },
    {
        "name": "震慑",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "无法发动主动战法和普通攻击",
        "tags": ["主动", "普通攻击"],
        "effect_kind": "disable_active_and_normal_attack",
    },
    {
        "name": "缴械",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "无法普通攻击",
        "tags": ["普通攻击"],
        "effect_kind": "disable_normal_attack",
    },
    {
        "name": "技穷",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "无法发动主动战法",
        "tags": ["主动"],
        "effect_kind": "disable_active_skill",
    },
    {
        "name": "混乱",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "普通攻击、追击战法和主动战法无差别选择目标",
        "tags": ["普通攻击", "追击", "主动", "目标选择"],
        "effect_kind": "random_targeting",
    },
    {
        "name": "嘲讽",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "强制普通攻击嘲讽施加者",
        "tags": ["普通攻击", "目标选择"],
        "effect_kind": "forced_normal_attack_target",
    },
    {
        "name": "虚弱",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "造成的最终伤害降低 70%",
        "tags": ["最终伤害", "伤害降低"],
        "effect_kind": "final_damage_down",
    },
    {
        "name": "断粮",
        "status_group": "负面状态",
        "status_subgroup": "控制状态",
        "description_raw": "受到的恢复兵力效果降低 70%",
        "tags": ["治疗", "恢复兵力"],
        "effect_kind": "healing_received_down",
    },
]


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def extract_numbers(text: str) -> list[dict[str, Any]]:
    numbers: list[dict[str, Any]] = []
    pattern = re.compile(r"(?P<num>\d+(?:\.\d+)?)(?P<unit>\s*(?:%|点|层|次|回合|人|个|级))?")
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


def status_basic_rule(entry: dict[str, Any]) -> dict[str, Any]:
    subgroup = entry["status_subgroup"]
    if entry["status_group"] == "增益状态":
        section_path = f"增益状态 > {subgroup}"
    elif subgroup == "非控制状态":
        section_path = "负面状态 > 异常状态 > 非控制状态"
    elif subgroup == "控制状态":
        section_path = "负面状态 > 异常状态 > 控制状态"
    else:
        section_path = f"负面状态 > {subgroup}"
    return {
        "section_path": section_path,
        "category": entry["status_group"],
        "name": entry["name"],
        "description_raw": entry["description_raw"],
        "tags": entry["tags"],
    }


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def replace_status_basic_rules(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        DELETE FROM game_basic_rules
        WHERE category IN ('战斗状态', '增益状态', '负面状态')
        """
    )
    return int(cursor.rowcount)


def upsert_basic_rule(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
    payload = {
        **entry,
        "source": SOURCE_LABEL,
        "source_path": str(SOURCE_PATH),
    }
    conn.execute(
        """
        INSERT INTO game_basic_rules (
            section_path, category, name, description_raw, source_path,
            numbers_json, tags_json, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(section_path, category, name, description_raw) DO UPDATE SET
            source_path = excluded.source_path,
            numbers_json = excluded.numbers_json,
            tags_json = excluded.tags_json,
            payload_json = excluded.payload_json
        """,
        (
            entry["section_path"],
            entry["category"],
            entry.get("name"),
            entry["description_raw"],
            str(SOURCE_PATH),
            json_dumps(extract_numbers(entry["description_raw"])),
            json_dumps(entry.get("tags", [])),
            json_dumps(payload),
        ),
    )


def upsert_status(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
    payload = {
        **entry,
        "source": SOURCE_LABEL,
        "source_path": str(SOURCE_PATH),
    }
    conn.execute(
        """
        INSERT INTO game_statuses (
            name, status_group, status_subgroup, description_raw,
            source_path, numbers_json, tags_json, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            status_group = excluded.status_group,
            status_subgroup = excluded.status_subgroup,
            description_raw = excluded.description_raw,
            source_path = excluded.source_path,
            numbers_json = excluded.numbers_json,
            tags_json = excluded.tags_json,
            payload_json = excluded.payload_json
        """,
        (
            entry["name"],
            entry["status_group"],
            entry["status_subgroup"],
            entry["description_raw"],
            str(SOURCE_PATH),
            json_dumps(extract_numbers(entry["description_raw"])),
            json_dumps(entry["tags"]),
            json_dumps(payload),
        ),
    )


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")
    if not SOURCE_PATH.exists():
        raise SystemExit(f"Source notes not found: {SOURCE_PATH}")

    conn = connect()
    try:
        with conn:
            deleted = replace_status_basic_rules(conn)
            rules = [*BASIC_RULES, *(status_basic_rule(entry) for entry in STATUSES)]
            for entry in rules:
                upsert_basic_rule(conn, entry)
            for entry in STATUSES:
                upsert_status(conn, entry)

        print(f"status_basic_rules_deleted: {deleted}")
        print(f"status_basic_rules_imported: {len(rules)}")
        print(f"statuses_upserted: {len(STATUSES)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
