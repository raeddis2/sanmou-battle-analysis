"""Forward-check the user's force-type damage formula against SQLite.

The script reads only ``data/sanmou_battles.sqlite`` and compares final
observed damage with the forward prediction:

    (300 + 0.5 * setup_force) * current_force
    / (target_effective_command + 160)
    * skill_multiplier
    * troop_factor
    * unit_counter
    * damage/taken buckets
    * special multipliers
    * grade multiplier

Huang Gai is deliberately excluded from the force-type source set because the
current hypothesis treats him as a command/统率-type hero.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable


DB_PATH = Path("data/sanmou_battles.sqlite")
OUT_DETAIL_CSV = Path("docs/forward_force_damage_formula_20260623.csv")
OUT_SUMMARY_MD = Path("docs/forward_force_damage_formula_20260623.md")

B_FIXED = 160.0
TROOP_CAP = 9000.0
TROOP_ALPHA = 0.33
ATTR_PROPS = ("武力", "智力", "统率", "先攻")
COUNTRIES = ("魏", "蜀", "吴", "群")

# Fixed-coefficient force/physical skills that are explicit enough to forward
# check without inventing extra mechanics.
INCLUDED_PHYSICAL_SKILLS = {
    "摧坚克难",
    "纵马横枪",
    "定军扬威",
    "红妆缭乱",
    "万人之敌",
    "辕门射戟",
    "水淹七军",
    "骁勇无前",
}

EXCLUDED_PHYSICAL_SKILLS = {
    "七进七出": "同回合龙胆伤害系数递减，当前行无法可靠定位第几次龙胆",
    "千里突袭": "额外受双方先攻差和后排条件影响",
}

FORCE_TYPE_MANUAL_EXCLUDES = {
    "黄盖": "用户指定黄盖为统率型，不按武力型公式检验",
}


@dataclass(frozen=True)
class ForwardSample:
    damage_id: int
    report_id: int
    report_key: str
    event_order: int
    round_no: int | None
    source: str
    target: str
    skill: str
    buff: str
    sample_type: str
    observed_damage: int
    pred_raw: float
    pred_int_uncapped: int
    pred_final: int
    pred_grade_low: int
    pred_grade_high: int
    abs_diff: int
    rel_error: float
    obs_over_pred: float
    target_hp_before: float
    target_hp_after: float | None
    is_lethal: bool
    setup_force: float
    current_force: float
    target_command: float
    pierce_pct: float
    target_eff_command: float
    ignores_command: bool
    source_hp: float
    skill_multiplier: float
    troop_multiplier: float
    unit_multiplier: float
    source_bucket: float
    target_bucket: float
    source_damage_pct: float
    target_damage_taken_pct: float
    source_country_bonus: float
    target_country_reduction: float
    resolved_source_country: str
    resolved_target_country: str
    source_grade: int
    target_grade: int
    grade_multiplier: float
    trigger_multiplier: float
    special_multiplier: float
    zongma_negative_bonus: float
    zongma_negative_reason: str
    total_external_multiplier: float
    current_trigger: str
    source_is_npc: bool
    target_is_npc: bool
    raw_text: str


def safe_json_loads(value: str | None) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def parse_grade(value: object, *, is_npc: bool = False) -> int:
    if is_npc or value is None:
        return 0
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else 0


def normalize_unit(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text[0] if text else ""


def unit_counter_multiplier(source_unit: object, target_unit: object) -> float:
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    beats = {"盾": "弓", "弓": "枪", "枪": "骑", "骑": "盾"}
    if beats.get(source) == target:
        return 1.15
    if beats.get(target) == source:
        return 0.85
    return 1.0


def props(context_json: str | None) -> dict:
    context = safe_json_loads(context_json)
    props_value = context.get("props")
    return props_value if isinstance(props_value, dict) else {}


def prop_value(context_json: str | None, name: str) -> float:
    data = props(context_json).get(name)
    if isinstance(data, dict):
        return as_float(data.get("value")) or 0.0
    return 0.0


def active_buffs(value: str | None) -> dict:
    loaded = safe_json_loads(value)
    return loaded if isinstance(loaded, dict) else {}


def damage_numbers(row: sqlite3.Row) -> list[float]:
    try:
        loaded = json.loads(row["knowledge_numbers_json"] or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    values: list[float] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        value = as_float(item.get("value"))
        if value is not None:
            values.append(value)
    return values


def current_hp(context_json: str | None, initial_troops: object) -> float | None:
    context = safe_json_loads(context_json)
    hp = as_float(context.get("hp"))
    return hp if hp is not None else as_float(initial_troops)


def troop_factor(hp: float | None) -> float | None:
    if hp is None or hp <= 0:
        return None
    if hp >= TROOP_CAP:
        return 1.0
    return (hp / TROOP_CAP) ** TROOP_ALPHA


def current_trigger_ratio(
    source_context_json: str | None,
    action_context_json: str | None,
    damage_event_order: int,
) -> tuple[str, float]:
    source_context = safe_json_loads(source_context_json)
    trigger = source_context.get("recent_trigger")
    if not isinstance(trigger, dict):
        return "", 1.0
    action = safe_json_loads(action_context_json)
    trigger_order = as_float(trigger.get("event_order"))
    action_order = as_float(action.get("event_order"))
    if trigger_order is None or action_order is None:
        return "", 1.0
    if not (action_order <= trigger_order < damage_event_order):
        return "", 1.0
    ratio_text = str(trigger.get("damage_ratio") or "").strip()
    if ratio_text.endswith("%"):
        ratio = as_float(ratio_text[:-1])
    else:
        ratio = as_float(ratio_text)
    if not ratio:
        return str(trigger.get("trigger") or ""), 1.0
    return str(trigger.get("trigger") or ""), ratio / 100.0


def infer_country_from_context(context_json: str | None, prefix: str, suffix: str) -> str:
    data = props(context_json)
    for country in COUNTRIES:
        if prop_value(context_json, f"{prefix}{country}{suffix}") != 0:
            return country
    return ""


def fetch_hero_country_defaults(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT hero, country, COUNT(*) AS n
        FROM participants
        WHERE country IS NOT NULL AND country <> ''
        GROUP BY hero, country
        ORDER BY hero, n DESC
        """
    ).fetchall()
    by_hero: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in rows:
        hero = str(row["hero"])
        country = str(row["country"])
        if hero in by_hero and by_hero[hero] != country:
            ambiguous.add(hero)
            continue
        by_hero[hero] = country
    for hero in ambiguous:
        by_hero.pop(hero, None)
    return by_hero


