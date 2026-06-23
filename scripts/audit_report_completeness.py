#!/usr/bin/env python3
"""Audit SQLite battle reports for missing human-reviewed configuration.

This script is read-only. It treats SQLite as the source of truth and produces a
Markdown review sheet so incomplete reports can be grouped before manual repair.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "sanmou_battles.sqlite"
DEFAULT_OUTPUT = ROOT / "docs" / "report_completeness_audit.md"

PLACEHOLDER_FRAGMENTS = (
    "待手动补充",
    "待截图确认",
    "待补",
    "待判断",
    "未知",
)

CORE_FIELDS = (
    ("redness", "武将红度"),
    ("gold_seals", "金印数"),
    ("unit_type", "兵种"),
    ("skills_text", "战法顺序"),
)

ANALYSIS_FIELDS = (
    ("country", "国家"),
    ("level", "等级"),
    ("initial_troops", "初始兵力"),
    ("innate_skill", "自带战法"),
    ("innate_skill_redness", "自带战法红度"),
)

OPTIONAL_FIELDS = (
    ("grade", "品级"),
    ("tactics_text", "韬略"),
)

ALL_FIELDS = CORE_FIELDS + ANALYSIS_FIELDS + OPTIONAL_FIELDS


@dataclass(frozen=True)
class ReportAudit:
    report_id: int
    report_key: str
    event_count: int
    participant_count: int
    missing_by_field: dict[str, int]
    missing_labels: list[str]
    status: str
    full_signature: str


@dataclass(frozen=True)
class TeamAudit:
    key: str
    report_id: int
    report_key: str
    side: str
    heroes: str
    skills: str
    weak_signature: bool
    missing_labels: list[str]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def is_missing(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return any(fragment in text for fragment in PLACEHOLDER_FRAGMENTS)


def md_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def short_text(value: str, limit: int = 54) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def fetch_report_rows(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT
            r.id AS report_id,
            r.report_key,
            r.raw_count,
            r.parsed_count,
            r.event_count,
            r.markdown_path,
            p.id AS participant_id,
            p.side,
            p.team_id,
            p.hero,
            p.country,
            p.level,
            p.grade,
            p.unit_type,
            p.initial_troops,
            p.redness,
            p.gold_seals,
            p.innate_skill,
            p.innate_skill_redness,
            p.skills_text,
            p.tactics_text
        FROM reports r
        LEFT JOIN participants p ON p.report_id = r.id
        ORDER BY r.report_key, p.side, p.hero
        """
    ).fetchall()
    by_report: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_report[int(row["report_id"])].append(row)
    return by_report


def participant_rows(rows: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in rows if row["participant_id"] is not None]


def missing_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    return {
        field: sum(1 for row in rows if is_missing(row[field]))
        for field, _label in ALL_FIELDS
    }


def missing_labels(counts: dict[str, int], total: int, fields: Iterable[tuple[str, str]]) -> list[str]:
    labels = []
    for field, label in fields:
        count = counts[field]
        if count:
            labels.append(f"{label}{count}/{total}")
    return labels


def side_sort_key(side: str) -> tuple[int, str]:
    priority = {
        "我方": 0,
        "攻方": 1,
        "阵营 A": 2,
        "守方": 3,
        "守军": 4,
        "阵营 B": 5,
    }
    return (priority.get(side, 99), side)


def team_key(rows: list[sqlite3.Row]) -> tuple[str, bool]:
    heroes = sorted(str(row["hero"]) for row in rows if row["hero"])
    skills = {
        str(row["hero"]): str(row["skills_text"]).strip()
        for row in rows
        if row["hero"] and not is_missing(row["skills_text"])
    }
    weak = len(skills) != len(heroes)
    if skills:
        payload = {
            "heroes": heroes,
            "skills": {hero: skills.get(hero, "") for hero in heroes},
        }
    else:
        payload = {"heroes": heroes}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True), weak


