"""Build a queryable SQLite battle database with damage-time state snapshots.

Battle events are imported from raw captured text. Markdown reports are only
used as human-review companions for manually confirmed setup fields such as
unit type, redness, seals, skill order, and tactic details.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sanmou.report_parser import (
    collect_report_heroes,
    initial_troops_for,
    max_observed_troops_for,
    parse_capture,
    tactics_text_for,
    unit_type_for,
)
from sanmou.rules import (
    DEFAULT_HERO_LEVEL,
    NPC_REDNESS,
    PLAYER_MAX_TROOPS,
    apply_npc_defaults,
    derive_grade,
    is_npc_initial_troops,
    is_npc_row,
    normalize_gold_seals,
    normalize_progression_redness,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "sanmou_battles.sqlite"
DEFAULT_CAPTURED_DIR = PROJECT_ROOT / "data" / "raw_captures"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_SKILLS_DOC = PROJECT_ROOT / "docs" / "skills_database.md"
DEFAULT_TACTICS_DOC = PROJECT_ROOT / "docs" / "tactics_database.md"
DEFAULT_BASICS_DOC = PROJECT_ROOT / "docs" / "battle_basics.md"

COMMON_PROP_COLUMNS = {
    "force": "武力",
    "intelligence": "智力",
    "command": "统率",
    "initiative": "先攻",
    "damage_pct": "造成伤害",
    "damage_taken_pct": "受到伤害",
    "crit_chance_pct": "会心几率",
    "crit_damage_pct": "会心伤害",
    "pierce_pct": "破甲",
    "combo_pct": "连击率",
    "lifesteal_pct": "倒戈",
    "counter_pct": "反击触发率",
    "avoid_pct": "规避",
}

STATE_EVENT_TYPES = {
    "property",
    "buff_apply",
    "buff_refresh",
    "buff_expire",
    "buff_stack",
    "buff_temporarily_invalid",
    "trigger",
    "damage_mod",
    "heal",
    "damage",
    "damage_raw",
    "death",
}

SKILL_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
VERSION_HEADING_RE = re.compile(r"^####\s+(.+?)\s*$")
TACTIC_GROUP_RE = re.compile(r"^###\s+(.+?)\s*$")
TACTIC_ROW_RE = re.compile(r"^-\s*《(.+?)》([^：:]*)[：:]\s*(.+?)\s*$")
BASIC_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BASIC_EXCLUDED_HEADINGS = {"增减伤乘区当前结论"}
INFERRED_RULE_ROOT = "增减伤乘区当前结论"
BASIC_SPECULATIVE_TERMS = (
    "高可能性推测",
    "不保证",
    "后续仍需",
    "后续样本",
    "待后续",
    "当前较稳结论",
    "暂按",
    "拟合",
    "样本",
    "假设",
    "H(N)",
)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setup_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY,
            report_key TEXT NOT NULL UNIQUE,
            source_hash TEXT NOT NULL UNIQUE,
            capture_path TEXT NOT NULL,
            markdown_path TEXT,
            raw_count INTEGER NOT NULL,
            parsed_count INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            dedup_mode TEXT NOT NULL,
            parser_name TEXT NOT NULL DEFAULT 'sanmou.report_parser',
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            side TEXT,
            team_id TEXT,
            hero TEXT NOT NULL,
            country TEXT,
            level INTEGER,
            grade TEXT,
            unit_type TEXT,
            initial_troops INTEGER,
            redness TEXT,
            gold_seals TEXT,
            innate_skill TEXT,
            innate_skill_redness TEXT,
            skills_text TEXT,
            tactics_text TEXT,
            source_note TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(report_id, side, team_id, hero)
        );

        CREATE TABLE IF NOT EXISTS report_skill_details (
            id INTEGER PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            skill_name TEXT NOT NULL,
            side TEXT,
            hero TEXT,
            redness TEXT,
            grade TEXT,
            skill_type TEXT,
            unit_types TEXT,
            description TEXT,
            source_text TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            event_order INTEGER NOT NULL,
            raw_index INTEGER,
            section TEXT,
            round_no INTEGER,
            event_type TEXT NOT NULL,
            hero TEXT,
            source TEXT,
            target TEXT,
            skill TEXT,
            buff TEXT,
            damage INTEGER,
            heal INTEGER,
            remain INTEGER,
            raw_text TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(report_id, event_order)
        );

        CREATE TABLE IF NOT EXISTS state_changes (
            id INTEGER PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            event_order INTEGER NOT NULL,
            raw_index INTEGER,
            round_no INTEGER,
            hero TEXT,
            change_type TEXT NOT NULL,
            prop TEXT,
            direction TEXT,
            value_text TEXT,
            value_num REAL,
            result_text TEXT,
            result_num REAL,
            skill TEXT,
            buff TEXT,
            active_after INTEGER,
            raw_text TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS damage_contexts (
            id INTEGER PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            event_order INTEGER NOT NULL,
            raw_index INTEGER,
            section TEXT,
            round_no INTEGER,
            damage_event_type TEXT NOT NULL,
            source TEXT,
            target TEXT NOT NULL,
            skill TEXT,
            buff TEXT,
            damage INTEGER NOT NULL,
            remain INTEGER,
            target_hp_before INTEGER,
            target_hp_after INTEGER,
            action_type TEXT,
            action_skill TEXT,
            action_buff TEXT,
            action_target TEXT,
            recent_trigger TEXT,
            source_force REAL,
            source_intelligence REAL,
            source_command REAL,
            source_initiative REAL,
            source_damage_pct REAL,
            source_damage_taken_pct REAL,
            source_crit_chance_pct REAL,
            source_crit_damage_pct REAL,
            source_pierce_pct REAL,
            source_combo_pct REAL,
            source_lifesteal_pct REAL,
            source_counter_pct REAL,
            source_avoid_pct REAL,
            target_force REAL,
            target_intelligence REAL,
            target_command REAL,
            target_initiative REAL,
            target_damage_pct REAL,
            target_damage_taken_pct REAL,
            target_crit_chance_pct REAL,
            target_crit_damage_pct REAL,
            target_pierce_pct REAL,
            target_combo_pct REAL,
            target_lifesteal_pct REAL,
            target_counter_pct REAL,
            target_avoid_pct REAL,
            source_context_json TEXT NOT NULL,
            target_context_json TEXT NOT NULL,
            source_active_buffs_json TEXT NOT NULL,
            target_active_buffs_json TEXT NOT NULL,
            action_context_json TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_report_order
            ON events(report_id, event_order);
        CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_actor
            ON events(source, target, hero);
        CREATE INDEX IF NOT EXISTS idx_state_changes_hero_prop
            ON state_changes(report_id, hero, prop, event_order);
        CREATE INDEX IF NOT EXISTS idx_damage_contexts_lookup
            ON damage_contexts(source, target, skill, buff);
        CREATE INDEX IF NOT EXISTS idx_damage_contexts_report_order
            ON damage_contexts(report_id, event_order);

        CREATE TABLE IF NOT EXISTS game_skills (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            first_seen_order INTEGER,
            notes TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_skill_versions (
            id INTEGER PRIMARY KEY,
            skill_id INTEGER NOT NULL REFERENCES game_skills(id) ON DELETE CASCADE,
            skill_name TEXT NOT NULL,
            red_level_text TEXT NOT NULL,
            red_level INTEGER,
            skill_level TEXT,
            skill_type TEXT,
            skill_feature TEXT,
            probability_text TEXT,
            probability_pct REAL,
            description_raw TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(skill_name, red_level_text)
        );

        CREATE TABLE IF NOT EXISTS game_tactics (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            quality TEXT,
            group_name TEXT NOT NULL,
            description_raw TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(name, quality, group_name, description_raw)
        );

        CREATE TABLE IF NOT EXISTS game_basic_rules (
            id INTEGER PRIMARY KEY,
            section_path TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(section_path, category, name, description_raw)
        );

        CREATE TABLE IF NOT EXISTS game_properties (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_formations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            formation_group TEXT,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_camps (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_camp_bonuses (
            id INTEGER PRIMARY KEY,
            same_camp_count INTEGER NOT NULL UNIQUE,
            all_attribute_pct REAL NOT NULL,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_unit_types (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_unit_counters (
            id INTEGER PRIMARY KEY,
            attacker_unit TEXT NOT NULL,
            defender_unit TEXT NOT NULL,
            damage_multiplier REAL NOT NULL,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(attacker_unit, defender_unit)
        );

        CREATE TABLE IF NOT EXISTS game_unit_bonuses (
            id INTEGER PRIMARY KEY,
            unit_type TEXT NOT NULL,
            same_unit_count INTEGER NOT NULL,
            damage_pct REAL,
            damage_taken_pct REAL,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(unit_type, same_unit_count)
        );

        CREATE TABLE IF NOT EXISTS game_bonds (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            condition_text TEXT,
            heroes_text TEXT,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_statuses (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            status_group TEXT NOT NULL,
            status_subgroup TEXT,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS game_skill_categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category_type TEXT NOT NULL,
            description_raw TEXT NOT NULL,
            source_path TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(name, category_type)
        );

        CREATE TABLE IF NOT EXISTS game_inferred_rules (
            id INTEGER PRIMARY KEY,
            section_path TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT,
            rule_status TEXT NOT NULL DEFAULT 'inferred',
            confidence_level TEXT NOT NULL,
            confidence_score REAL,
            description_raw TEXT NOT NULL,
            evidence_raw TEXT,
            source_path TEXT NOT NULL,
            numbers_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(section_path, category, name, description_raw)
        );

        CREATE INDEX IF NOT EXISTS idx_game_skill_versions_lookup
            ON game_skill_versions(skill_name, red_level);
        CREATE INDEX IF NOT EXISTS idx_game_skill_versions_type
            ON game_skill_versions(skill_type, skill_feature);
        CREATE INDEX IF NOT EXISTS idx_game_tactics_lookup
            ON game_tactics(name, quality, group_name);
        CREATE INDEX IF NOT EXISTS idx_game_basic_rules_category
            ON game_basic_rules(category, name);
        CREATE INDEX IF NOT EXISTS idx_game_statuses_group
            ON game_statuses(status_group, status_subgroup);
        CREATE INDEX IF NOT EXISTS idx_game_inferred_rules_category
            ON game_inferred_rules(category, confidence_level);

        DROP VIEW IF EXISTS damage_with_skill_info;
        CREATE VIEW damage_with_skill_info AS
        WITH participant_one AS (
            SELECT
                p.*,
                ROW_NUMBER() OVER (
                    PARTITION BY p.report_id, p.hero
                    ORDER BY p.id
                ) AS rn
            FROM participants p
        ),
        skill_version_candidates AS (
            SELECT
                d.id AS damage_context_id,
                v.id AS version_id,
                ROW_NUMBER() OVER (
                    PARTITION BY d.id
                    ORDER BY
                        CASE
                            WHEN COALESCE(p.skills_text, '') LIKE '%' || d.skill || char(65288) || v.red_level_text || char(65289) || '%' THEN 0
                            WHEN COALESCE(p.skills_text, '') LIKE '%' || d.skill || '(' || v.red_level_text || ')%' THEN 1
                            WHEN p.innate_skill = d.skill AND p.innate_skill_redness = v.red_level_text THEN 2
                            WHEN v.red_level = 0 THEN 10
                            ELSE 20
                        END,
                        v.red_level DESC,
                        v.id
                ) AS rn
            FROM damage_contexts d
            LEFT JOIN participant_one p
                ON p.report_id = d.report_id
               AND p.hero = d.source
               AND p.rn = 1
            JOIN game_skill_versions v
                ON v.skill_name = d.skill
        )
        SELECT
            d.*,
            r.report_key,
            p.country AS source_country,
            p.unit_type AS source_unit_type,
            p.redness AS source_redness,
            p.gold_seals AS source_gold_seals,
            p.innate_skill AS source_innate_skill,
            p.innate_skill_redness AS source_innate_skill_redness,
            p.skills_text AS source_skills_text,
            p.tactics_text AS source_tactics_text,
            v.red_level_text AS knowledge_red_level_text,
            v.red_level AS knowledge_red_level,
            v.skill_type AS knowledge_skill_type,
            v.skill_feature AS knowledge_skill_feature,
            v.probability_text AS knowledge_probability_text,
            v.probability_pct AS knowledge_probability_pct,
            v.description_raw AS knowledge_description_raw,
            v.numbers_json AS knowledge_numbers_json,
            v.tags_json AS knowledge_tags_json
        FROM damage_contexts d
        JOIN reports r ON r.id = d.report_id
        LEFT JOIN participant_one p
            ON p.report_id = d.report_id
           AND p.hero = d.source
           AND p.rn = 1
        LEFT JOIN skill_version_candidates c
            ON c.damage_context_id = d.id
           AND c.rn = 1
        LEFT JOIN game_skill_versions v
            ON v.id = c.version_id;
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS event_search
            USING fts5(report_id UNINDEXED, event_id UNINDEXED, raw_text)
            """
        )
    except sqlite3.OperationalError:
        # Some Python/SQLite builds omit FTS5.  The structured tables still work.
        pass
    ensure_column(conn, "participants", "grade", "TEXT")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace("%", "").replace("−", "-")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"-?\d+", value.replace(",", ""))
    return int(m.group(0)) if m else None