def resolved_countries(
    row: sqlite3.Row,
    hero_country_defaults: dict[str, str],
) -> tuple[str, str]:
    source_country = str(row["source_country"] or "").strip()
    target_country = str(row["target_country"] or "").strip()
    if not source_country:
        source_country = hero_country_defaults.get(str(row["source"] or ""), "")
    if not target_country:
        target_country = hero_country_defaults.get(str(row["target"] or ""), "")
    if not target_country:
        target_country = infer_country_from_context(row["source_context_json"], "对", "武将伤害提升")
    if not source_country:
        source_country = infer_country_from_context(row["target_context_json"], "受到", "武将伤害降低")
    return source_country, target_country


def damage_buckets(
    row: sqlite3.Row,
    source_country: str,
    target_country: str,
) -> tuple[float, float, float, float]:
    source_country_bonus = (
        prop_value(row["source_context_json"], f"对{target_country}武将伤害提升")
        if target_country
        else 0.0
    )
    target_country_reduction = (
        prop_value(row["target_context_json"], f"受到{source_country}武将伤害降低")
        if source_country
        else 0.0
    )
    source_damage_pct = as_float(row["source_damage_pct"]) or 0.0
    target_taken_pct = as_float(row["target_damage_taken_pct"]) or 0.0
    source_bucket = 1.0 + (source_damage_pct + source_country_bonus) / 100.0
    target_bucket = 1.0 + (target_taken_pct - target_country_reduction) / 100.0
    return source_bucket, target_bucket, source_country_bonus, target_country_reduction


def pierce_pct(row: sqlite3.Row) -> float:
    flat = as_float(row["source_pierce_pct"]) or 0.0
    embedded = prop_value(row["source_context_json"], "破甲")
    if flat and embedded and abs(flat - embedded) <= 1e-6:
        return flat
    return max(flat, embedded)


