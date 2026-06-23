"""Rebuild physical/normal attack Base samples and plot them against force attrs.

Reads only data/sanmou_battles.sqlite.  This script intentionally avoids any
previous derived tables: every point is rebuilt from damage_contexts plus
participant/config knowledge in SQLite.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable


DB_PATH = Path("data/sanmou_battles.sqlite")
OUT_CSV = Path("docs/physical_base_samples_20260623.csv")
OUT_PLOT_CSV = Path("docs/physical_base_plot_samples_hp5000_no_machao_zongma_20260623.csv")
OUT_OFF_FORCE_PNG = Path("docs/physical_base_vs_off_force_hp5000_no_machao_zongma_20260623.png")
OUT_SETUP_FORCE_PNG = Path("docs/physical_base_vs_setup_force_hp5000_no_machao_zongma_20260623.png")
OUT_Z_OFF_FORCE_PNG = Path("docs/physical_z_vs_off_force_hp5000_no_machao_zongma_20260623.png")
OUT_Z_SETUP_FORCE_PNG = Path("docs/physical_z_vs_setup_force_hp5000_no_machao_zongma_20260623.png")

TROOP_CAP = 9000.0
TROOP_ALPHA = 0.38
PLOT_MIN_SOURCE_HP = 5000.0
B_FIXED = 160.0
ATTR_PROPS = ("武力", "智力", "统率", "先攻")

# Include only physical damage whose skill coefficient is currently explicit
# enough to strip without inventing hidden mechanics.
INCLUDED_PHYSICAL_SKILLS = {
    "摧坚克难",
    "纵马横枪",
    "定军扬威",
    "红妆缭乱",
    "万人之敌",
    "辕门射戟",
    "水淹七军",
}

EXCLUDED_PHYSICAL_SKILLS = {
    "七进七出": "同回合龙胆系数递减，需逐段识别",
    "千里突袭": "额外受先攻差和目标前后排影响",
    "骁勇无前": "互相普攻和额外兵刃混合，当前不拆",
}


@dataclass(frozen=True)
class BaseSample:
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
    damage: float
    base_common: float
    z_b160: float
    off_force: float
    setup_force: float
    current_force: float
    off_intelligence: float
    setup_intelligence: float
    current_intelligence: float
    target_command: float
    target_eff_command: float
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
    pierce_pct: float
    grade_multiplier: float
    trigger_multiplier: float
    special_multiplier: float
    total_multiplier: float
    target_has_yijing_static: bool
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


def parse_grade(value: object, *, is_npc: bool = False) -> int:
    if is_npc:
        return 0
    if value is None:
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
    ratio = None
    if ratio_text.endswith("%"):
        ratio = as_float(ratio_text[:-1])
    else:
        ratio = as_float(ratio_text)
    return str(trigger.get("trigger") or ""), (ratio / 100.0 if ratio else 1.0)


def damage_buckets(row: sqlite3.Row) -> tuple[float, float, float, float]:
    source_country_bonus = prop_value(
        row["source_context_json"], f"对{row['target_country']}武将伤害提升"
    )
    target_country_reduction = prop_value(
        row["target_context_json"], f"受到{row['source_country']}武将伤害降低"
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


def is_nonlethal(row: sqlite3.Row) -> bool:
    target_hp_after = as_float(row["target_hp_after"])
    remain = as_float(row["remain"])
    if target_hp_after is not None and target_hp_after <= 0:
        return False
    if remain is not None and remain <= 0:
        return False
    return True


def skill_multiplier(row: sqlite3.Row) -> float | None:
    skill = str(row["skill"] or "")
    if skill == "普通攻击":
        if row["action_type"] == "normal_attack" and row["damage_event_type"] == "damage_raw":
            return 1.0
        return None
    if skill not in INCLUDED_PHYSICAL_SKILLS:
        return None

    # Explicitly keyed because descriptions contain non-damage numbers too.
    if skill == "摧坚克难":
        red = str(row["knowledge_red_level_text"] or "")
        return 1.166 if "2" in red else 1.10
    if skill == "纵马横枪":
        red = str(row["knowledge_red_level_text"] or "")
        return 0.636 if "2" in red else 0.60
    if skill == "定军扬威":
        return 1.80
    if skill == "红妆缭乱":
        return 2.398
    if skill == "万人之敌":
        return 1.40
    if skill == "辕门射戟":
        return 2.20
    if skill == "水淹七军":
        return 2.60
    return None


def special_multiplier(row: sqlite3.Row) -> float:
    multiplier = 1.0
    skill = str(row["skill"] or "")
    action_type = str(row["action_type"] or "")
    target_buffs = active_buffs(row["target_active_buffs_json"])

    if "以静制动-静" in target_buffs:
        # 普攻只按普通攻击伤害剥一层；事件类型不是兵刃标签。
        # 非普攻兵刃/追击行也只先剥一层，待更多证据再细分。
        if skill == "普通攻击" and action_type == "normal_attack":
            multiplier *= 0.65
        elif skill in INCLUDED_PHYSICAL_SKILLS:
            multiplier *= 0.65

    # 已由用户和负面状态知识确认：当前库内马超纵马横枪追伤都触发负面 +20%。
    if skill == "纵马横枪":
        multiplier *= 1.20

    return multiplier


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


def fetch_samples(conn: sqlite3.Connection) -> tuple[list[BaseSample], Counter[str]]:
    off_attrs = fetch_off_attrs(conn)
    setup_attrs = fetch_setup_attrs(conn)
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
            r.report_key,
            sp.initial_troops AS source_initial,
            sp.unit_type AS source_unit,
            sp.country AS source_country,
            sp.grade AS source_grade,
            CASE WHEN sp.initial_troops = 16000 THEN 1 ELSE 0 END AS source_is_npc,
            tp.initial_troops AS target_initial,
            tp.unit_type AS target_unit,
            tp.country AS target_country,
            tp.grade AS target_grade,
            CASE WHEN tp.initial_troops = 16000 THEN 1 ELSE 0 END AS target_is_npc
        FROM damage_with_skill_info d
        JOIN reports r ON r.id = d.report_id
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

    samples: list[BaseSample] = []
    for row in rows:
        skill = str(row["skill"] or "")
        if skill in EXCLUDED_PHYSICAL_SKILLS:
            skip_reasons[f"excluded_{skill}:{EXCLUDED_PHYSICAL_SKILLS[skill]}"] += 1
            continue
        coefficient = skill_multiplier(row)
        if coefficient is None:
            skip_reasons[f"unsupported_skill:{skill}"] += 1
            continue
        if not is_nonlethal(row):
            skip_reasons["lethal_truncation"] += 1
            continue

        off = off_attrs.get((int(row["report_id"]), str(row["source"])), {})
        setup = setup_attrs.get((int(row["report_id"]), str(row["source"])), {})
        off_f = as_float(off.get("武力"))
        off_i = as_float(off.get("智力"))
        setup_f = as_float(setup.get("武力"))
        setup_i = as_float(setup.get("智力"))
        if None in (off_f, off_i, setup_f, setup_i):
            skip_reasons["missing_off_or_setup_attr"] += 1
            continue
        if float(off_f) < float(off_i):
            skip_reasons["not_force_type"] += 1
            continue

        current_f = as_float(row["source_force"])
        current_i = as_float(row["source_intelligence"])
        target_command = as_float(row["target_command"])
        if None in (current_f, current_i, target_command):
            skip_reasons["missing_realtime_attr"] += 1
            continue

        source_hp = current_hp(row["source_context_json"], row["source_initial"])
        troop = troop_factor(source_hp)
        if troop is None:
            skip_reasons["missing_source_hp"] += 1
            continue

        source_bucket, target_bucket, source_country_bonus, target_country_reduction = damage_buckets(row)
        unit = unit_counter_multiplier(row["source_unit"], row["target_unit"])
        source_is_npc = bool(row["source_is_npc"]) or (source_hp is not None and source_hp > 11000)
        target_hp_before = as_float(row["target_hp_before"])
        target_is_npc = bool(row["target_is_npc"]) or (
            target_hp_before is not None and target_hp_before > 11000
        )
        source_grade = parse_grade(row["source_grade"], is_npc=source_is_npc)
        target_grade = parse_grade(row["target_grade"], is_npc=target_is_npc)
        grade = (1.0 + source_grade / 100.0) * (1.0 - target_grade / 100.0)
        trigger_name, trigger = current_trigger_ratio(
            row["source_context_json"], row["action_context_json"], int(row["event_order"])
        )
        special = special_multiplier(row)
        total = coefficient * troop * unit * source_bucket * target_bucket * grade * trigger * special
        if total <= 0:
            skip_reasons["nonpositive_multiplier"] += 1
            continue

        damage = float(row["damage"])
        base_common = damage / total
        pierce = pierce_pct(row)
        target_eff_command = float(target_command) * (1.0 - pierce / 100.0)
        sample_type = "normal_attack" if skill == "普通攻击" else "physical_skill"
        samples.append(
            BaseSample(
                damage_id=int(row["id"]),
                report_id=int(row["report_id"]),
                report_key=str(row["report_key"]),
                event_order=int(row["event_order"]),
                round_no=row["round_no"],
                source=str(row["source"]),
                target=str(row["target"]),
                skill=skill,
                buff=str(row["buff"] or ""),
                sample_type=sample_type,
                damage=damage,
                base_common=base_common,
                z_b160=base_common * (target_eff_command + B_FIXED),
                off_force=float(off_f),
                setup_force=float(setup_f),
                current_force=float(current_f),
                off_intelligence=float(off_i),
                setup_intelligence=float(setup_i),
                current_intelligence=float(current_i),
                target_command=float(target_command),
                target_eff_command=target_eff_command,
                source_hp=float(source_hp or 0.0),
                skill_multiplier=coefficient,
                troop_multiplier=troop,
                unit_multiplier=unit,
                source_bucket=source_bucket,
                target_bucket=target_bucket,
                source_damage_pct=as_float(row["source_damage_pct"]) or 0.0,
                target_damage_taken_pct=as_float(row["target_damage_taken_pct"]) or 0.0,
                source_country_bonus=source_country_bonus,
                target_country_reduction=target_country_reduction,
                pierce_pct=pierce,
                grade_multiplier=grade,
                trigger_multiplier=trigger,
                special_multiplier=special,
                total_multiplier=total,
                target_has_yijing_static="以静制动-静"
                in active_buffs(row["target_active_buffs_json"]),
                current_trigger=trigger_name,
                source_is_npc=source_is_npc,
                target_is_npc=target_is_npc,
                raw_text=str(row["raw_text"] or ""),
            )
        )
    return samples, skip_reasons


def export_csv(samples: list[BaseSample]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "damage_id",
        "report_id",
        "report_key",
        "event_order",
        "round_no",
        "source",
        "target",
        "skill",
        "buff",
        "sample_type",
        "damage",
        "base_common",
        "z_b160",
        "off_force",
        "setup_force",
        "current_force",
        "off_intelligence",
        "setup_intelligence",
        "current_intelligence",
        "target_command",
        "target_eff_command",
        "source_hp",
        "skill_multiplier",
        "troop_multiplier",
        "unit_multiplier",
        "source_bucket",
        "target_bucket",
        "source_damage_pct",
        "target_damage_taken_pct",
        "source_country_bonus",
        "target_country_reduction",
        "pierce_pct",
        "grade_multiplier",
        "trigger_multiplier",
        "special_multiplier",
        "total_multiplier",
        "target_has_yijing_static",
        "current_trigger",
        "source_is_npc",
        "target_is_npc",
        "raw_text",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow({name: getattr(sample, name) for name in fieldnames})


def export_plot_csv(samples: list[BaseSample]) -> None:
    fieldnames = [
        "damage_id",
        "report_key",
        "event_order",
        "source",
        "target",
        "skill",
        "sample_type",
        "damage",
        "base_common",
        "z_b160",
        "off_force",
        "setup_force",
        "current_force",
        "target_command",
        "target_eff_command",
        "source_hp",
        "target_damage_taken_pct",
        "pierce_pct",
        "total_multiplier",
        "current_trigger",
        "raw_text",
    ]
    with OUT_PLOT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow({name: getattr(sample, name) for name in fieldnames})


def filter_plot_samples(samples: list[BaseSample]) -> tuple[list[BaseSample], Counter[str]]:
    filtered: list[BaseSample] = []
    reasons: Counter[str] = Counter()
    for sample in samples:
        if sample.source == "马超" and sample.skill == "纵马横枪":
            reasons["exclude_machao_zongma"] += 1
            continue
        if sample.source_hp < PLOT_MIN_SOURCE_HP:
            reasons["exclude_source_hp_lt_5000"] += 1
            continue
        filtered.append(sample)
    return filtered, reasons


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom <= 0:
        return y_mean, 0.0, math.nan
    b = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
    a = y_mean - b * x_mean
    preds = [a + b * x for x in xs]
    sse = sum((y - p) ** 2 for y, p in zip(ys, preds))
    sst = sum((y - y_mean) ** 2 for y in ys)
    r2 = 1 - sse / sst if sst else math.nan
    return a, b, r2


def plot_scatter(
    samples: list[BaseSample],
    x_attr: str,
    y_attr: str,
    out_path: Path,
    title: str,
    y_label: str,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(10.5, 6.6), dpi=180)
    heroes = sorted({sample.source for sample in samples})
    palette = plt.get_cmap("tab10")
    markers = {"normal_attack": "o", "physical_skill": "^"}

    for index, hero in enumerate(heroes):
        hero_samples = [sample for sample in samples if sample.source == hero]
        color = palette(index % 10)
        for sample_type, marker in markers.items():
            typed = [sample for sample in hero_samples if sample.sample_type == sample_type]
            if not typed:
                continue
            ax.scatter(
                [getattr(sample, x_attr) for sample in typed],
                [getattr(sample, y_attr) for sample in typed],
                s=28 if sample_type == "normal_attack" else 36,
                marker=marker,
                color=color,
                alpha=0.58 if sample_type == "normal_attack" else 0.74,
                edgecolors="#222222",
                linewidths=0.25,
                label=f"{hero}-{('普攻' if sample_type == 'normal_attack' else '兵刃战法')}",
            )

    xs = [getattr(sample, x_attr) for sample in samples]
    ys = [getattr(sample, y_attr) for sample in samples]
    a, b, r2 = _linfit(xs, ys)
    x_min, x_max = min(xs), max(xs)
    ax.plot(
        [x_min, x_max],
        [a + b * x_min, a + b * x_max],
        color="#111111",
        linewidth=1.1,
        linestyle="--",
        label=f"all linear R2={r2:.3f}",
    )

    ax.set_title(title)
    ax.set_xlabel("场外武力" if x_attr == "off_force" else "首回合前武力")
    ax.set_ylabel(y_label)
    ax.grid(True, color="#dddddd", linewidth=0.8, alpha=0.75)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=7.2, frameon=False)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def summarize(
    samples: list[BaseSample],
    plot_samples: list[BaseSample],
    skip_reasons: Counter[str],
    plot_skip_reasons: Counter[str],
) -> None:
    print(f"SQLite={DB_PATH}")
    print("Base_common = damage / skill / troop / unit / source_bucket / target_bucket / grade / trigger / special")
    print(f"Z_B160 = Base_common * (target_eff_command + {B_FIXED:.0f})")
    print(f"export samples={len(samples)} -> {OUT_CSV}")
    print(f"plot samples={len(plot_samples)} -> {OUT_PLOT_CSV}")
    print(
        "plots -> "
        f"{OUT_OFF_FORCE_PNG}, {OUT_SETUP_FORCE_PNG}, "
        f"{OUT_Z_OFF_FORCE_PNG}, {OUT_Z_SETUP_FORCE_PNG}"
    )
    print("\nIncluded rows by hero/skill:")
    for (hero, skill), count in sorted(Counter((s.source, s.skill) for s in plot_samples).items()):
        values = [s.base_common for s in plot_samples if s.source == hero and s.skill == skill]
        print(f"  {hero}\t{skill}\tn={count}\tbase_med={median(values):.1f}")
    print("\nPlot filters:")
    for reason, count in plot_skip_reasons.most_common():
        print(f"  {reason}: {count}")
    print("\nSkipped rows:")
    for reason, count in skip_reasons.most_common():
        print(f"  {reason}: {count}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    samples, skip_reasons = fetch_samples(conn)
    if not samples:
        raise SystemExit("no samples")
    plot_samples, plot_skip_reasons = filter_plot_samples(samples)
    if not plot_samples:
        raise SystemExit("no plot samples")
    export_csv(samples)
    export_plot_csv(plot_samples)
    plot_scatter(
        plot_samples,
        "off_force",
        "base_common",
        OUT_OFF_FORCE_PNG,
        "武力型：还原 Base vs 场外武力（hp>=5000，去马超纵马横枪）",
        "Base_common = damage / stripped multipliers",
    )
    plot_scatter(
        plot_samples,
        "setup_force",
        "base_common",
        OUT_SETUP_FORCE_PNG,
        "武力型：还原 Base vs 首回合前武力（hp>=5000，去马超纵马横枪）",
        "Base_common = damage / stripped multipliers",
    )
    plot_scatter(
        plot_samples,
        "off_force",
        "z_b160",
        OUT_Z_OFF_FORCE_PNG,
        "武力型：Z=Base×(目标有效统率+160) vs 场外武力",
        "Z_B160",
    )
    plot_scatter(
        plot_samples,
        "setup_force",
        "z_b160",
        OUT_Z_SETUP_FORCE_PNG,
        "武力型：Z=Base×(目标有效统率+160) vs 首回合前武力",
        "Z_B160",
    )
    summarize(samples, plot_samples, skip_reasons, plot_skip_reasons)


if __name__ == "__main__":
    main()