def normalize_doc_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_sentence_punctuation(text: str) -> str:
    return text.strip().rstrip("。.;；").strip()


def parse_red_level(value: str | None) -> int | None:
    return parse_int(value)


def normalize_participant_progression(row: dict[str, Any]) -> dict[str, Any]:
    row["gold_seals"] = normalize_gold_seals(row.get("gold_seals"))
    row["innate_skill_redness"] = normalize_progression_redness(row.get("innate_skill_redness"))
    row["grade"] = derive_grade(
        grade=row.get("grade"),
        gold_seals=row.get("gold_seals"),
        innate_skill_redness=row.get("innate_skill_redness"),
    )
    payload = row.get("payload_json")
    if isinstance(payload, dict):
        payload["gold_seals"] = row.get("gold_seals")
        payload["innate_skill_redness"] = row.get("innate_skill_redness")
        payload["grade"] = row.get("grade")
    return row


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
                "start": match.start(),
                "end": match.end(),
            }
        )
    return numbers


def infer_tags(text: str, *, skill_type: str | None = None, skill_feature: str | None = None) -> list[str]:
    tag_terms = {
        "兵刃": "兵刃",
        "谋略": "谋略",
        "治疗": "治疗",
        "恢复": "治疗",
        "会心": "会心",
        "奇谋": "奇谋",
        "破甲": "破甲",
        "倒戈": "倒戈",
        "攻心": "攻心",
        "规避": "规避",
        "反击": "反击",
        "连击": "连击",
        "追击": "追击",
        "主动": "主动",
        "指挥": "指挥",
        "被动": "被动",
        "受到伤害": "受到伤害",
        "造成伤害": "造成伤害",
        "发动率": "发动率",
        "发动概率": "发动率",
        "普通攻击": "普通攻击",
        "普攻": "普通攻击",
        "回合开始": "回合开始",
        "回合结束": "回合结束",
        "行动结束": "行动结束",
        "行动时": "行动时",
        "持续": "持续",
        "叠加": "叠加",
        "无视统率": "无视统率",
        "受智力影响": "受智力影响",
        "受武力影响": "受武力影响",
        "受统率影响": "受统率影响",
        "受先攻影响": "受先攻影响",
        "逃兵": "逃兵",
        "断粮": "断粮",
        "畏惧": "畏惧",
        "技穷": "技穷",
        "缴械": "缴械",
        "震慑": "震慑",
        "火攻": "火攻",
        "洪水": "洪水",
        "抵御": "抵御",
    }
    tags: list[str] = []
    for source in (skill_type, skill_feature):
        if source and source not in tags:
            tags.append(source)
    for term, tag in tag_terms.items():
        if term in text and tag not in tags:
            tags.append(tag)
    return tags


def parse_bullet_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        text = line.strip()
        if not text.startswith("-"):
            continue
        item = text.lstrip("-").strip()
        if "：" in item:
            key, value = item.split("：", 1)
        elif ":" in item:
            key, value = item.split(":", 1)
        else:
            continue
        fields[key.strip()] = strip_sentence_punctuation(value)
    return fields


def parse_skill_doc(path: Path) -> list[dict[str, Any]]:
    text = normalize_doc_text(path.read_text(encoding="utf-8-sig"))
    lines = text.splitlines()
    entries: list[dict[str, Any]] = []
    current_skill: str | None = None
    current_version: str | None = None
    version_lines: list[str] = []
    order = 0

    def flush() -> None:
        nonlocal version_lines, order
        if not current_skill or not current_version:
            version_lines = []
            return
        fields = parse_bullet_fields(version_lines)
        description = fields.get("战法说明", "")
        skill_type = fields.get("战法类型", "")
        skill_feature = fields.get("战法特性", "")
        probability_text = fields.get("发动概率", "")
        probability_pct = parse_number(probability_text)
        order += 1
        entries.append(
            {
                "name": current_skill,
                "red_level_text": current_version,
                "red_level": parse_red_level(current_version),
                "skill_level": fields.get("战法等级", ""),
                "skill_type": skill_type,
                "skill_feature": skill_feature,
                "probability_text": probability_text,
                "probability_pct": probability_pct,
                "description_raw": description,
                "numbers": extract_numbers(description),
                "tags": infer_tags(description, skill_type=skill_type, skill_feature=skill_feature),
                "source_path": str(path),
                "order": order,
                "fields": fields,
            }
        )
        version_lines = []

    for line in lines:
        skill_match = SKILL_HEADING_RE.match(line)
        if skill_match:
            flush()
            current_skill = skill_match.group(1).strip()
            current_version = None
            version_lines = []
            continue
        version_match = VERSION_HEADING_RE.match(line)
        if version_match and current_skill:
            flush()
            current_version = version_match.group(1).strip()
            version_lines = []
            continue
        if current_skill and current_version:
            version_lines.append(line)
    flush()
    return entries


def split_tactic_name_quality(name: str, suffix: str) -> tuple[str, str | None]:
    quality = suffix.strip() or None
    return name.strip(), quality


def parse_tactics_doc(path: Path) -> list[dict[str, Any]]:
    text = normalize_doc_text(path.read_text(encoding="utf-8-sig"))
    entries: list[dict[str, Any]] = []
    group_name = ""
    order = 0
    for line in text.splitlines():
        group_match = TACTIC_GROUP_RE.match(line)
        if group_match:
            group_name = group_match.group(1).strip()
            continue
        row_match = TACTIC_ROW_RE.match(line.strip())
        if not row_match:
            continue
        order += 1
        name, quality = split_tactic_name_quality(row_match.group(1), row_match.group(2))
        description = strip_sentence_punctuation(row_match.group(3))
        entries.append(
            {
                "name": name,
                "quality": quality,
                "group_name": group_name,
                "description_raw": description,
                "numbers": extract_numbers(description),
                "tags": infer_tags(description),
                "source_path": str(path),
                "order": order,
            }
        )
    return entries


def iter_basic_doc_lines(path: Path) -> list[tuple[tuple[str, ...], str]]:
    text = normalize_doc_text(path.read_text(encoding="utf-8-sig"))
    lines: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[int, str]] = []
    in_code = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        heading_match = BASIC_HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        if any(title in BASIC_EXCLUDED_HEADINGS for _, title in stack):
            continue
        lines.append((tuple(title for _, title in stack), line))
    return lines


def iter_markdown_blocks(path: Path) -> list[tuple[tuple[str, ...], str, bool]]:
    text = normalize_doc_text(path.read_text(encoding="utf-8-sig"))
    blocks: list[tuple[tuple[str, ...], str, bool]] = []
    stack: list[tuple[int, str]] = []
    in_code = False
    code_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                blocks.append((tuple(title for _, title in stack), "\n".join(code_lines).strip(), True))
                code_lines = []
                in_code = False
            else:
                in_code = True
                code_lines = []
            continue
        if in_code:
            code_lines.append(line)
            continue
        heading_match = BASIC_HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        blocks.append((tuple(title for _, title in stack), line, False))
    if in_code and code_lines:
        blocks.append((tuple(title for _, title in stack), "\n".join(code_lines).strip(), True))
    return blocks