def skill_multiplier(row: sqlite3.Row) -> float | None:
    skill = str(row["skill"] or "")
    if skill == "普通攻击":
        if row["action_type"] == "normal_attack" and row["damage_event_type"] == "damage_raw":
            return 1.0
        return None
    if skill not in INCLUDED_PHYSICAL_SKILLS:
        return None
    numbers = damage_numbers(row)
    pct_numbers = [value for value in numbers if value > 1]
    if skill == "摧坚克难":
        return max(pct_numbers) / 100.0 if pct_numbers else None
    if skill == "纵马横枪":
        candidates = [value for value in pct_numbers if 50.0 <= value <= 100.0]
        return max(candidates) / 100.0 if candidates else None
    if skill == "定军扬威":
        candidates = [value for value in pct_numbers if value >= 100.0]
        return max(candidates) / 100.0 if candidates else None
    if skill == "红妆缭乱":
        candidates = [value for value in pct_numbers if value >= 100.0]
        return max(candidates) / 100.0 if candidates else None
    if skill == "万人之敌":
        candidates = [value for value in pct_numbers if value >= 100.0]
        return max(candidates) / 100.0 if candidates else None
    if skill == "辕门射戟":
        candidates = [value for value in pct_numbers if value >= 100.0]
        return max(candidates) / 100.0 if candidates else None
    if skill == "水淹七军":
        candidates = [value for value in pct_numbers if value >= 100.0]
        return max(candidates) / 100.0 if candidates else None
    if skill == "骁勇无前":
        return 1.00
    return None


def ignores_command(row: sqlite3.Row) -> bool:
    return str(row["skill"] or "") == "纵马横枪"


def fetch_negative_statuses(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM game_statuses WHERE status_group = '负面状态'"
        )
    }


def target_has_negative_status(
    row: sqlite3.Row,
    negative_statuses: set[str],
) -> tuple[bool, str]:
    target_buffs = active_buffs(row["target_active_buffs_json"])
    official_hits = sorted(name for name in target_buffs if name in negative_statuses)
    if official_hits:
        return True, "official_negative:" + "/".join(official_hits)

    # 国色经《相思文赋》扩散后的负面影响常只体现在受到伤害结果值，
    # 不一定作为常规 buff 名进入 active_buffs。这里仅用明确窗口作证据。
    if "《相思文赋》" in target_buffs:
        return True, "xiangsiwenfu_guose_window"

    target_taken = as_float(row["target_damage_taken_pct"]) or 0.0
    if target_taken > 0:
        return True, "damage_taken_up_state_change"

    return False, ""


def special_multiplier(
    row: sqlite3.Row,
    negative_statuses: set[str],
) -> tuple[float, float, str, float | None]:
    multiplier = 1.0
    skill = str(row["skill"] or "")
    action_type = str(row["action_type"] or "")
    target_buffs = active_buffs(row["target_active_buffs_json"])
    source_buffs = active_buffs(row["source_active_buffs_json"])
    zongma_bonus = 1.0
    zongma_reason = ""
    zongma_coeff_override: float | None = None

    if "以静制动-静" in target_buffs:
        if skill == "普通攻击" and action_type == "normal_attack":
            multiplier *= 0.65
        elif skill in INCLUDED_PHYSICAL_SKILLS:
            multiplier *= 0.65

    if skill == "纵马横枪":
        has_negative, reason = target_has_negative_status(row, negative_statuses)
        if has_negative:
            base_coeff = skill_multiplier(row)
            # 战法说明的“追伤伤害提升20%”在当前马超样本中更像
            # 63.6% + 20个百分点，而不是 63.6% × 1.2。
            if base_coeff is not None:
                zongma_coeff_override = base_coeff + 0.20
                zongma_bonus = zongma_coeff_override / base_coeff
            zongma_reason = reason

    if "虚弱" in source_buffs:
        multiplier *= 0.30

    return multiplier, zongma_bonus, zongma_reason, zongma_coeff_override


def fetch_off_attrs(conn: sqlite3.Connection) -> dict[tuple[int, str], dict[str, float | None]]:
    rows = conn.execute(
        """
        WITH participant_one AS (
            SELECT
                p.*,
                ROW_NUMBER() OVER (
                    PARTITION BY p.report_id, p.side, p.team_id, p.hero
                    ORDER BY p.id
                ) AS participant_rn
            FROM participants p
        ),
        first_props AS (
            SELECT
                s.*,
                ROW_NUMBER() OVER (
                    PARTITION BY s.report_id, s.hero, s.prop
                    ORDER BY s.event_order, s.id
                ) AS prop_rn
            FROM state_changes s
            WHERE s.change_type = 'property'
              AND s.prop IN ('武力', '智力', '统率', '先攻')
        )
        SELECT
            p.report_id,
            p.hero,
            f.prop,
            CASE f.direction
                WHEN '提升' THEN ROUND(f.result_num - f.value_num, 2)
                WHEN '降低' THEN ROUND(f.result_num + f.value_num, 2)
                WHEN '保持不变' THEN ROUND(f.result_num, 2)
                ELSE NULL
            END AS off_value
        FROM participant_one p
        LEFT JOIN first_props f
          ON f.report_id = p.report_id
         AND f.hero = p.hero
         AND f.prop_rn = 1
        WHERE p.participant_rn = 1
        """
    ).fetchall()
    by_key: dict[tuple[int, str], dict[str, float | None]] = defaultdict(dict)
    for row in rows:
        if row["prop"] in ATTR_PROPS:
            by_key[(int(row["report_id"]), str(row["hero"]))][str(row["prop"])] = as_float(
                row["off_value"]
            )
    return by_key


