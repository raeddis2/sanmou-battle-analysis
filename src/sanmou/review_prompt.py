"""Interactive PowerShell review prompts for newly captured battle reports."""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sanmou import report_parser
from sanmou.rules import (
    DEFAULT_HERO_LEVEL,
    NPC_GRADE,
    NPC_GOLD_SEALS,
    NPC_INITIAL_TROOPS,
    NPC_REDNESS,
    NPC_TACTICS,
    PLAYER_MAX_TROOPS,
    derive_grade,
    is_npc_initial_troops,
    normalize_gold_seals,
    normalize_progression_redness,
)


UNIT_TYPES = {"盾兵", "弓兵", "枪兵", "骑兵"}
REDO_COMMANDS = {"重填", "重新填", "redo", ":redo", "r"}


class RedoHero(Exception):
    """Restart the current hero review."""


@dataclass
class HeroReview:
    side: str
    team_id: str
    hero: str
    country: str = ""
    level: str = str(DEFAULT_HERO_LEVEL)
    redness: str = ""
    grade: str = ""
    gold_seals: str = ""
    initial_troops: str = ""
    unit_type: str = ""
    innate_skill: str = ""
    innate_skill_redness: str = ""
    skills: list[tuple[str, str]] = field(default_factory=list)
    tactics: str = ""


@dataclass
class SkillVersion:
    skill_name: str
    red_level_text: str
    skill_level: str
    skill_type: str
    description_raw: str


def setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def normalize_red(value: str, default: str = "0红") -> str:
    return normalize_progression_redness(value, default)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if value.lower() in REDO_COMMANDS:
        raise RedoHero
    return value if value else (default or "")


def ask_required(prompt: str, default: str | None = None) -> str:
    while True:
        value = ask(prompt, default)
        if value:
            return value
        print("不能为空，请重新输入。")


def ask_unit(prompt: str, default: str = "") -> str:
    while True:
        value = ask(prompt, default)
        if not value or value in UNIT_TYPES:
            return value
        print("请输入：盾兵 / 弓兵 / 枪兵 / 骑兵，或留空。")


def unit_prompt_for(report: report_parser.ParsedReport, team_id: str, hero: str) -> tuple[str, str]:
    default = report_parser.unit_type_for(report, team_id, hero) or ""
    if not default:
        return "兵种", ""
    if team_id in report.unit_types and report.unit_types[team_id].get(hero) == default:
        return "兵种已从流水识别", default
    return "兵种（默认推测）", default


def ask_bool(prompt: str, default: bool = False) -> bool:
    default_text = "Y" if default else "N"
    value = ask(f"{prompt} (y/n)", default_text).lower()
    return value in {"y", "yes", "1", "是"}