def is_speculative_basic_line(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return False
    return any(term in compact for term in BASIC_SPECULATIVE_TERMS)


def should_skip_basic_rule(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return True
    if compact.startswith("|"):
        return True
    if "推导规则" in compact:
        return True
    if is_speculative_basic_line(compact):
        return True
    project_terms = (
        "本项目",
        "录入",
        "截图",
        "战报总览页",
        "战报日志",
        "用户于",
        "来源：",
        "录入来源",
    )
    return any(term in compact for term in project_terms)


def basic_section_path(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    return " > ".join(path[1:] if path[0] == "项目基础说明" else path)


def basic_category(path: tuple[str, ...]) -> str:
    if len(path) >= 2 and path[0] == "项目基础说明":
        return path[1]
    return path[0] if path else ""


def split_basic_item(line: str) -> tuple[str | None, str]:
    text = strip_sentence_punctuation(line.strip().lstrip("-").strip())
    if "：" in text:
        name, description = text.split("：", 1)
        return name.strip(), strip_sentence_punctuation(description)
    if ":" in text:
        name, description = text.split(":", 1)
        return name.strip(), strip_sentence_punctuation(description)
    return None, text


def append_unique(items: list[dict[str, Any]], item: dict[str, Any], keys: tuple[str, ...]) -> None:
    marker = tuple(item.get(key) for key in keys)
    for existing in items:
        if tuple(existing.get(key) for key in keys) == marker:
            return
    items.append(item)


def inferred_confidence(text: str) -> tuple[str, float]:
    compact = text.strip()
    if "推导规则" in compact:
        return "medium", 0.6
    if any(term in compact for term in ("已验证", "较稳结论", "符合", "贴近实际", "至少包括")):
        return "medium_high", 0.75
    if any(term in compact for term in ("目前均按", "暂按", "样本较少", "无法完全", "如果无法", "临时可用", "应单独建模")):
        return "low", 0.35
    if any(term in compact for term in ("更像", "不一定", "若能", "否则")):
        return "medium_low", 0.5
    return "medium", 0.6


def inferred_rule_name(text: str, section: tuple[str, ...]) -> str | None:
    stripped = strip_sentence_punctuation(text.strip().lstrip("-").strip())
    if not stripped:
        return None
    if "p 品武将" in stripped and "造成伤害增加" in stripped:
        return "武将升品造成伤害增加实际范围"
    if "：" in stripped:
        name = stripped.split("：", 1)[0].strip()
        return name[:80]
    if ":" in stripped:
        name = stripped.split(":", 1)[0].strip()
        return name[:80]
    if section:
        return section[-1]
    return None


def parse_grade_damage_ranges(text: str) -> list[dict[str, Any]]:
    if "品" not in text or "造成伤害增加" not in text:
        return []
    ranges: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?P<grade>\d+)\s*品(?:为|：|:)\s*"
        r"(?P<min>[+-]?\d+(?:\.\d+)?)%\s*[~～-]\s*"
        r"(?P<max>[+-]?\d+(?:\.\d+)?)%"
    )
    for match in pattern.finditer(text):
        grade = int(match.group("grade"))
        min_pct = float(match.group("min"))
        max_pct = float(match.group("max"))
        ranges.append(
            {
                "grade": grade,
                "mean_pct": float(grade),
                "min_pct": min_pct,
                "max_pct": max_pct,
                "range_width_pct": max_pct - min_pct,
            }
        )
    return ranges


def parse_inferred_rules_doc(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    source_path = str(path)
    for section, block, is_code in iter_markdown_blocks(path):
        text = block.strip()
        if INFERRED_RULE_ROOT not in section and "推导规则" not in text:
            continue
        if not text:
            continue
        if INFERRED_RULE_ROOT in section and section and section[-1] == INFERRED_RULE_ROOT and not text.startswith("-"):
            continue
        if is_code:
            description = text
            name = f"{section[-1]}公式" if section else "公式"
        elif text.startswith("-"):
            description = strip_sentence_punctuation(text.lstrip("-").strip())
            name = inferred_rule_name(text, section)
        else:
            description = strip_sentence_punctuation(text)
            name = inferred_rule_name(text, section)
        if not description:
            continue
        level, score = inferred_confidence(description)
        grade_damage_ranges = parse_grade_damage_ranges(description)
        section_path = basic_section_path(section)
        category = section[-1] if section else INFERRED_RULE_ROOT
        append_unique(
            entries,
            {
                "section_path": section_path,
                "category": category,
                "name": name,
                "rule_status": "inferred",
                "confidence_level": level,
                "confidence_score": score,
                "description_raw": description,
                "evidence_raw": description if any(term in description for term in ("例：", "样本", "显示", "伤害", "倍率")) else None,
                "numbers": extract_numbers(description),
                "tags": infer_tags(description),
                "source_path": source_path,
                "is_code": is_code,
                "structured": {"grade_damage_ranges": grade_damage_ranges} if grade_damage_ranges else {},
            },
            ("section_path", "category", "name", "description_raw"),
        )
    return entries


def parse_basic_rules_doc(path: Path) -> dict[str, list[dict[str, Any]]]:
    rows = iter_basic_doc_lines(path)
    data: dict[str, list[dict[str, Any]]] = {
        "basic_rules": [],
        "properties": [],
        "formations": [],
        "camps": [],
        "camp_bonuses": [],
        "unit_types": [],
        "unit_counters": [],
        "unit_bonuses": [],
        "bonds": [],
        "statuses": [],
        "skill_categories": [],
        "inferred_rules": parse_inferred_rules_doc(path),
    }
    source_path = str(path)
    bond_fields: dict[str, dict[str, str]] = {}
    current_formation_group: str | None = None

    for section, raw_line in rows:
        stripped = raw_line.strip()
        if not stripped:
            continue

        if section and section[-1] == "阵型":
            heading_text = strip_sentence_punctuation(stripped).rstrip("：:").strip()
            if heading_text in {"基础阵型", "新增阵型"}:
                current_formation_group = heading_text
                continue

        if "分为魏、蜀、吴、群" in stripped:
            for name in ("魏", "蜀", "吴", "群"):
                append_unique(
                    data["camps"],
                    {"name": name, "source_path": source_path},
                    ("name",),
                )

        if "盾、弓、枪、骑" in stripped:
            for name in ("盾", "弓", "枪", "骑"):
                append_unique(
                    data["unit_types"],
                    {"name": name, "source_path": source_path},
                    ("name",),
                )

        if "盾克弓" in stripped and "弓克枪" in stripped:
            counter_pairs = (("盾", "弓"), ("弓", "枪"), ("枪", "骑"), ("骑", "盾"))
            for attacker, defender in counter_pairs:
                append_unique(
                    data["unit_counters"],
                    {
                        "attacker_unit": attacker,
                        "defender_unit": defender,
                        "damage_multiplier": 1.15,
                        "description_raw": stripped.lstrip("-").strip(),
                        "source_path": source_path,
                    },
                    ("attacker_unit", "defender_unit"),
                )
                append_unique(
                    data["unit_counters"],
                    {
                        "attacker_unit": defender,
                        "defender_unit": attacker,
                        "damage_multiplier": 0.85,
                        "description_raw": "攻方被目标克制时，本次伤害 ×0.85",
                        "source_path": source_path,
                    },
                    ("attacker_unit", "defender_unit"),
                )

        bullet_match = re.match(r"^-\s*(.+)$", stripped)
        if bullet_match:
            item_text = bullet_match.group(1).strip()
            name, description = split_basic_item(stripped)
            section_path = basic_section_path(section)
            category = basic_category(section)

            if section_path == "游戏战斗基础" and name in {"武力", "智力", "统率", "先攻"}:
                append_unique(
                    data["properties"],
                    {
                        "name": name,
                        "description_raw": description,
                        "source_path": source_path,
                    },
                    ("name",),
                )

            if section and section[-1] == "阵型" and name and name.endswith("阵"):
                append_unique(
                    data["formations"],
                    {
                        "name": name,
                        "formation_group": current_formation_group,
                        "description_raw": description,
                        "numbers": extract_numbers(description),
                        "tags": infer_tags(description),
                        "source_path": source_path,
                    },
                    ("name",),
                )

            camp_bonus_match = re.search(r"(\d+)\s*名同阵营武将.*全属性\s*\+?(\d+(?:\.\d+)?)%", item_text)
            if camp_bonus_match:
                append_unique(
                    data["camp_bonuses"],
                    {
                        "same_camp_count": int(camp_bonus_match.group(1)),
                        "all_attribute_pct": float(camp_bonus_match.group(2)),
                        "description_raw": item_text,
                        "source_path": source_path,
                    },
                    ("same_camp_count",),
                )

            if "兵种武将" in item_text and ("造成伤害" in item_text or "受到" in item_text):
                for segment in re.split(r"[；;]", item_text):
                    unit_bonus_match = re.search(r"(\d+)\s*名([盾弓枪骑])兵种武将[：:](.+)", segment.strip())
                    if not unit_bonus_match:
                        continue
                    desc = strip_sentence_punctuation(unit_bonus_match.group(3))
                    damage_match = re.search(r"造成伤害(?:提升|增加)\s*(\d+(?:\.\d+)?)%", desc)
                    taken_match = re.search(r"受到的?伤害降低\s*(\d+(?:\.\d+)?)%", desc)
                    append_unique(
                        data["unit_bonuses"],
                        {
                            "unit_type": unit_bonus_match.group(2),
                            "same_unit_count": int(unit_bonus_match.group(1)),
                            "damage_pct": float(damage_match.group(1)) if damage_match else None,
                            "damage_taken_pct": -float(taken_match.group(1)) if taken_match else None,
                            "description_raw": desc,
                            "source_path": source_path,
                        },
                        ("unit_type", "same_unit_count"),
                    )

            if len(section) >= 3 and section[-2] == "缘分" and section[-1] != "录入规则":
                fields = bond_fields.setdefault(section[-1], {})
                if name in {"生效条件", "缘分武将", "缘分说明", "录入来源"}:
                    fields[name] = description

            if section and section[-1] in {"常规增益状态", "功能性增益状态", "非控制状态", "控制状态", "常规负面状态"}:
                if name and description and not should_skip_basic_rule(item_text):
                    status_group = "增益状态" if "增益状态" in section else "负面状态"
                    append_unique(
                        data["statuses"],
                        {
                            "name": name,
                            "status_group": status_group,
                            "status_subgroup": section[-1],
                            "description_raw": description,
                            "numbers": extract_numbers(description),
                            "tags": infer_tags(description),
                            "source_path": source_path,
                        },
                        ("name",),
                    )

            if section and section[-1] == "战法类型" and name and description:
                append_unique(
                    data["skill_categories"],
                    {
                        "name": name.replace("战法", ""),
                        "category_type": "战法类型",
                        "description_raw": description,
                        "tags": infer_tags(description, skill_type=name.replace("战法", "")),
                        "source_path": source_path,
                    },
                    ("name", "category_type"),
                )
            if section and section[-1] == "战法特性标签" and name and description:
                append_unique(
                    data["skill_categories"],
                    {
                        "name": name,
                        "category_type": "战法特性",
                        "description_raw": description,
                        "tags": infer_tags(description, skill_feature=name),
                        "source_path": source_path,
                    },
                    ("name", "category_type"),
                )

        if not should_skip_basic_rule(stripped):
            name, description = split_basic_item(stripped)
            if description:
                append_unique(
                    data["basic_rules"],
                    {
                        "section_path": basic_section_path(section),
                        "category": basic_category(section),
                        "name": name,
                        "description_raw": description,
                        "numbers": extract_numbers(description),
                        "tags": infer_tags(description),
                        "source_path": source_path,
                    },
                    ("section_path", "category", "name", "description_raw"),
                )

    for bond_name, fields in bond_fields.items():
        description = fields.get("缘分说明")
        if not description:
            continue
        append_unique(
            data["bonds"],
            {
                "name": bond_name,
                "condition_text": fields.get("生效条件"),
                "heroes_text": fields.get("缘分武将"),
                "description_raw": description,
                "numbers": extract_numbers(description),
                "tags": infer_tags(description),
                "source_path": source_path,
                "source_note": fields.get("录入来源"),
            },
            ("name",),
        )

    return data


def strip_markdown_cell(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("<br>", " / ").replace("<br/>", " / ").replace("<br />", " / ")
    return text.strip()


def split_table_row(line: str) -> list[str]:
    return [strip_markdown_cell(part) for part in line.strip().strip("|").split("|")]


def parse_markdown_tables(markdown_path: Path | None) -> dict[str, dict[str, Any]]:
    if not markdown_path or not markdown_path.exists():
        return {}
    lines = markdown_path.read_text(encoding="utf-8-sig").splitlines()
    by_hero: dict[str, dict[str, Any]] = {}
    idx = 0
    while idx < len(lines) - 1:
        line = lines[idx].strip()
        sep = lines[idx + 1].strip()
        if not line.startswith("|") or not sep.startswith("|") or "---" not in sep:
            idx += 1
            continue
        headers = split_table_row(line)
        rows: list[dict[str, str]] = []
        idx += 2
        while idx < len(lines) and lines[idx].strip().startswith("|"):
            cells = split_table_row(lines[idx])
            if len(cells) < len(headers):
                cells += [""] * (len(headers) - len(cells))
            rows.append(dict(zip(headers, cells)))
            idx += 1

        if "武将" not in headers:
            continue
        useful_cols = {
            "阵营",
            "国家",
            "等级",
            "兵种",
            "初始兵力",
            "品级",
            "红度",
            "金印数",
            "自带战法",
            "自带战法红度",
            "战法顺序与红度",
            "截图可见战法/统计顺序",
            "韬略",
            "备注",
        }
        if not useful_cols.intersection(headers):
            continue
        for row in rows:
            hero = row.get("武将", "").strip()
            if not hero or any(sep in hero for sep in ("、", "/", "，", ",")):
                continue
            data = by_hero.setdefault(hero, {})
            mapping = {
                "阵营": "side",
                "国家": "country",
                "等级": "level",
                "兵种": "unit_type",
                "初始兵力": "initial_troops",
                "品级": "grade",
                "红度": "redness",
                "金印数": "gold_seals",
                "自带战法": "innate_skill",
                "自带战法红度": "innate_skill_redness",
                "战法顺序与红度": "skills_text",
                "战法/统计顺序": "skills_text",
                "截图可见战法/统计顺序": "skills_text",
                "韬略": "tactics_text",
                "备注": "source_note",
            }
            for header, field_name in mapping.items():
                value = row.get(header, "").strip()
                if value and value not in {"待补", "待手动补充", "待截图确认"}:
                    data[field_name] = value
            data.setdefault("markdown_path", str(markdown_path))
    return by_hero


def parse_detail_title(title: str) -> tuple[str, str | None, str | None]:
    match = re.match(r"^(.*?)[（(]\s*(攻方|守方|阵营\s*A|阵营\s*B|阵营A|阵营B)\s*[）)]$", title)
    if match:
        return match.group(1).strip(), match.group(2).replace(" ", ""), None
    match = re.match(r"^(.*?)[（(]\s*([\u4e00-\u9fff]{1,8})\s*[）)]$", title)
    if match:
        return match.group(1).strip(), None, match.group(2).strip()
    return title.strip(), None, None


def parse_bullet_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*([^：:]+)[：:]\s*(.*?)\s*$", line)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()
            if value in {"待补", "待手动补充", "待截图确认"}:
                value = ""
            fields[key] = value
    return fields


def parse_skill_details(markdown_path: Path | None) -> list[dict[str, Any]]:
    if not markdown_path or not markdown_path.exists():
        return []
    text = markdown_path.read_text(encoding="utf-8-sig")
    section_match = re.search(r"(?m)^## 战法详情补充\s*$", text)
    if not section_match:
        return []
    next_section = re.search(r"(?m)^## [^\n#]+?\s*$", text[section_match.end() :])
    section_end = section_match.end() + next_section.start() if next_section else len(text)
    body = text[section_match.end() : section_end]
    headings = list(re.finditer(r"(?m)^### (.+?)\s*$", body))
    details: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        title = heading.group(1).strip()
        if title in {"待补战法名", "待补"}:
            continue
        block = body[heading.start() : end].strip()
        fields = parse_bullet_fields(body[heading.end() : end])
        skill_name, side, hero = parse_detail_title(title)
        description = fields.get("说明") or fields.get("描述") or fields.get("效果") or ""
        payload = {
            "title": title,
            "fields": fields,
            "markdown_path": str(markdown_path),
        }
        details.append(
            {
                "skill_name": skill_name,
                "side": side,
                "hero": hero,
                "redness": fields.get("战法红度"),
                # Deprecated: Sanmou tactics have red-level variants, not a
                # separate tactic grade. Keep the DB column empty for backward
                # schema compatibility.
                "grade": None,
                "skill_type": fields.get("战法类型") or fields.get("类型"),
                "unit_types": fields.get("适用兵种"),
                "description": description,
                "source_text": block,
                "payload_json": payload,
            }
        )
    return details


def npc_rule_from_row(row: dict[str, Any]) -> str:
    if is_npc_initial_troops(row.get("initial_troops")):
        return "initial_troops=16000"
    return f"max_observed_troops>{PLAYER_MAX_TROOPS}"


def row_has_skill(row: dict[str, Any], skill_name: str | None) -> bool:
    if not skill_name:
        return False
    for skill in re.split(r"[/／,，]", str(row.get("skills_text") or "")):
        name = re.sub(r"[（(]\s*\d+红\s*[）)]", "", skill).strip()
        if name == skill_name:
            return True
    return row.get("innate_skill") == skill_name


def apply_npc_skill_detail_defaults(
    skill_details: list[dict[str, Any]],
    participants: list[dict[str, Any]],
) -> None:
    npc_skills: set[str] = set()
    for row in participants:
        if not is_npc_row(row):
            continue
        for skill in re.split(r"[/／,，]", str(row.get("skills_text") or "")):
            name = re.sub(r"[（(]\s*\d+红\s*[）)]", "", skill).strip()
            if name and name != "普攻":
                npc_skills.add(name)
        if row.get("innate_skill"):
            npc_skills.add(str(row["innate_skill"]))
    for detail in skill_details:
        if detail.get("skill_name") not in npc_skills:
            continue
        detail_redness = str(detail.get("redness") or "").strip()
        if detail_redness and detail_redness != NPC_REDNESS:
            continue
        detail["redness"] = NPC_REDNESS
        payload = detail.get("payload_json")
        if isinstance(payload, dict):
            matching_row = next(
                (row for row in participants if is_npc_row(row) and row_has_skill(row, detail.get("skill_name"))),
                None,
            )
            payload["npc_rule"] = npc_rule_from_row(matching_row) if matching_row else "npc"


def chinese_to_int(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if value in digits:
        return digits[value]
    return None


def round_no_from_section(section: str) -> int | None:
    if section == "列队布阵":
        return 0
    m = re.search(r"第?([零一二三四五六七八九十百0-9]+)回合", section)
    return chinese_to_int(m.group(1)) if m else None


def discover_markdown(capture_path: Path, reports_dir: Path) -> Path | None:
    candidate = reports_dir / f"{capture_path.stem}.md"
    return candidate if candidate.exists() else None


def build_participants(parsed_report: Any, markdown_path: Path | None) -> list[dict[str, Any]]:
    md_data = parse_markdown_tables(markdown_path)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for side, team_id, hero in collect_report_heroes(parsed_report):
        data = dict(md_data.get(hero, {}))
        md_initial_troops = parse_int(data.get("initial_troops"))
        inferred_initial_troops = initial_troops_for(parsed_report, team_id, hero)
        max_observed_troops = max_observed_troops_for(parsed_report, team_id, hero)
        inferred_tactics_text = tactics_text_for(parsed_report, team_id, hero)
        payload = dict(data)
        if max_observed_troops is not None:
            payload["max_observed_troops"] = max_observed_troops
        if inferred_tactics_text:
            payload["inferred_tactics_text"] = inferred_tactics_text
        row = {
            "side": data.get("side", side),
            "team_id": team_id,
            "hero": hero,
            "country": data.get("country"),
            "level": parse_int(data.get("level")) or DEFAULT_HERO_LEVEL,
            "grade": data.get("grade"),
            "unit_type": data.get("unit_type") or unit_type_for(parsed_report, team_id, hero),
            "initial_troops": md_initial_troops if md_initial_troops is not None else inferred_initial_troops,
            "redness": data.get("redness"),
            "gold_seals": data.get("gold_seals"),
            "innate_skill": data.get("innate_skill"),
            "innate_skill_redness": data.get("innate_skill_redness"),
            "skills_text": data.get("skills_text"),
            "tactics_text": data.get("tactics_text") or inferred_tactics_text,
            "source_note": data.get("source_note"),
            "max_observed_troops": max_observed_troops,
            "payload_json": payload,
        }
        apply_npc_defaults(row)
        normalize_participant_progression(row)
        key = (row["side"] or "", row["team_id"] or "", row["hero"])
        seen.add(key)
        rows.append(row)

    # Markdown-only rows are useful when the parser cannot extract team links.
    for hero, data in md_data.items():
        key = (data.get("side", ""), "", hero)
        if any(existing[2] == hero for existing in seen):
            continue
        md_initial_troops = parse_int(data.get("initial_troops"))
        inferred_initial_troops = getattr(parsed_report, "initial_troops", {}).get(hero)
        max_observed_troops = getattr(parsed_report, "max_observed_troops", {}).get(hero)
        inferred_tactics_text = "、".join(getattr(parsed_report, "tactics", {}).get(hero, []))
        payload = dict(data)
        if max_observed_troops is not None:
            payload["max_observed_troops"] = max_observed_troops
        if inferred_tactics_text:
            payload["inferred_tactics_text"] = inferred_tactics_text
        row = {
                "side": data.get("side"),
                "team_id": None,
                "hero": hero,
                "country": data.get("country"),
                "level": parse_int(data.get("level")) or DEFAULT_HERO_LEVEL,
                "grade": data.get("grade"),
                "unit_type": data.get("unit_type"),
                "initial_troops": md_initial_troops if md_initial_troops is not None else inferred_initial_troops,
                "redness": data.get("redness"),
                "gold_seals": data.get("gold_seals"),
                "innate_skill": data.get("innate_skill"),
                "innate_skill_redness": data.get("innate_skill_redness"),
                "skills_text": data.get("skills_text"),
                "tactics_text": data.get("tactics_text") or inferred_tactics_text,
                "source_note": data.get("source_note"),
                "max_observed_troops": max_observed_troops,
                "payload_json": payload,
            }
        apply_npc_defaults(row)
        normalize_participant_progression(row)
        rows.append(row)
    return rows


def insert_participants(conn: sqlite3.Connection, report_id: int, participants: list[dict[str, Any]]) -> None:
    for row in participants:
        conn.execute(
            """
            INSERT INTO participants (
                report_id, side, team_id, hero, country, level, grade, unit_type,
                initial_troops, redness, gold_seals, innate_skill,
                innate_skill_redness, skills_text, tactics_text, source_note,
                payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                row.get("side"),
                row.get("team_id"),
                row["hero"],
                row.get("country"),
                row.get("level"),
                row.get("grade"),
                row.get("unit_type"),
                row.get("initial_troops"),
                row.get("redness"),
                row.get("gold_seals"),
                row.get("innate_skill"),
                row.get("innate_skill_redness"),
                row.get("skills_text"),
                row.get("tactics_text"),
                row.get("source_note"),
                json_dumps(row.get("payload_json", {})),
            ),
        )


def insert_report_skill_details(
    conn: sqlite3.Connection,
    report_id: int,
    skill_details: list[dict[str, Any]],
) -> None:
    for detail in skill_details:
        conn.execute(
            """
            INSERT INTO report_skill_details (
                report_id, skill_name, side, hero, redness, grade, skill_type,
                unit_types, description, source_text, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                detail["skill_name"],
                detail.get("side"),
                detail.get("hero"),
                detail.get("redness"),
                detail.get("grade"),
                detail.get("skill_type"),
                detail.get("unit_types"),
                detail.get("description"),
                detail.get("source_text"),
                json_dumps(detail.get("payload_json", {})),
            ),
        )


@dataclass
class ReplayState:
    participants: dict[str, dict[str, Any]]
    props: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    buffs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    hp: dict[str, int] = field(default_factory=dict)
    current_turn: str | None = None
    recent_action: dict[str, Any] = field(default_factory=dict)
    recent_effect: dict[str, Any] = field(default_factory=dict)
    recent_trigger_by_hero: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for hero, row in self.participants.items():
            if row.get("initial_troops") is not None:
                self.hp[hero] = row["initial_troops"]

    def prop_value(self, hero: str | None, prop: str) -> float | None:
        if not hero:
            return None
        item = self.props.get(hero, {}).get(prop)
        if not item:
            return None
        return item.get("value")

    def active_buffs(self, hero: str | None) -> dict[str, Any]:
        if not hero:
            return {}
        return {
            buff: data
            for buff, data in self.buffs.get(hero, {}).items()
            if data.get("active", True)
        }

    def hero_context(self, hero: str | None) -> dict[str, Any]:
        if not hero:
            return {}
        return {
            "hero": hero,
            "participant": self.participants.get(hero, {}),
            "hp": self.hp.get(hero),
            "props": self.props.get(hero, {}),
            "active_buffs": self.active_buffs(hero),
            "recent_trigger": self.recent_trigger_by_hero.get(hero),
            "current_turn": self.current_turn,
        }

    def infer_damage_identity(self, ev: dict[str, Any]) -> dict[str, Any]:
        if ev["type"] == "damage":
            source = ev.get("source")
            return {
                "source": source,
                "target": ev.get("target"),
                "skill": ev.get("skill") or "",
                "buff": ev.get("buff") or "",
                "action": dict(self.recent_action),
                "trigger": self.recent_trigger_by_hero.get(source, {}),
            }

        target = ev.get("target")
        action = dict(self.recent_action)
        source = action.get("source") or self.current_turn
        skill = action.get("skill") or ("普通攻击" if action.get("type") == "normal_attack" else "")
        buff = action.get("buff") or ""
        return {
            "source": source,
            "target": target,
            "skill": skill,
            "buff": buff,
            "action": action,
            "trigger": self.recent_trigger_by_hero.get(source, {}),
        }

    def apply_event(self, ev: dict[str, Any], event_order: int, raw_index: int | None) -> None:
        t = ev["type"]
        if t == "turn_start":
            self.current_turn = ev.get("hero")
            self.recent_action = {
                "type": "turn_start",
                "source": ev.get("hero"),
                "event_order": event_order,
                "raw_index": raw_index,
            }
            return

        if t == "normal_attack":
            self.recent_action = {
                "type": "normal_attack",
                "source": ev.get("attacker"),
                "target": ev.get("target"),
                "skill": "普通攻击",
                "event_order": event_order,
                "raw_index": raw_index,
                "raw": ev.get("raw", ""),
            }
            return

        if t == "counter":
            self.recent_action = {
                "type": "counter",
                "source": ev.get("hero"),
                "skill": "反击",
                "event_order": event_order,
                "raw_index": raw_index,
                "raw": ev.get("raw", ""),
            }
            return

        if t == "skill_cast":
            self.recent_action = {
                "type": "skill_cast",
                "source": ev.get("hero"),
                "skill": ev.get("skill") or "",
                "event_order": event_order,
                "raw_index": raw_index,
                "raw": ev.get("raw", ""),
            }
            return

        if t == "buff_exec":
            self.recent_effect = {
                "hero": ev.get("hero"),
                "skill": ev.get("skill") or "",
                "buff": ev.get("buff") or "",
                "event_order": event_order,
                "raw_index": raw_index,
                "raw": ev.get("raw", ""),
            }
            if ev.get("skill") or ev.get("buff"):
                self.recent_action = {
                    "type": "buff_exec",
                    "source": ev.get("hero"),
                    "skill": ev.get("skill") or "",
                    "buff": ev.get("buff") or "",
                    "event_order": event_order,
                    "raw_index": raw_index,
                    "raw": ev.get("raw", ""),
                }
            return

        if t == "property":
            hero = ev.get("hero")
            prop = ev.get("prop") or ""
            if hero and prop:
                result = ev.get("result") or ""
                self.props.setdefault(hero, {})[prop] = {
                    "text": result or ev.get("value") or "",
                    "value": parse_number(result or ev.get("value")),
                    "delta_text": ev.get("value") or "",
                    "delta": parse_number(ev.get("value")),
                    "direction": ev.get("direction") or "",
                    "event_order": event_order,
                    "raw_index": raw_index,
                    "raw": ev.get("raw", ""),
                }
            return

        if t in {"buff_apply", "buff_refresh", "buff_stack"}:
            hero = ev.get("hero")
            buff = ev.get("buff") or ""
            if hero and buff:
                current = self.buffs.setdefault(hero, {}).setdefault(buff, {})
                current.update(
                    {
                        "active": True,
                        "count": ev.get("count", current.get("count")),
                        "full": ev.get("full", current.get("full", False)),
                        "event_order": event_order,
                        "raw_index": raw_index,
                        "last_change": t,
                        "raw": ev.get("raw", ""),
                    }
                )
            return

        if t == "buff_expire":
            hero = ev.get("hero")
            buff = ev.get("buff") or ""
            if hero and buff:
                current = self.buffs.setdefault(hero, {}).setdefault(buff, {})
                current.update(
                    {
                        "active": False,
                        "event_order": event_order,
                        "raw_index": raw_index,
                        "last_change": t,
                        "raw": ev.get("raw", ""),
                    }
                )
            return

        if t == "trigger":
            hero = ev.get("hero")
            if hero:
                self.recent_trigger_by_hero[hero] = {
                    "trigger": ev.get("trigger"),
                    "damage_ratio": ev.get("damage_ratio"),
                    "event_order": event_order,
                    "raw_index": raw_index,
                    "raw": ev.get("raw", ""),
                }
            return

        if t in {"damage", "damage_raw"}:
            target = ev.get("target")
            if target:
                if ev.get("remain") is not None:
                    self.hp[target] = ev["remain"]
                elif target in self.hp:
                    self.hp[target] = max(0, self.hp[target] - int(ev.get("damage") or 0))
            return

        if t == "heal":
            hero = ev.get("hero")
            if hero:
                if ev.get("remain") is not None:
                    self.hp[hero] = ev["remain"]
                elif hero in self.hp:
                    self.hp[hero] += int(ev.get("heal") or 0)
            return

        if t == "death":
            hero = ev.get("hero")
            if hero:
                self.hp[hero] = 0


def event_identity(ev: dict[str, Any], replay: ReplayState) -> dict[str, Any]:
    if ev["type"] in {"damage", "damage_raw"}:
        identity = replay.infer_damage_identity(ev)
        return {
            "hero": ev.get("hero"),
            "source": identity.get("source"),
            "target": identity.get("target"),
            "skill": identity.get("skill"),
            "buff": identity.get("buff"),
            "damage": ev.get("damage"),
            "heal": None,
            "remain": ev.get("remain"),
        }
    return {
        "hero": ev.get("hero") or ev.get("attacker") or ev.get("team"),
        "source": ev.get("source") or ev.get("attacker"),
        "target": ev.get("target"),
        "skill": ev.get("skill"),
        "buff": ev.get("buff"),
        "damage": ev.get("damage"),
        "heal": ev.get("heal"),
        "remain": ev.get("remain"),
    }


def insert_event(
    conn: sqlite3.Connection,
    report_id: int,
    event_order: int,
    section: str,
    round_no: int | None,
    ev: dict[str, Any],
    identity: dict[str, Any],
) -> int:
    payload = {k: v for k, v in ev.items() if k != "raw"}
    cursor = conn.execute(
        """
        INSERT INTO events (
            report_id, event_order, raw_index, section, round_no, event_type,
            hero, source, target, skill, buff, damage, heal, remain,
            raw_text, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            event_order,
            ev.get("_raw_index"),
            section,
            round_no,
            ev["type"],
            identity.get("hero"),
            identity.get("source"),
            identity.get("target"),
            identity.get("skill"),
            identity.get("buff"),
            identity.get("damage"),
            identity.get("heal"),
            identity.get("remain"),
            ev.get("raw", ""),
            json_dumps(payload),
        ),
    )
    event_id = int(cursor.lastrowid)
    try:
        conn.execute(
            "INSERT INTO event_search(report_id, event_id, raw_text) VALUES (?, ?, ?)",
            (report_id, event_id, ev.get("raw", "")),
        )
    except sqlite3.OperationalError:
        pass
    return event_id


def insert_state_change(
    conn: sqlite3.Connection,
    report_id: int,
    event_id: int,
    event_order: int,
    round_no: int | None,
    ev: dict[str, Any],
    identity: dict[str, Any],
    replay: ReplayState,
) -> None:
    if ev["type"] not in STATE_EVENT_TYPES:
        return

    hero = ev.get("hero") or identity.get("target") or identity.get("source")
    prop = ev.get("prop")
    value_text = ev.get("value")
    result_text = ev.get("result")
    active_after = None
    if ev["type"] in {"buff_apply", "buff_refresh", "buff_stack"}:
        active_after = 1
    elif ev["type"] == "buff_expire":
        active_after = 0
    elif ev["type"] in {"damage", "damage_raw"}:
        prop = "兵力"
        value_text = str(-int(ev.get("damage") or 0))
        result_text = str(ev.get("remain")) if ev.get("remain") is not None else None
    elif ev["type"] == "heal":
        prop = "兵力"
        value_text = str(int(ev.get("heal") or 0))
        result_text = str(ev.get("remain")) if ev.get("remain") is not None else None
    elif ev["type"] == "death":
        prop = "兵力"
        result_text = "0"

    conn.execute(
        """
        INSERT INTO state_changes (
            report_id, event_id, event_order, raw_index, round_no, hero,
            change_type, prop, direction, value_text, value_num,
            result_text, result_num, skill, buff, active_after, raw_text,
            payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            event_id,
            event_order,
            ev.get("_raw_index"),
            round_no,
            hero,
            ev["type"],
            prop,
            ev.get("direction"),
            value_text,
            parse_number(value_text),
            result_text,
            parse_number(result_text),
            identity.get("skill") or ev.get("skill"),
            identity.get("buff") or ev.get("buff"),
            active_after,
            ev.get("raw", ""),
            json_dumps({k: v for k, v in ev.items() if k != "raw"}),
        ),
    )


def common_prop_values(replay: ReplayState, hero: str | None, prefix: str) -> dict[str, float | None]:
    return {
        f"{prefix}_{column}": replay.prop_value(hero, prop)
        for column, prop in COMMON_PROP_COLUMNS.items()
    }


def insert_damage_context(
    conn: sqlite3.Connection,
    report_id: int,
    event_id: int,
    event_order: int,
    section: str,
    round_no: int | None,
    ev: dict[str, Any],
    identity: dict[str, Any],
    replay: ReplayState,
) -> None:
    source = identity.get("source")
    target = identity.get("target")
    damage = int(ev.get("damage") or 0)
    target_hp_before = replay.hp.get(target) if target else None
    target_hp_after = ev.get("remain")
    if target_hp_after is None and target_hp_before is not None:
        target_hp_after = max(0, target_hp_before - damage)

    action = identity.get("action") or {}
    trigger = identity.get("trigger") or {}
    source_context = replay.hero_context(source)
    target_context = replay.hero_context(target)
    source_buffs = replay.active_buffs(source)
    target_buffs = replay.active_buffs(target)
    prop_values = {
        **common_prop_values(replay, source, "source"),
        **common_prop_values(replay, target, "target"),
    }

    conn.execute(
        """
        INSERT INTO damage_contexts (
            report_id, event_id, event_order, raw_index, section, round_no,
            damage_event_type, source, target, skill, buff, damage, remain,
            target_hp_before, target_hp_after, action_type, action_skill,
            action_buff, action_target, recent_trigger,
            source_force, source_intelligence, source_command,
            source_initiative, source_damage_pct, source_damage_taken_pct,
            source_crit_chance_pct, source_crit_damage_pct, source_pierce_pct,
            source_combo_pct, source_lifesteal_pct, source_counter_pct,
            source_avoid_pct, target_force, target_intelligence,
            target_command, target_initiative, target_damage_pct,
            target_damage_taken_pct, target_crit_chance_pct,
            target_crit_damage_pct, target_pierce_pct, target_combo_pct,
            target_lifesteal_pct, target_counter_pct, target_avoid_pct,
            source_context_json, target_context_json,
            source_active_buffs_json, target_active_buffs_json,
            action_context_json, raw_text, payload_json
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            report_id,
            event_id,
            event_order,
            ev.get("_raw_index"),
            section,
            round_no,
            ev["type"],
            source,
            target,
            identity.get("skill") or "",
            identity.get("buff") or "",
            damage,
            ev.get("remain"),
            target_hp_before,
            target_hp_after,
            action.get("type"),
            action.get("skill"),
            action.get("buff"),
            action.get("target"),
            trigger.get("trigger"),
            prop_values["source_force"],
            prop_values["source_intelligence"],
            prop_values["source_command"],
            prop_values["source_initiative"],
            prop_values["source_damage_pct"],
            prop_values["source_damage_taken_pct"],
            prop_values["source_crit_chance_pct"],
            prop_values["source_crit_damage_pct"],
            prop_values["source_pierce_pct"],
            prop_values["source_combo_pct"],
            prop_values["source_lifesteal_pct"],
            prop_values["source_counter_pct"],
            prop_values["source_avoid_pct"],
            prop_values["target_force"],
            prop_values["target_intelligence"],
            prop_values["target_command"],
            prop_values["target_initiative"],
            prop_values["target_damage_pct"],
            prop_values["target_damage_taken_pct"],
            prop_values["target_crit_chance_pct"],
            prop_values["target_crit_damage_pct"],
            prop_values["target_pierce_pct"],
            prop_values["target_combo_pct"],
            prop_values["target_lifesteal_pct"],
            prop_values["target_counter_pct"],
            prop_values["target_avoid_pct"],
            json_dumps(source_context),
            json_dumps(target_context),
            json_dumps(source_buffs),
            json_dumps(target_buffs),
            json_dumps(action),
            ev.get("raw", ""),
            json_dumps({k: v for k, v in ev.items() if k != "raw"}),
        ),
    )


def load_participants_by_hero(participants: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_hero: dict[str, dict[str, Any]] = {}
    for row in participants:
        by_hero.setdefault(row["hero"], row)
    return by_hero


def delete_existing_report(conn: sqlite3.Connection, source_hash: str, capture_path: Path) -> None:
    rows = conn.execute(
        "SELECT id FROM reports WHERE source_hash = ? OR capture_path = ?",
        (source_hash, str(capture_path)),
    ).fetchall()
    for row in rows:
        try:
            conn.execute("DELETE FROM event_search WHERE report_id = ?", (row["id"],))
        except sqlite3.OperationalError:
            pass
        conn.execute("DELETE FROM reports WHERE id = ?", (row["id"],))


def import_capture(
    conn: sqlite3.Connection,
    capture_path: Path,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    dedup: str = "local",
    parsed_report: Any | None = None,
) -> int:
    capture_path = capture_path.resolve()
    markdown_path = discover_markdown(capture_path, reports_dir)
    source_hash = sha256_file(capture_path)
    if parsed_report is None:
        parsed_report = parse_capture(capture_path, dedup=dedup)
    participants = build_participants(parsed_report, markdown_path)
    skill_details = parse_skill_details(markdown_path)
    apply_npc_skill_detail_defaults(skill_details, participants)

    with conn:
        setup_schema(conn)
        delete_existing_report(conn, source_hash, capture_path)
        cursor = conn.execute(
            """
            INSERT INTO reports (
                report_key, source_hash, capture_path, markdown_path,
                raw_count, parsed_count, event_count, dedup_mode, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture_path.stem,
                source_hash,
                str(capture_path),
                str(markdown_path.resolve()) if markdown_path else None,
                parsed_report.raw_count,
                parsed_report.parsed_count,
                parsed_report.event_count,
                parsed_report.dedup_mode,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        report_id = int(cursor.lastrowid)
        insert_participants(conn, report_id, participants)
        insert_report_skill_details(conn, report_id, skill_details)
        replay = ReplayState(load_participants_by_hero(participants))

        event_order = 0
        for section, events in parsed_report.sections:
            round_no = round_no_from_section(section)
            for ev in events:
                event_order += 1
                identity = event_identity(ev, replay)
                event_id = insert_event(conn, report_id, event_order, section, round_no, ev, identity)
                if ev["type"] in {"damage", "damage_raw"}:
                    insert_damage_context(
                        conn,
                        report_id,
                        event_id,
                        event_order,
                        section,
                        round_no,
                        ev,
                        replay.infer_damage_identity(ev),
                        replay,
                    )
                insert_state_change(conn, report_id, event_id, event_order, round_no, ev, identity, replay)
                replay.apply_event(ev, event_order, ev.get("_raw_index"))
    return report_id


def import_all(conn: sqlite3.Connection, captured_dir: Path, reports_dir: Path, dedup: str) -> list[tuple[Path, int]]:
    results: list[tuple[Path, int]] = []
    for path in sorted(captured_dir.glob("battle_*.txt")):
        report_id = import_capture(conn, path, reports_dir=reports_dir, dedup=dedup)
        results.append((path, report_id))
    return results


def clear_knowledge(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM game_tactics")
    conn.execute("DELETE FROM game_skills")
    conn.execute("DELETE FROM game_inferred_rules")
    conn.execute("DELETE FROM game_skill_categories")
    conn.execute("DELETE FROM game_statuses")
    conn.execute("DELETE FROM game_bonds")
    conn.execute("DELETE FROM game_unit_bonuses")
    conn.execute("DELETE FROM game_unit_counters")
    conn.execute("DELETE FROM game_unit_types")
    conn.execute("DELETE FROM game_camp_bonuses")
    conn.execute("DELETE FROM game_camps")
    conn.execute("DELETE FROM game_formations")
    conn.execute("DELETE FROM game_properties")
    conn.execute("DELETE FROM game_basic_rules")


def import_skill_entries(conn: sqlite3.Connection, entries: list[dict[str, Any]]) -> int:
    skill_ids: dict[str, int] = {}
    for entry in entries:
        name = entry["name"]
        if name not in skill_ids:
            cursor = conn.execute(
                """
                INSERT INTO game_skills (name, source_path, first_seen_order, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    source_path = excluded.source_path,
                    first_seen_order = COALESCE(game_skills.first_seen_order, excluded.first_seen_order),
                    payload_json = excluded.payload_json
                """,
                (
                    name,
                    entry["source_path"],
                    entry["order"],
                    json_dumps({"source": "skills_database.md"}),
                ),
            )
            if cursor.lastrowid:
                skill_ids[name] = int(cursor.lastrowid)
            else:
                row = conn.execute("SELECT id FROM game_skills WHERE name = ?", (name,)).fetchone()
                skill_ids[name] = int(row["id"])
        skill_id = skill_ids[name]
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
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
                entry.get("skill_level"),
                entry.get("skill_type"),
                entry.get("skill_feature"),
                entry.get("probability_text"),
                entry.get("probability_pct"),
                entry.get("description_raw") or "",
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                entry["source_path"],
                json_dumps(payload),
            ),
        )
    return len(entries)


def import_tactic_entries(conn: sqlite3.Connection, entries: list[dict[str, Any]]) -> int:
    for entry in entries:
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
        conn.execute(
            """
            INSERT INTO game_tactics (
                name, quality, group_name, description_raw, numbers_json,
                tags_json, source_path, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, quality, group_name, description_raw) DO UPDATE SET
                numbers_json = excluded.numbers_json,
                tags_json = excluded.tags_json,
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (
                entry["name"],
                entry.get("quality"),
                entry.get("group_name") or "",
                entry["description_raw"],
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                entry["source_path"],
                json_dumps(payload),
            ),
        )
    return len(entries)


def import_basic_rules(conn: sqlite3.Connection, data: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    for entry in data.get("basic_rules", []):
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
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
                entry["source_path"],
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                json_dumps(payload),
            ),
        )

    for entry in data.get("properties", []):
        conn.execute(
            """
            INSERT INTO game_properties (name, description_raw, source_path, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (
                entry["name"],
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry),
            ),
        )

    for entry in data.get("formations", []):
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
        conn.execute(
            """
            INSERT INTO game_formations (
                name, formation_group, description_raw, source_path,
                numbers_json, tags_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                formation_group = excluded.formation_group,
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                numbers_json = excluded.numbers_json,
                tags_json = excluded.tags_json,
                payload_json = excluded.payload_json
            """,
            (
                entry["name"],
                entry.get("formation_group"),
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                json_dumps(payload),
            ),
        )

    for entry in data.get("camps", []):
        conn.execute(
            """
            INSERT INTO game_camps (name, source_path, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (entry["name"], entry["source_path"], json_dumps(entry)),
        )

    for entry in data.get("camp_bonuses", []):
        conn.execute(
            """
            INSERT INTO game_camp_bonuses (
                same_camp_count, all_attribute_pct, description_raw, source_path, payload_json
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(same_camp_count) DO UPDATE SET
                all_attribute_pct = excluded.all_attribute_pct,
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (
                entry["same_camp_count"],
                entry["all_attribute_pct"],
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry),
            ),
        )

    for entry in data.get("unit_types", []):
        conn.execute(
            """
            INSERT INTO game_unit_types (name, source_path, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (entry["name"], entry["source_path"], json_dumps(entry)),
        )

    for entry in data.get("unit_counters", []):
        conn.execute(
            """
            INSERT INTO game_unit_counters (
                attacker_unit, defender_unit, damage_multiplier,
                description_raw, source_path, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(attacker_unit, defender_unit) DO UPDATE SET
                damage_multiplier = excluded.damage_multiplier,
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (
                entry["attacker_unit"],
                entry["defender_unit"],
                entry["damage_multiplier"],
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry),
            ),
        )

    for entry in data.get("unit_bonuses", []):
        conn.execute(
            """
            INSERT INTO game_unit_bonuses (
                unit_type, same_unit_count, damage_pct, damage_taken_pct,
                description_raw, source_path, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_type, same_unit_count) DO UPDATE SET
                damage_pct = excluded.damage_pct,
                damage_taken_pct = excluded.damage_taken_pct,
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                payload_json = excluded.payload_json
            """,
            (
                entry["unit_type"],
                entry["same_unit_count"],
                entry.get("damage_pct"),
                entry.get("damage_taken_pct"),
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry),
            ),
        )

    for entry in data.get("bonds", []):
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
        conn.execute(
            """
            INSERT INTO game_bonds (
                name, condition_text, heroes_text, description_raw, source_path,
                numbers_json, tags_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                condition_text = excluded.condition_text,
                heroes_text = excluded.heroes_text,
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                numbers_json = excluded.numbers_json,
                tags_json = excluded.tags_json,
                payload_json = excluded.payload_json
            """,
            (
                entry["name"],
                entry.get("condition_text"),
                entry.get("heroes_text"),
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                json_dumps(payload),
            ),
        )

    for entry in data.get("statuses", []):
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
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
                entry.get("status_subgroup"),
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                json_dumps(payload),
            ),
        )

    for entry in data.get("skill_categories", []):
        payload = {k: v for k, v in entry.items() if k != "tags"}
        conn.execute(
            """
            INSERT INTO game_skill_categories (
                name, category_type, description_raw, source_path, tags_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, category_type) DO UPDATE SET
                description_raw = excluded.description_raw,
                source_path = excluded.source_path,
                tags_json = excluded.tags_json,
                payload_json = excluded.payload_json
            """,
            (
                entry["name"],
                entry["category_type"],
                entry["description_raw"],
                entry["source_path"],
                json_dumps(entry.get("tags", [])),
                json_dumps(payload),
            ),
        )

    for entry in data.get("inferred_rules", []):
        payload = {k: v for k, v in entry.items() if k not in {"numbers", "tags"}}
        conn.execute(
            """
            INSERT INTO game_inferred_rules (
                section_path, category, name, rule_status, confidence_level,
                confidence_score, description_raw, evidence_raw, source_path,
                numbers_json, tags_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(section_path, category, name, description_raw) DO UPDATE SET
                rule_status = excluded.rule_status,
                confidence_level = excluded.confidence_level,
                confidence_score = excluded.confidence_score,
                evidence_raw = excluded.evidence_raw,
                source_path = excluded.source_path,
                numbers_json = excluded.numbers_json,
                tags_json = excluded.tags_json,
                payload_json = excluded.payload_json
            """,
            (
                entry["section_path"],
                entry["category"],
                entry.get("name"),
                entry.get("rule_status", "inferred"),
                entry["confidence_level"],
                entry.get("confidence_score"),
                entry["description_raw"],
                entry.get("evidence_raw"),
                entry["source_path"],
                json_dumps(entry.get("numbers", [])),
                json_dumps(entry.get("tags", [])),
                json_dumps(payload),
            ),
        )

    return {name: len(items) for name, items in data.items()}


def import_knowledge(
    conn: sqlite3.Connection,
    skills_path: Path = DEFAULT_SKILLS_DOC,
    tactics_path: Path = DEFAULT_TACTICS_DOC,
    basics_path: Path = DEFAULT_BASICS_DOC,
    clear: bool = True,
    include_basics: bool = True,
) -> dict[str, int]:
    with conn:
        setup_schema(conn)
        if clear:
            clear_knowledge(conn)
        skills = parse_skill_doc(skills_path) if skills_path.exists() else []
        tactics = parse_tactics_doc(tactics_path) if tactics_path.exists() else []
        basics = parse_basic_rules_doc(basics_path) if include_basics and basics_path.exists() else {}
        skill_versions = import_skill_entries(conn, skills)
        tactic_count = import_tactic_entries(conn, tactics)
        basic_counts = import_basic_rules(conn, basics) if basics else {}
    return {
        "skills": conn.execute("SELECT COUNT(*) FROM game_skills").fetchone()[0],
        "skill_versions": skill_versions,
        "tactics": tactic_count,
        "basic_rules": conn.execute("SELECT COUNT(*) FROM game_basic_rules").fetchone()[0],
        "properties": conn.execute("SELECT COUNT(*) FROM game_properties").fetchone()[0],
        "formations": conn.execute("SELECT COUNT(*) FROM game_formations").fetchone()[0],
        "camps": conn.execute("SELECT COUNT(*) FROM game_camps").fetchone()[0],
        "camp_bonuses": conn.execute("SELECT COUNT(*) FROM game_camp_bonuses").fetchone()[0],
        "unit_types": conn.execute("SELECT COUNT(*) FROM game_unit_types").fetchone()[0],
        "unit_counters": conn.execute("SELECT COUNT(*) FROM game_unit_counters").fetchone()[0],
        "unit_bonuses": conn.execute("SELECT COUNT(*) FROM game_unit_bonuses").fetchone()[0],
        "bonds": conn.execute("SELECT COUNT(*) FROM game_bonds").fetchone()[0],
        "statuses": conn.execute("SELECT COUNT(*) FROM game_statuses").fetchone()[0],
        "skill_categories": conn.execute("SELECT COUNT(*) FROM game_skill_categories").fetchone()[0],
        "inferred_rules": conn.execute("SELECT COUNT(*) FROM game_inferred_rules").fetchone()[0],
        "basic_parse_counts": basic_counts,
    }


def print_damage_examples(conn: sqlite3.Connection, limit: int) -> None:
    rows = conn.execute(
        """
        SELECT
            r.report_key,
            d.raw_index,
            d.round_no,
            d.source,
            d.target,
            d.skill,
            d.buff,
            d.damage,
            d.source_force,
            d.target_command,
            d.source_damage_pct,
            d.target_damage_taken_pct,
            d.raw_text
        FROM damage_contexts d
        JOIN reports r ON r.id = d.report_id
        ORDER BY r.report_key, d.event_order
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        print(
            f"{row['report_key']} #{row['raw_index']:05d} R{row['round_no']} "
            f"{row['source']} -> {row['target']} {row['skill']} {row['buff']} "
            f"damage={row['damage']} force={row['source_force']} "
            f"target_command={row['target_command']} src_dmg={row['source_damage_pct']} "
            f"tgt_taken={row['target_damage_taken_pct']}"
        )
        print(f"  {row['raw_text']}")


def validate_database(
    conn: sqlite3.Connection,
    captured_dir: Path = DEFAULT_CAPTURED_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    deep: bool = False,
    dedup: str = "local",
) -> int:
    captured_files = sorted(captured_dir.glob("battle_*.txt"))
    markdown_files = sorted(reports_dir.glob("battle_*.md"))
    hash_to_paths: dict[str, list[Path]] = defaultdict(list)
    for path in captured_files:
        hash_to_paths[sha256_file(path)].append(path)

    db_rows = conn.execute(
        """
        SELECT id, report_key, source_hash, capture_path, markdown_path,
               raw_count, parsed_count, event_count
        FROM reports
        ORDER BY report_key
        """
    ).fetchall()
    db_hashes = {row["source_hash"] for row in db_rows}
    failures: list[str] = []
    warnings: list[str] = []

    def scalar(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    unique_hash_count = len(hash_to_paths)
    duplicate_groups = [paths for paths in hash_to_paths.values() if len(paths) > 1]
    missing_hashes = sorted(set(hash_to_paths) - db_hashes)
    extra_hashes = sorted(db_hashes - set(hash_to_paths))
    if missing_hashes:
        failures.append(f"{len(missing_hashes)} unique captured hash(es) are missing from reports")
    if extra_hashes:
        warnings.append(f"{len(extra_hashes)} report hash(es) are not present in {captured_dir}")

    checks = {
        "events_without_report": "SELECT COUNT(*) FROM events e LEFT JOIN reports r ON r.id=e.report_id WHERE r.id IS NULL",
        "participants_without_report": "SELECT COUNT(*) FROM participants p LEFT JOIN reports r ON r.id=p.report_id WHERE r.id IS NULL",
        "damage_without_event": "SELECT COUNT(*) FROM damage_contexts d LEFT JOIN events e ON e.id=d.event_id WHERE e.id IS NULL",
        "state_without_event": "SELECT COUNT(*) FROM state_changes s LEFT JOIN events e ON e.id=s.event_id WHERE e.id IS NULL",
        "event_count_mismatch": "SELECT COUNT(*) FROM reports r WHERE r.event_count != (SELECT COUNT(*) FROM events e WHERE e.report_id=r.id)",
        "damage_count_mismatch": """
            SELECT COUNT(*)
            FROM reports r
            WHERE (SELECT COUNT(*) FROM damage_contexts d WHERE d.report_id=r.id)
               != (SELECT COUNT(*) FROM events e WHERE e.report_id=r.id AND e.event_type IN ('damage','damage_raw'))
        """,
        "duplicate_report_keys": "SELECT COUNT(*) FROM (SELECT report_key FROM reports GROUP BY report_key HAVING COUNT(*)>1)",
        "duplicate_hashes": "SELECT COUNT(*) FROM (SELECT source_hash FROM reports GROUP BY source_hash HAVING COUNT(*)>1)",
    }
    for name, sql in checks.items():
        count = scalar(sql)
        if count:
            failures.append(f"{name}: {count}")

    no_markdown = conn.execute(
        """
        SELECT report_key, raw_count, event_count, capture_path
        FROM reports
        WHERE markdown_path IS NULL
        ORDER BY report_key
        """
    ).fetchall()
    for row in no_markdown:
        warnings.append(
            f"{row['report_key']} has no matching Markdown path "
            f"(raw={row['raw_count']}, events={row['event_count']})"
        )

    zero_event = conn.execute(
        """
        SELECT report_key, raw_count, capture_path
        FROM reports
        WHERE event_count = 0 OR raw_count = 0
        ORDER BY report_key
        """
    ).fetchall()
    for row in zero_event:
        warnings.append(f"{row['report_key']} has no parsed battle events (raw={row['raw_count']})")

    if deep:
        for row in db_rows:
            capture_path = Path(row["capture_path"])
            if not capture_path.exists():
                failures.append(f"{row['report_key']} capture path does not exist: {capture_path}")
                continue
            parsed = parse_capture(capture_path, dedup=dedup)
            if parsed.raw_count != row["raw_count"]:
                failures.append(
                    f"{row['report_key']} raw_count mismatch: db={row['raw_count']} parsed={parsed.raw_count}"
                )
            if parsed.parsed_count != row["parsed_count"]:
                failures.append(
                    f"{row['report_key']} parsed_count mismatch: db={row['parsed_count']} parsed={parsed.parsed_count}"
                )
            if parsed.event_count != row["event_count"]:
                failures.append(
                    f"{row['report_key']} event_count mismatch: db={row['event_count']} parsed={parsed.event_count}"
                )

    print(f"captured_files: {len(captured_files)}")
    print(f"unique_captured_hashes: {unique_hash_count}")
    print(f"markdown_files: {len(markdown_files)}")
    print(f"db_reports: {len(db_rows)}")
    print(f"db_events: {scalar('SELECT COUNT(*) FROM events')}")
    print(f"db_damage_contexts: {scalar('SELECT COUNT(*) FROM damage_contexts')}")
    print(f"db_participants: {scalar('SELECT COUNT(*) FROM participants')}")
    print(f"db_state_changes: {scalar('SELECT COUNT(*) FROM state_changes')}")
    print(f"db_inferred_rules: {scalar('SELECT COUNT(*) FROM game_inferred_rules')}")
    print(f"duplicate_capture_groups: {len(duplicate_groups)}")
    for group in duplicate_groups:
        print("  duplicate_group:")
        for path in group:
            print(f"    {path}")

    if warnings:
        print("WARNINGS:")
        for item in warnings:
            print(f"  - {item}")
    if failures:
        print("FAILURES:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("PASS: database covers all unique captured files and internal counts are consistent")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入战报并生成 SQLite 伤害上下文数据库")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--dedup", choices=("local", "none"), default="local", help="解析器去重策略")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="只初始化数据库表")
    init_parser.set_defaults(command="init")

    import_parser = subparsers.add_parser("import", help="导入单个 captured/battle_*.txt")
    import_parser.add_argument("file", help="captured 战报文本路径")
    import_parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="Markdown 战报目录")

    import_all_parser = subparsers.add_parser("import-all", help="导入 captured 目录下所有战报")
    import_all_parser.add_argument("--captured-dir", default=str(DEFAULT_CAPTURED_DIR), help="captured 目录")
    import_all_parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="Markdown 战报目录")

    knowledge_parser = subparsers.add_parser("import-knowledge", help="导入战法与韬略资料库")
    knowledge_parser.add_argument("--skills", default=str(DEFAULT_SKILLS_DOC), help="skills_database.md 路径")
    knowledge_parser.add_argument("--tactics", default=str(DEFAULT_TACTICS_DOC), help="tactics_database.md 路径")
    knowledge_parser.add_argument("--basics", default=str(DEFAULT_BASICS_DOC), help="battle_basics.md 路径")
    knowledge_parser.add_argument("--no-basics", action="store_true", help="不导入基础规则资料")
    knowledge_parser.add_argument("--no-clear", action="store_true", help="不先清空现有资料库表")

    examples_parser = subparsers.add_parser("show-damage", help="显示若干伤害上下文样例")
    examples_parser.add_argument("--limit", type=int, default=10)

    validate_parser = subparsers.add_parser("validate", help="校验 captured/SQLite 覆盖和内部一致性")
    validate_parser.add_argument("--captured-dir", default=str(DEFAULT_CAPTURED_DIR), help="captured 目录")
    validate_parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="Markdown 战报目录")
    validate_parser.add_argument("--deep", action="store_true", help="重新解析库中 captured 文件并逐份对比计数")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    conn = connect(Path(args.db))
    setup_schema(conn)

    if args.command == "init":
        print(f"SQLite schema ready: {Path(args.db).resolve()}")
        return

    if args.command == "import":
        report_id = import_capture(
            conn,
            Path(args.file),
            reports_dir=Path(args.reports_dir),
            dedup=args.dedup,
        )
        print(f"Imported report_id={report_id}: {Path(args.file).resolve()}")
        return

    if args.command == "import-all":
        results = import_all(
            conn,
            captured_dir=Path(args.captured_dir),
            reports_dir=Path(args.reports_dir),
            dedup=args.dedup,
        )
        print(f"Imported {len(results)} reports into {Path(args.db).resolve()}")
        for path, report_id in results:
            print(f"  report_id={report_id}: {path}")
        return

    if args.command == "import-knowledge":
        counts = import_knowledge(
            conn,
            skills_path=Path(args.skills),
            tactics_path=Path(args.tactics),
            basics_path=Path(args.basics),
            clear=not args.no_clear,
            include_basics=not args.no_basics,
        )
        print(
            f"Imported knowledge into {Path(args.db).resolve()}: "
            f"skills={counts['skills']}, skill_versions={counts['skill_versions']}, "
            f"tactics={counts['tactics']}, basic_rules={counts['basic_rules']}, "
            f"statuses={counts['statuses']}, formations={counts['formations']}, "
            f"unit_counters={counts['unit_counters']}, inferred_rules={counts['inferred_rules']}"
        )
        return

    if args.command == "show-damage":
        print_damage_examples(conn, args.limit)
        return

    if args.command == "validate":
        code = validate_database(
            conn,
            captured_dir=Path(args.captured_dir),
            reports_dir=Path(args.reports_dir),
            deep=args.deep,
            dedup=args.dedup,
        )
        sys.exit(code)


if __name__ == "__main__":
    main()