def fetch_setup_attrs(conn: sqlite3.Connection) -> dict[tuple[int, str], dict[str, float | None]]:
    rows = conn.execute(
        """
        WITH last_round0_props AS (
            SELECT
                s.*,
                ROW_NUMBER() OVER (
                    PARTITION BY s.report_id, s.hero, s.prop
                    ORDER BY s.event_order DESC, s.id DESC
                ) AS prop_rn
            FROM state_changes s
            WHERE s.change_type = 'property'
              AND s.round_no = 0
              AND s.prop IN ('武力', '智力', '统率', '先攻')
        )
        SELECT report_id, hero, prop, result_num
        FROM last_round0_props
        WHERE prop_rn = 1
        """
    ).fetchall()
    by_key: dict[tuple[int, str], dict[str, float | None]] = defaultdict(dict)
    for row in rows:
        by_key[(int(row["report_id"]), str(row["hero"]))][str(row["prop"])] = as_float(
            row["result_num"]
        )
    return by_key


def is_force_type_source(row: sqlite3.Row, off: dict[str, float | None]) -> tuple[bool, str]:
    source = str(row["source"] or "")
    if source in FORCE_TYPE_MANUAL_EXCLUDES:
        return False, FORCE_TYPE_MANUAL_EXCLUDES[source]
    off_force = as_float(off.get("武力"))
    off_int = as_float(off.get("智力"))
    if off_force is None or off_int is None:
        return False, "缺少场外武力或智力"
    if off_force < off_int:
        return False, "场外武力低于场外智力，按非武力型跳过"
    return True, ""


def grade_source_bounds(source_grade: int, *, is_npc: bool) -> tuple[float, float]:
    if is_npc or source_grade <= 0:
        return 1.0, 1.0
    low = 1.0 + max(source_grade - 2, 0) / 100.0
    high = 1.0 + (source_grade + 2) / 100.0
    return low, high


def capped_damage(value: float, target_hp_before: float) -> int:
    return min(round_half_up(value), int(target_hp_before))