def side_groups(rows: list[sqlite3.Row]) -> list[tuple[str, list[sqlite3.Row]]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        side = str(row["side"] or "未识别")
        grouped[side].append(row)
    return sorted(grouped.items(), key=lambda item: side_sort_key(item[0]))


def full_report_signature(rows: list[sqlite3.Row]) -> str:
    payload = []
    for side, team_rows in side_groups(rows):
        key, _weak = team_key(team_rows)
        payload.append({"side": side, "team": json.loads(key)})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def classify_report(row: sqlite3.Row, participants: list[sqlite3.Row], counts: dict[str, int]) -> str:
    if int(row["event_count"]) == 0 or int(row["raw_count"]) == 0:
        return "建议删除：无有效流水"
    if not participants:
        return "建议检查：无武将配置"
    if any(counts[field] for field, _label in CORE_FIELDS):
        return "流水可用：核心配置缺失"
    if any(counts[field] for field, _label in ANALYSIS_FIELDS):
        return "核心可用：分析字段缺失"
    if any(counts[field] for field, _label in OPTIONAL_FIELDS):
        return "核心可用：仅附加字段缺失"
    return "配置完整"


def audit_reports(by_report: dict[int, list[sqlite3.Row]]) -> list[ReportAudit]:
    audits = []
    for report_id, rows in by_report.items():
        first = rows[0]
        participants = participant_rows(rows)
        counts = missing_counts(participants)
        total = len(participants)
        labels = []
        labels.extend(missing_labels(counts, total, CORE_FIELDS))
        labels.extend(missing_labels(counts, total, ANALYSIS_FIELDS))
        labels.extend(missing_labels(counts, total, OPTIONAL_FIELDS))
        audits.append(
            ReportAudit(
                report_id=report_id,
                report_key=str(first["report_key"]),
                event_count=int(first["event_count"]),
                participant_count=total,
                missing_by_field=counts,
                missing_labels=labels,
                status=classify_report(first, participants, counts),
                full_signature=full_report_signature(participants),
            )
        )
    return sorted(audits, key=lambda item: item.report_key)


def audit_teams(by_report: dict[int, list[sqlite3.Row]]) -> list[TeamAudit]:
    teams = []
    for report_id, rows in by_report.items():
        first = rows[0]
        participants = participant_rows(rows)
        for side, team_rows in side_groups(participants):
            key, weak = team_key(team_rows)
            counts = missing_counts(team_rows)
            labels = []
            labels.extend(missing_labels(counts, len(team_rows), CORE_FIELDS))
            labels.extend(missing_labels(counts, len(team_rows), ANALYSIS_FIELDS))
            labels.extend(missing_labels(counts, len(team_rows), OPTIONAL_FIELDS))
            heroes = "、".join(str(row["hero"]) for row in sorted(team_rows, key=lambda row: str(row["hero"])))
            skills = "；".join(
                f"{row['hero']}：{row['skills_text']}"
                for row in sorted(team_rows, key=lambda row: str(row["hero"]))
                if not is_missing(row["skills_text"])
            )
            teams.append(
                TeamAudit(
                    key=key,
                    report_id=report_id,
                    report_key=str(first["report_key"]),
                    side=side,
                    heroes=heroes,
                    skills=skills,
                    weak_signature=weak,
                    missing_labels=labels,
                )
            )
    return teams


def field_summary(by_report: dict[int, list[sqlite3.Row]]) -> list[tuple[str, str, int, int]]:
    participants = []
    for rows in by_report.values():
        participants.extend(participant_rows(rows))
    result = []
    total = len(participants)
    for field, label in ALL_FIELDS:
        result.append((field, label, sum(1 for row in participants if is_missing(row[field])), total))
    return result


def table(headers: list[str], rows: Iterable[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_cell(value) for value in row) + " |")
    return lines


def build_markdown(
    audits: list[ReportAudit],
    teams: list[TeamAudit],
    field_rows: list[tuple[str, str, int, int]],
    *,
    skill_detail_count: int,
) -> str:
    status_counts = Counter(audit.status for audit in audits)
    full_groups: dict[str, list[ReportAudit]] = defaultdict(list)
    for audit in audits:
        full_groups[audit.full_signature].append(audit)

    team_groups: dict[str, list[TeamAudit]] = defaultdict(list)
    for team in teams:
        team_groups[team.key].append(team)
    repeated_team_groups = sorted(
        (group for group in team_groups.values() if len(group) > 1),
        key=lambda group: (-len(group), group[0].report_key, group[0].side),
    )
    repeated_full_groups = sorted(
        (group for group in full_groups.values() if len(group) > 1),
        key=lambda group: (-len(group), group[0].report_key),
    )

    delete_candidates = [
        audit
        for audit in audits
        if audit.status.startswith("建议删除")
    ]

    lines = [
        "# 战报配置完整性审计",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 战报数：{len(audits)}",
        f"- 武将配置行：{sum(audit.participant_count for audit in audits)}",
        f"- 战法详情补充行：{skill_detail_count}",
        "- 口径：只读 SQLite，不从 Markdown 反向解析战斗流水。",
        "",
        "## 建议结论",
        "",
        "- 不建议因为缺红度、兵种、韬略就删除战报；这类战报仍可用于流水、触发、回合和状态分析。",
        "- 建议删除的对象仅限无有效流水、无武将配置、重复导入或截图截坏到无法解析的记录。",
        "- 缺资料但有事件的战报应保留，并在后续分析中按字段完整性过滤。",
        "",
        "## 状态汇总",
        "",
    ]
    lines.extend(
        table(
            ["状态", "战报数"],
            [[status, count] for status, count in sorted(status_counts.items())],
        )
    )
    lines.extend(["", "## 字段缺失汇总", ""])
    lines.extend(
        table(
            ["字段", "缺失行", "总行", "缺失率"],
            [
                [label, missing, total, f"{missing / total:.1%}" if total else "0.0%"]
                for _field, label, missing, total in field_rows
            ],
        )
    )
    lines.extend(["", "## 删除候选", ""])
    if delete_candidates:
        lines.extend(
            table(
                ["战报", "事件数", "武将行", "原因"],
                [
                    [audit.report_key, audit.event_count, audit.participant_count, audit.status]
                    for audit in delete_candidates
                ],
            )
        )
    else:
        lines.append("当前没有无有效流水的删除候选。")

    lines.extend(["", "## 可继承的队伍配置组", ""])
    if repeated_team_groups:
        rows = []
        for index, group in enumerate(repeated_team_groups, 1):
            representative = group[0]
            reports = "、".join(item.report_key for item in group[:8])
            if len(group) > 8:
                reports += f" 等 {len(group)} 份"
            weak = "是" if any(item.weak_signature for item in group) else "否"
            missing = "；".join(representative.missing_labels) or "无"
            rows.append(
                [
                    f"T{index:02d}",
                    len(group),
                    representative.report_key,
                    representative.side,
                    representative.heroes,
                    short_text(representative.skills or "无战法文本"),
                    weak,
                    missing,
                    reports,
                ]
            )
        lines.extend(
            table(
                ["组", "出现", "代表战报", "阵营", "武将", "战法摘要", "弱匹配", "代表缺失", "覆盖战报"],
                rows,
            )
        )
    else:
        lines.append("没有找到可重复继承的队伍配置组。")

    lines.extend(["", "## 完全相同对阵组", ""])
    if repeated_full_groups:
        rows = []
        for index, group in enumerate(repeated_full_groups, 1):
            reports = "、".join(item.report_key for item in group[:10])
            if len(group) > 10:
                reports += f" 等 {len(group)} 份"
            missing = "；".join(group[0].missing_labels) or "无"
            rows.append([f"R{index:02d}", len(group), group[0].report_key, group[0].status, missing, reports])
        lines.extend(
            table(
                ["组", "份数", "代表战报", "代表状态", "代表缺失", "覆盖战报"],
                rows,
            )
        )
    else:
        lines.append("没有找到完全相同的双方对阵组。")

    lines.extend(["", "## 逐份战报缺失清单", ""])
    lines.extend(
        table(
            ["战报", "状态", "事件数", "武将行", "缺失字段"],
            [
                [
                    audit.report_key,
                    audit.status,
                    audit.event_count,
                    audit.participant_count,
                    "；".join(audit.missing_labels) or "无",
                ]
                for audit in audits
            ],
        )
    )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="审计 SQLite 战报配置缺失情况并生成 Markdown 清单")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 Markdown 路径")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    output_path = Path(args.output)
    if not db_path.exists():
        raise SystemExit(f"数据库不存在: {db_path}")

    conn = connect(db_path)
    try:
        by_report = fetch_report_rows(conn)
        audits = audit_reports(by_report)
        teams = audit_teams(by_report)
        field_rows = field_summary(by_report)
        try:
            skill_detail_count = int(conn.execute("SELECT COUNT(*) FROM report_skill_details").fetchone()[0])
        except sqlite3.DatabaseError:
            skill_detail_count = 0
    finally:
        conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_markdown(audits, teams, field_rows, skill_detail_count=skill_detail_count),
        encoding="utf-8",
        newline="\n",
    )
    print(f"已生成: {output_path}")
    print(f"战报数: {len(audits)}")
    print(f"删除候选: {sum(1 for audit in audits if audit.status.startswith('建议删除'))}")
    print(f"核心配置缺失: {sum(1 for audit in audits if audit.status == '流水可用：核心配置缺失')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