def lookup_skill_version(conn: sqlite3.Connection, skill_name: str, red_text: str) -> SkillVersion | None:
    red_text = normalize_red(red_text)
    row = conn.execute(
        """
        SELECT skill_name, red_level_text, skill_level, skill_type, description_raw
        FROM game_skill_versions
        WHERE skill_name = ? AND red_level_text = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (skill_name, red_text),
    ).fetchone()
    if not row and red_text == "0红":
        row = conn.execute(
            """
            SELECT skill_name, red_level_text, skill_level, skill_type, description_raw
            FROM game_skill_versions
            WHERE skill_name = ?
            ORDER BY COALESCE(red_level, 99), id
            LIMIT 1
            """,
            (skill_name,),
        ).fetchone()
    if not row:
        return None
    return SkillVersion(
        skill_name=row["skill_name"],
        red_level_text=row["red_level_text"],
        skill_level=row["skill_level"] or "",
        skill_type=row["skill_type"] or "",
        description_raw=row["description_raw"] or "",
    )


def confirm_review(review: HeroReview) -> bool:
    print()
    print("本武将配置预览：")
    print(
        f"- {review.side} / {review.hero}: 初始兵力={review.initial_troops or '空'}，"
        f"国家={review.country or '空'}，等级={review.level or '空'}，"
        f"红度={review.redness or '空'}，品级={review.grade or '空'}，金印={review.gold_seals or '空'}"
    )
    print(
        f"- 兵种={review.unit_type or '空'}，自带战法={review.innate_skill or '空'}，"
        f"自带战法红度={review.innate_skill_redness or '空'}"
    )
    print(f"- 战法顺序={skill_order_text(review)}")
    print(f"- 韬略={review.tactics or '空'}")
    return ask_bool("确认本武将配置", True)


def infer_skill_casts(report: report_parser.ParsedReport) -> dict[str, list[str]]:
    by_hero: dict[str, list[str]] = {}
    for _section, events in report.sections:
        for ev in events:
            hero = ev.get("hero") or ev.get("source")
            skill = ev.get("skill")
            if not hero or not skill:
                continue
            if report_parser.is_tactic_name(skill):
                continue
            if ev.get("type") not in {
                "skill_cast",
                "skill_gain",
                "skill_miss",
                "skill_blocked",
                "buff_exec",
                "damage",
                "effect_source",
                "effect_miss",
                "heal_limit",
            }:
                continue
            skills = by_hero.setdefault(hero, [])
            if skill not in skills:
                skills.append(skill)
    return by_hero


def review_heroes(
    report: report_parser.ParsedReport,
    conn: sqlite3.Connection,
) -> tuple[list[HeroReview], dict[tuple[str, str], SkillVersion | None]]:
    rows = report_parser.collect_report_heroes(report)
    inferred_skills = infer_skill_casts(report)
    reviews: list[HeroReview] = []
    skill_versions: dict[tuple[str, str], SkillVersion | None] = {}

    print()
    print("开始补全战报配置。直接回车会使用默认值；不确定可以留空。")
    print("任意输入项可输入 `重填` 或 `:redo` 重新填写当前武将。")
    print()

    for side, team_id, hero in rows:
        while True:
            try:
                print("-" * 60)
                print(f"{side} / {hero}")
                review = HeroReview(side=side, team_id=team_id, hero=hero)
                inferred_initial_troops = report_parser.initial_troops_for(report, team_id, hero)
                if inferred_initial_troops:
                    review.initial_troops = str(inferred_initial_troops)
                    print(f"初始兵力已从流水识别：{review.initial_troops}")
                else:
                    review.initial_troops = ask("初始兵力", "")
                review.country = ask("国家", "")
                review.level = ask("等级", str(DEFAULT_HERO_LEVEL))
                max_observed_troops = report_parser.max_observed_troops_for(report, team_id, hero)
                is_npc = is_npc_initial_troops(review.initial_troops) or (
                    max_observed_troops is not None and max_observed_troops > PLAYER_MAX_TROOPS
                )
                if is_npc:
                    if is_npc_initial_troops(review.initial_troops):
                        reason = f"初始兵力为 {NPC_INITIAL_TROOPS}"
                    else:
                        reason = f"流水中最高兵力为 {max_observed_troops}，超过玩家上限 {PLAYER_MAX_TROOPS}"
                    print(f"{reason}，按 NPC 处理：红度/品级/金印/自带战法等级均固定为 0。")
                    review.redness = NPC_REDNESS
                    review.grade = NPC_GRADE
                    review.gold_seals = NPC_GOLD_SEALS
                else:
                    review.redness = normalize_red(ask("武将红度", "0"))
                    review.gold_seals = normalize_gold_seals(ask("金印数/品级/自带战法等级", "0"))
                    review.grade = derive_grade(gold_seals=review.gold_seals)
                unit_prompt, unit_default = unit_prompt_for(report, team_id, hero)
                review.unit_type = ask_unit(unit_prompt, unit_default)
                review.innate_skill = ask("自带战法", inferred_skills.get(hero, [""])[0] if inferred_skills.get(hero) else "")
                review.innate_skill_redness = (
                    NPC_REDNESS
                    if is_npc
                    else normalize_red(ask("自带战法等级/红度", review.grade or "0"))
                )
                review.grade = derive_grade(
                    grade=review.grade,
                    gold_seals=review.gold_seals,
                    innate_skill_redness=review.innate_skill_redness,
                )

                candidates = list(inferred_skills.get(hero, []))
                if review.innate_skill and review.innate_skill not in candidates:
                    candidates.insert(0, review.innate_skill)
                if candidates:
                    print(f"识别到战法：{' / '.join(candidates)}")
                skill_text = ask("本武将战法顺序（用 / 分隔，留空用识别结果）", " / ".join(candidates))
                skill_names = [part.strip() for part in re.split(r"[/／,，]", skill_text) if part.strip()]
                for skill_name in skill_names:
                    default_red = review.innate_skill_redness if skill_name == review.innate_skill else "0红"
                    red = NPC_REDNESS if is_npc else normalize_red(ask(f"【{skill_name}】几红", default_red))
                    if is_npc:
                        print(f"【{skill_name}】按 NPC 规则固定为 0红")
                    review.skills.append((skill_name, red))
                    skill_versions[(skill_name, red)] = lookup_skill_version(conn, skill_name, red)
                    if skill_versions[(skill_name, red)]:
                        version = skill_versions[(skill_name, red)]
                        print(f"  找到：{version.skill_name} {version.red_level_text} {version.skill_type}")
                    else:
                        print("  数据库未找到该红度说明，MD 会保留待补说明。")
                inferred_tactics = report_parser.tactics_text_for(report, team_id, hero)
                if is_npc:
                    review.tactics = NPC_TACTICS
                    print(f"韬略按 NPC 规则固定为：{review.tactics}")
                elif inferred_tactics:
                    review.tactics = inferred_tactics
                    print(f"韬略已从流水识别：{review.tactics}")
                else:
                    review.tactics = ask("韬略", "")
                if confirm_review(review):
                    reviews.append(review)
                    break
                print("已选择重填当前武将。")
            except RedoHero:
                print("已选择重填当前武将。")

    return reviews, skill_versions


def skill_order_text(review: HeroReview) -> str:
    return " / ".join(f"{name}（{red}）" for name, red in review.skills) or "待手动补充"


def counter_result(unit_type: str, opponent_units: set[str]) -> str:
    if not unit_type:
        return "待判断"
    counters = {"盾兵": "弓兵", "弓兵": "枪兵", "枪兵": "骑兵", "骑兵": "盾兵"}
    countered_by = {v: k for k, v in counters.items()}
    beats = counters.get(unit_type)
    loses_to = countered_by.get(unit_type)
    if beats in opponent_units:
        return f"克制{beats}"
    if loses_to in opponent_units:
        return f"被{loses_to}克制"
    return "无克制"


def build_review_prefix(
    reviews: list[HeroReview],
    skill_versions: dict[tuple[str, str], SkillVersion | None],
) -> str:
    lines: list[str] = ["# 三国谋定天下战报", ""]
    lines.append("## 武将红度与金印")
    lines.append("")
    lines.append("| 阵营 | 武将 | 红度 | 金印数 | 自带战法 | 自带战法红度 | 截图可见战法/统计顺序 | 备注 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for review in reviews:
        lines.append(
            f"| {review.side} | {review.hero} | {review.redness} | {review.gold_seals} | "
            f"{review.innate_skill or '待手动补充'} | {review.innate_skill_redness or '待手动补充'} | "
            f"{skill_order_text(review)} | PowerShell 交互录入 |"
        )
    lines.append("")

    sides: dict[str, list[str]] = {}
    for review in reviews:
        sides.setdefault(review.side, []).append(review.hero)
    lines.append("## 对阵")
    for side, heroes in sides.items():
        lines.append(f"- **{side}**: {', '.join(heroes)}")
    lines.append("")

    lines.append("## 总览补充")
    lines.append("")
    lines.append("| 阵营 | 武将 | 国家 | 等级 | 品级 | 兵种 | 初始兵力 | 自带战法 | 战法顺序与红度 | 韬略 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for review in reviews:
        lines.append(
            f"| {review.side} | {review.hero} | {review.country or '待手动补充'} | {review.level or DEFAULT_HERO_LEVEL} | {review.grade or '待手动补充'} | "
            f"{review.unit_type or '待手动补充'} | {review.initial_troops or '待手动补充'} | {review.innate_skill or '待手动补充'} | "
            f"{skill_order_text(review)} | {review.tactics or '待手动补充'} |"
        )
    lines.append("")

    lines.append("## 战法详情补充")
    lines.append("")
    seen: set[tuple[str, str]] = set()
    for review in reviews:
        for skill_name, red in review.skills:
            key = (skill_name, red)
            if key in seen:
                continue
            seen.add(key)
            version = skill_versions.get(key)
            lines.append(f"### {skill_name}")
            lines.append("")
            lines.append(f"- 战法红度：{red}")
            lines.append(f"- 战法类型：{version.skill_type if version else '待手动补充'}")
            lines.append("- 适用兵种：盾兵、弓兵、枪兵、骑兵")
            if version:
                lines.append(f"- 说明：{version.description_raw}")
                if version.skill_level:
                    lines.append(f"- 等级：{version.skill_level}")
            else:
                lines.append("- 说明：待手动补充")
            lines.append("")

    lines.append("## 兵种克制核对")
    lines.append("")
    lines.append("- 兵种克制关系：盾克弓、弓克枪、枪克骑、骑克盾。")
    lines.append("- 克制目标时伤害独立乘区 `×1.15`；被目标克制时独立乘区 `×0.85`；无克制 `×1.00`。")
    lines.append("")
    lines.append("| 阵营 | 武将 | 兵种 | 克制/被克制核对 |")
    lines.append("| --- | --- | --- | --- |")
    units_by_side: dict[str, set[str]] = {}
    for review in reviews:
        if review.unit_type:
            units_by_side.setdefault(review.side, set()).add(review.unit_type)
    for review in reviews:
        opponent_units = set()
        for side, units in units_by_side.items():
            if side != review.side:
                opponent_units.update(units)
        lines.append(
            f"| {review.side} | {review.hero} | {review.unit_type or '待手动补充'} | "
            f"{counter_result(review.unit_type, opponent_units)} |"
        )
    lines.append("")
    return "\n".join(lines)


def apply_review_to_markdown(md_path: Path, prefix: str) -> None:
    text = md_path.read_text(encoding="utf-8-sig")
    match = re.search(r"(?m)^## 解析统计\s*$", text)
    if not match:
        raise ValueError(f"{md_path} does not contain ## 解析统计")
    suffix = text[match.start() :].lstrip()
    md_path.write_text(f"{prefix.rstrip()}\n\n{suffix}", encoding="utf-8", newline="")


def review_markdown_interactively(
    md_path: Path,
    report: report_parser.ParsedReport,
    db_path: Path,
) -> bool:
    setup_stdio()
    if not ask_bool("是否在 PowerShell 中补全本战报配置", True):
        return False
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        reviews, skill_versions = review_heroes(report, conn)
    finally:
        conn.close()
    prefix = build_review_prefix(reviews, skill_versions)
    apply_review_to_markdown(md_path, prefix)
    print(f"已写入人工配置: {md_path}")
    return True