def fetch_samples(conn: sqlite3.Connection) -> tuple[list[ForwardSample], Counter[str]]:
    off_attrs = fetch_off_attrs(conn)
    setup_attrs = fetch_setup_attrs(conn)
    hero_country_defaults = fetch_hero_country_defaults(conn)
    negative_statuses = fetch_negative_statuses(conn)
    skip_reasons: Counter[str] = Counter()

    rows = conn.execute(
        """
        WITH participant_one AS (
            SELECT
                p.*,
                ROW_NUMBER() OVER (
                    PARTITION BY p.report_id, p.hero
                    ORDER BY p.id
                ) AS rn
            FROM participants p
        )
        SELECT
            d.*,
            sp.initial_troops AS source_initial,
            sp.unit_type AS source_unit,
            sp.country AS source_country,
            sp.grade AS source_grade,
            CASE WHEN sp.initial_troops = 16000 THEN 1 ELSE 0 END AS source_initial_npc,
            tp.initial_troops AS target_initial,
            tp.unit_type AS target_unit,
            tp.country AS target_country,
            tp.grade AS target_grade,
            CASE WHEN tp.initial_troops = 16000 THEN 1 ELSE 0 END AS target_initial_npc
        FROM damage_with_skill_info d
        LEFT JOIN participant_one sp
          ON sp.report_id = d.report_id
         AND sp.hero = d.source
         AND sp.rn = 1
        LEFT JOIN participant_one tp
          ON tp.report_id = d.report_id
         AND tp.hero = d.target
         AND tp.rn = 1
        WHERE d.damage > 0
          AND (
            d.skill = '普通攻击'
            OR d.knowledge_skill_feature = '兵刃'
          )
        ORDER BY d.report_id, d.event_order, d.id
        """
    ).fetchall()

    samples: list[ForwardSample] = []
    for row in rows:
        skill = str(row["skill"] or "")
        if skill in EXCLUDED_PHYSICAL_SKILLS:
            skip_reasons[f"excluded_skill:{skill}:{EXCLUDED_PHYSICAL_SKILLS[skill]}"] += 1
            continue

        coefficient = skill_multiplier(row)
        if coefficient is None:
            skip_reasons[f"unsupported_skill:{skill}"] += 1
            continue

        off = off_attrs.get((int(row["report_id"]), str(row["source"])), {})
        force_ok, force_reason = is_force_type_source(row, off)
        if not force_ok:
            skip_reasons[f"non_force_source:{row['source']}:{force_reason}"] += 1
            continue

        setup = setup_attrs.get((int(row["report_id"]), str(row["source"])), {})
        setup_force = as_float(setup.get("武力"))
        current_force = as_float(row["source_force"])
        target_command = as_float(row["target_command"])
        if None in (setup_force, current_force, target_command):
            skip_reasons["missing_formula_attr"] += 1
            continue

        source_hp = current_hp(row["source_context_json"], row["source_initial"])
        troop = troop_factor(source_hp)
        target_hp_before = as_float(row["target_hp_before"])
        if target_hp_before is None:
            target_hp_before = current_hp(row["target_context_json"], row["target_initial"])
        if troop is None or target_hp_before is None or target_hp_before <= 0:
            skip_reasons["missing_hp"] += 1
            continue

        source_country, target_country = resolved_countries(row, hero_country_defaults)
        source_bucket, target_bucket, source_country_bonus, target_country_reduction = damage_buckets(
            row,
            source_country,
            target_country,
        )
        unit = unit_counter_multiplier(row["source_unit"], row["target_unit"])
        source_is_npc = bool(row["source_initial_npc"]) or float(source_hp) > 11000
        target_is_npc = bool(row["target_initial_npc"]) or float(target_hp_before) > 11000
        source_grade = parse_grade(row["source_grade"], is_npc=source_is_npc)
        target_grade = parse_grade(row["target_grade"], is_npc=target_is_npc)
        target_grade_mult = 1.0 - target_grade / 100.0
        grade = (1.0 + source_grade / 100.0) * target_grade_mult

        trigger_name, trigger = current_trigger_ratio(
            row["source_context_json"], row["action_context_json"], int(row["event_order"])
        )
        if skill == "纵马横枪":
            trigger_name, trigger = "", 1.0

        special, zongma_bonus, zongma_reason, zongma_coeff_override = special_multiplier(
            row,
            negative_statuses,
        )
        if zongma_coeff_override is not None:
            coefficient = zongma_coeff_override
        pierce = pierce_pct(row)
        command_ignored = ignores_command(row)
        target_eff_command = 0.0 if command_ignored else float(target_command) * (1.0 - pierce / 100.0)

        core = (300.0 + 0.5 * float(setup_force)) * float(current_force) / (
            target_eff_command + B_FIXED
        )
        external_without_grade = coefficient * troop * unit * source_bucket * target_bucket * trigger * special
        pred_raw = core * external_without_grade * grade
        pred_uncapped = round_half_up(pred_raw)
        pred_final = min(pred_uncapped, int(target_hp_before))

        source_grade_low, source_grade_high = grade_source_bounds(
            source_grade, is_npc=source_is_npc
        )
        pred_low_raw = core * external_without_grade * source_grade_low * target_grade_mult
        pred_high_raw = core * external_without_grade * source_grade_high * target_grade_mult
        pred_low = capped_damage(min(pred_low_raw, pred_high_raw), float(target_hp_before))
        pred_high = capped_damage(max(pred_low_raw, pred_high_raw), float(target_hp_before))

        observed = int(row["damage"])
        abs_diff = abs(observed - pred_final)
        rel_error = abs_diff / observed if observed else math.nan
        obs_over_pred = observed / pred_final if pred_final > 0 else math.nan
        target_hp_after = as_float(row["target_hp_after"])
        remain = as_float(row["remain"])
        is_lethal = bool(
            (target_hp_after is not None and target_hp_after <= 0)
            or (remain is not None and remain <= 0)
        )

        samples.append(
            ForwardSample(
                damage_id=int(row["id"]),
                report_id=int(row["report_id"]),
                report_key=str(row["report_key"]),
                event_order=int(row["event_order"]),
                round_no=row["round_no"],
                source=str(row["source"]),
                target=str(row["target"]),
                skill=skill,
                buff=str(row["buff"] or ""),
                sample_type="normal_attack" if skill == "普通攻击" else "physical_skill",
                observed_damage=observed,
                pred_raw=pred_raw,
                pred_int_uncapped=pred_uncapped,
                pred_final=pred_final,
                pred_grade_low=pred_low,
                pred_grade_high=pred_high,
                abs_diff=abs_diff,
                rel_error=rel_error,
                obs_over_pred=obs_over_pred,
                target_hp_before=float(target_hp_before),
                target_hp_after=target_hp_after,
                is_lethal=is_lethal,
                setup_force=float(setup_force),
                current_force=float(current_force),
                target_command=float(target_command),
                pierce_pct=pierce,
                target_eff_command=target_eff_command,
                ignores_command=command_ignored,
                source_hp=float(source_hp),
                skill_multiplier=coefficient,
                troop_multiplier=troop,
                unit_multiplier=unit,
                source_bucket=source_bucket,
                target_bucket=target_bucket,
                source_damage_pct=as_float(row["source_damage_pct"]) or 0.0,
                target_damage_taken_pct=as_float(row["target_damage_taken_pct"]) or 0.0,
                source_country_bonus=source_country_bonus,
                target_country_reduction=target_country_reduction,
                resolved_source_country=source_country,
                resolved_target_country=target_country,
                source_grade=source_grade,
                target_grade=target_grade,
                grade_multiplier=grade,
                trigger_multiplier=trigger,
                special_multiplier=special,
                zongma_negative_bonus=zongma_bonus,
                zongma_negative_reason=zongma_reason,
                total_external_multiplier=external_without_grade * grade,
                current_trigger=trigger_name,
                source_is_npc=source_is_npc,
                target_is_npc=target_is_npc,
                raw_text=str(row["raw_text"] or ""),
            )
        )

    return samples, skip_reasons


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_group(samples: list[ForwardSample]) -> dict[str, float | int]:
    if not samples:
        return {
            "n": 0,
            "exact": 0,
            "within_1": 0,
            "within_grade_band": 0,
            "within_5pct": 0,
            "within_10pct": 0,
            "median_abs_diff": math.nan,
            "median_rel_error": math.nan,
            "mean_rel_error": math.nan,
            "p90_rel_error": math.nan,
            "rmse_log": math.nan,
            "median_obs_over_pred": math.nan,
        }
    log_errors = [
        math.log(sample.observed_damage / sample.pred_final)
        for sample in samples
        if sample.observed_damage > 0 and sample.pred_final > 0
    ]
    rel_errors = [sample.rel_error for sample in samples if math.isfinite(sample.rel_error)]
    return {
        "n": len(samples),
        "exact": sum(sample.observed_damage == sample.pred_final for sample in samples),
        "within_1": sum(sample.abs_diff <= 1 for sample in samples),
        "within_grade_band": sum(
            sample.pred_grade_low <= sample.observed_damage <= sample.pred_grade_high
            for sample in samples
        ),
        "within_5pct": sum(sample.rel_error <= 0.05 for sample in samples),
        "within_10pct": sum(sample.rel_error <= 0.10 for sample in samples),
        "median_abs_diff": median([sample.abs_diff for sample in samples]),
        "median_rel_error": median(rel_errors),
        "mean_rel_error": mean(rel_errors),
        "p90_rel_error": percentile(rel_errors, 0.90),
        "rmse_log": math.sqrt(sum(value * value for value in log_errors) / len(log_errors))
        if log_errors
        else math.nan,
        "median_obs_over_pred": median(
            [sample.obs_over_pred for sample in samples if math.isfinite(sample.obs_over_pred)]
        ),
    }


def summary_rows_by(
    samples: Iterable[ForwardSample],
    key_fn,
) -> list[tuple[tuple[object, ...], dict[str, float | int]]]:
    groups: dict[tuple[object, ...], list[ForwardSample]] = defaultdict(list)
    for sample in samples:
        key = key_fn(sample)
        if not isinstance(key, tuple):
            key = (key,)
        groups[key].append(sample)
    return [
        (key, summarize_group(group))
        for key, group in sorted(groups.items(), key=lambda item: item[0])
    ]


def format_metric_row(label: str, stats: dict[str, float | int]) -> str:
    n = int(stats["n"])
    if not n:
        return f"| {label} | 0 | | | | | | | | | |"
    return (
        f"| {label} | {n} | "
        f"{int(stats['exact'])} ({int(stats['exact']) / n:.1%}) | "
        f"{int(stats['within_1'])} ({int(stats['within_1']) / n:.1%}) | "
        f"{int(stats['within_grade_band'])} ({int(stats['within_grade_band']) / n:.1%}) | "
        f"{int(stats['within_5pct'])} ({int(stats['within_5pct']) / n:.1%}) | "
        f"{int(stats['within_10pct'])} ({int(stats['within_10pct']) / n:.1%}) | "
        f"{stats['median_abs_diff']:.1f} | "
        f"{stats['median_rel_error']:.2%} | "
        f"{stats['p90_rel_error']:.2%} | "
        f"{stats['median_obs_over_pred']:.3f} |"
    )


def export_detail_csv(samples: list[ForwardSample]) -> None:
    OUT_DETAIL_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(ForwardSample.__dataclass_fields__.keys())
    with OUT_DETAIL_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow({name: getattr(sample, name) for name in fieldnames})


def export_summary_md(samples: list[ForwardSample], skip_reasons: Counter[str]) -> None:
    nonlethal = [sample for sample in samples if not sample.is_lethal]
    normal_nonlethal = [sample for sample in nonlethal if sample.sample_type == "normal_attack"]
    skill_nonlethal = [sample for sample in nonlethal if sample.sample_type == "physical_skill"]
    no_zongma = [sample for sample in nonlethal if sample.skill != "纵马横枪"]
    no_zongma_no_mayunlu = [
        sample
        for sample in no_zongma
        if sample.source != "马云禄"
    ]
    high_hp = [sample for sample in nonlethal if sample.source_hp >= TROOP_CAP]
    high_hp_no_zongma = [
        sample
        for sample in high_hp
        if sample.skill != "纵马横枪"
    ]
    zongma_nonlethal = [sample for sample in nonlethal if sample.skill == "纵马横枪"]
    all_stats = summarize_group(samples)
    nonlethal_stats = summarize_group(nonlethal)
    normal_stats = summarize_group(normal_nonlethal)
    no_zongma_stats = summarize_group(no_zongma)
    zongma_stats = summarize_group(zongma_nonlethal)

    lines: list[str] = []
    lines.append("# 武力型普攻/战法正向公式检验")
    lines.append("")
    lines.append("日期：2026-06-23")
    lines.append("")
    lines.append("唯一数据源：`data/sanmou_battles.sqlite`。")
    lines.append("")
    lines.append("## 本轮公式")
    lines.append("")
    lines.append("```text")
    lines.append(
        "pred = (300 + 0.5 × 首回合前武力) × 出手时武力 / (目标有效统率 + 160)"
    )
    lines.append("     × 战法系数 × 兵力因子 × 兵种克制 × 攻方增伤桶 × 目标受伤桶")
    lines.append("     × 当前会心/奇谋倍率 × 特殊乘区 × 品级项")
    lines.append("```")
    lines.append("")
    lines.append(f"兵力因子使用 `F(N)=1 if N>={TROOP_CAP:.0f} else (N/{TROOP_CAP:.0f})^{TROOP_ALPHA:.2f}`。")
    lines.append("最终显示伤害按 `min(round(pred), target_hp_before)` 和战报损兵数比较。")
    lines.append("黄盖按用户修正视为统率型，未纳入武力型样本。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(
        "| 口径 | n | 精确 | ±1 | 落在品级浮动带 | 5%内 | 10%内 | 中位绝对差 | 中位相对误差 | P90相对误差 | 中位obs/pred |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    lines.append(format_metric_row("全部含斩杀封顶", all_stats))
    lines.append(format_metric_row("非斩杀", nonlethal_stats))
    lines.append(format_metric_row("非斩杀-普攻", normal_stats))
    lines.append(format_metric_row("非斩杀-兵刃战法", summarize_group(skill_nonlethal)))
    lines.append(format_metric_row("非斩杀-去纵马横枪", no_zongma_stats))
    lines.append(format_metric_row("非斩杀-去纵马横枪和马云禄", summarize_group(no_zongma_no_mayunlu)))
    lines.append(format_metric_row("非斩杀-高兵力", summarize_group(high_hp)))
    lines.append(format_metric_row("非斩杀-高兵力且去纵马横枪", summarize_group(high_hp_no_zongma)))
    lines.append("")
    lines.append("## 按武将")
    lines.append("")
    lines.append("| 武将 | n | 精确 | ±1 | 品级带 | 5%内 | 10%内 | 中位绝对差 | 中位相对误差 | P90相对误差 | 中位obs/pred |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for (hero,), stats in summary_rows_by(nonlethal, lambda sample: sample.source):
        lines.append(format_metric_row(str(hero), stats))
    lines.append("")
    lines.append("## 按技能")
    lines.append("")
    lines.append("| 技能 | n | 精确 | ±1 | 品级带 | 5%内 | 10%内 | 中位绝对差 | 中位相对误差 | P90相对误差 | 中位obs/pred |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for (skill,), stats in summary_rows_by(nonlethal, lambda sample: sample.skill):
        lines.append(format_metric_row(str(skill), stats))
    lines.append("")
    lines.append("## 跳过项")
    lines.append("")
    for reason, count in skip_reasons.most_common():
        lines.append(f"- {reason}: {count}")
    lines.append("")
    lines.append("## 观察")
    lines.append("")
    normal_10 = int(normal_stats["within_10pct"]) / int(normal_stats["n"])
    no_zongma_10 = int(no_zongma_stats["within_10pct"]) / int(no_zongma_stats["n"])
    zongma_ratio = float(zongma_stats["median_obs_over_pred"])
    lines.append(f"- 普攻闭合最好；非斩杀普攻 {normal_10:.1%} 落在 10% 内。")
    lines.append(f"- 纵马横枪按用户判断采用追伤系数加 20 个百分点：2红从 63.6% 修正为 83.6%，非斩杀中位 obs/pred 约 {zongma_ratio:.3f}。")
    lines.append(f"- 排除纵马横枪后，非斩杀样本 {no_zongma_10:.1%} 落在 10% 内，说明主体公式对吃统率兵刃战法也基本成立。")
    lines.append("- 纵马横枪负面追伤仍逐行判定触发证据：明细中 `zongma_negative_reason` 会标出官方负面状态、《相思文赋》窗口或受到伤害提升状态变化。")
    lines.append("- 三份国家字段缺失的新战报只在分析层做回退：优先用 SQLite 里同武将已确认国家，其次用上下文里的对/受某国武将伤害键；不改数据库。")
    lines.append("- 黄盖按本轮用户口径排除在武力型外；他的统率型普攻/战法需要另行拆，不混入本表。")
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    lines.append(f"- 逐条预测：`{OUT_DETAIL_CSV.as_posix()}`")
    lines.append("")

    with OUT_SUMMARY_MD.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def print_console_summary(samples: list[ForwardSample], skip_reasons: Counter[str]) -> None:
    print(f"SQLite={DB_PATH}")
    print(f"detail_csv={OUT_DETAIL_CSV}")
    print(f"summary_md={OUT_SUMMARY_MD}")
    print(f"troop_factor=cap{TROOP_CAP:.0f}_alpha{TROOP_ALPHA:.2f}")
    for label, group in [
        ("all_capped", samples),
        ("nonlethal", [sample for sample in samples if not sample.is_lethal]),
        (
            "nonlethal_normal",
            [
                sample
                for sample in samples
                if not sample.is_lethal and sample.sample_type == "normal_attack"
            ],
        ),
        (
            "nonlethal_physical_skill",
            [
                sample
                for sample in samples
                if not sample.is_lethal and sample.sample_type == "physical_skill"
            ],
        ),
    ]:
        stats = summarize_group(group)
        n = int(stats["n"])
        if not n:
            continue
        print(
            f"{label}: n={n} exact={stats['exact']} "
            f"within5={stats['within_5pct']} ({stats['within_5pct'] / n:.1%}) "
            f"within10={stats['within_10pct']} ({stats['within_10pct'] / n:.1%}) "
            f"med_abs={stats['median_abs_diff']:.1f} "
            f"med_rel={stats['median_rel_error']:.2%} "
            f"rmse_log={stats['rmse_log']:.4f} "
            f"med_obs_pred={stats['median_obs_over_pred']:.3f}"
        )
    print("skip_reasons:")
    for reason, count in skip_reasons.most_common():
        print(f"  {reason}: {count}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    samples, skip_reasons = fetch_samples(conn)
    if not samples:
        raise SystemExit("no forward samples")
    export_detail_csv(samples)
    export_summary_md(samples, skip_reasons)
    print_console_summary(samples, skip_reasons)


if __name__ == "__main__":
    main()
