"""Compare normal-attack Z against current, off-battle, and setup attributes.

Reads only data/sanmou_battles.sqlite.  The working definition is:

    Z = Base * (target_effective_command + 160)

where Base strips troop factor, unit counter, damage/taken buckets, grade,
current critical trigger, and normal attack skill coefficient (=1).
"""

from __future__ import annotations

import json
import math
import sqlite3
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable


DB_PATH = Path("data/sanmou_battles.sqlite")
PHYSICAL_SCATTER_CSV = Path("docs/z_k_config_vs_off_force_physical_20260622.csv")
PHYSICAL_SCATTER_PNG = Path("docs/z_k_config_vs_off_force_physical_20260622.png")
PHYSICAL_CLEAN_SCATTER_CSV = Path("docs/z_k_config_vs_off_force_physical_clean_hp9000_20260622.csv")
PHYSICAL_CLEAN_SCATTER_PNG = Path("docs/z_k_config_vs_off_force_physical_clean_hp9000_20260622.png")
PHYSICAL_STABLE_SCATTER_CSV = Path(
    "docs/z_k_config_vs_off_force_physical_clean_hp9000_no_yijing_20260622.csv"
)
PHYSICAL_STABLE_SCATTER_PNG = Path(
    "docs/z_k_config_vs_off_force_physical_clean_hp9000_no_yijing_20260622.png"
)
B_FIXED = 160.0
TROOP_CAP = 9000.0
TROOP_ALPHA = 0.38
CLEAN_HP_MIN = 9000.0
ATTR_PROPS = ("武力", "智力", "统率", "先攻")


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
    if not text:
        return ""
    return text[0]


def unit_multiplier(source_unit: str, target_unit: str) -> float:
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    beats = {"盾": "弓", "弓": "枪", "枪": "骑", "骑": "盾"}
    if beats.get(source) == target:
        return 1.15
    if beats.get(target) == source:
        return 0.85
    return 1.0


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


def props(context_json: str | None) -> dict:
    context = safe_json_loads(context_json)
    props_value = context.get("props")
    return props_value if isinstance(props_value, dict) else {}


def prop_value(context_json: str | None, name: str) -> float:
    data = props(context_json).get(name)
    if isinstance(data, dict):
        return as_float(data.get("value")) or 0.0
    return 0.0


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


def damage_buckets(row: sqlite3.Row) -> tuple[float, float, float, float, float]:
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
    return (
        source_bucket,
        target_bucket,
        source_country_bonus,
        target_country_reduction,
        target_taken_pct,
    )


def active_buffs(value: str | None) -> dict:
    loaded = safe_json_loads(value)
    return loaded if isinstance(loaded, dict) else {}


def target_special_multiplier(row: sqlite3.Row) -> float:
    buffs = active_buffs(row["target_active_buffs_json"])
    if "以静制动-静" not in buffs:
        return 1.0
    skill = str(row["skill"] or "")
    action_type = str(row["action_type"] or "")
    # 普攻按“普通攻击伤害”命中以静制动-静，只剥离一层 35% 减伤。
    # damage_raw/damage 是事件类型，不可当作“兵刃伤害”标签叠乘。
    if skill == "普通攻击" and action_type == "normal_attack":
        return 0.65
    return 1.0


def pierce_pct(row: sqlite3.Row) -> float:
    flat = as_float(row["source_pierce_pct"]) or 0.0
    embedded = prop_value(row["source_context_json"], "破甲")
    if flat and embedded and abs(flat - embedded) <= 1e-6:
        return flat
    return max(flat, embedded)


def config_attr_key(
    row: sqlite3.Row,
    off: dict[str, float | None],
    setup: dict[str, float | None],
) -> tuple:
    return (
        row["source"],
        int(row["source_is_npc"] or 0),
        row["source_country"],
        row["source_unit"],
        int(as_float(row["source_initial"]) or 0),
        parse_grade(row["source_grade"], is_npc=bool(row["source_is_npc"])),
        round(off.get("武力") or -1, 2),
        round(off.get("智力") or -1, 2),
        round(off.get("统率") or -1, 2),
        round(off.get("先攻") or -1, 2),
        round(setup.get("武力") or -1, 2),
        round(setup.get("智力") or -1, 2),
        round(setup.get("统率") or -1, 2),
        round(setup.get("先攻") or -1, 2),
    )


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


def fetch_setup_attrs(
    conn: sqlite3.Connection,
) -> dict[tuple[int, str], dict[str, float | None]]:
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


@dataclass(frozen=True)
class Sample:
    damage_id: int
    report_id: int
    report_key: str
    source: str
    target: str
    group: str
    config_key: tuple
    z: float
    base: float
    target_eff_command: float
    current_f: float
    current_i: float
    current_m: float
    off_f: float
    off_i: float
    off_m: float
    setup_f: float
    setup_i: float
    setup_m: float
    source_hp: float
    target_command: float
    multiplier: float
    current_trigger: str
    target_has_yijing_static: bool


def fetch_samples(conn: sqlite3.Connection) -> list[Sample]:
    off_attrs = fetch_off_attrs(conn)
    setup_attrs = fetch_setup_attrs(conn)
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
        FROM damage_contexts d
        JOIN reports r ON r.id = d.report_id
        LEFT JOIN participant_one sp
          ON sp.report_id = d.report_id
         AND sp.hero = d.source
         AND sp.rn = 1
        LEFT JOIN participant_one tp
          ON tp.report_id = d.report_id
         AND tp.hero = d.target
         AND tp.rn = 1
        WHERE d.skill = '普通攻击'
          AND d.action_type = 'normal_attack'
          AND d.damage_event_type = 'damage_raw'
          AND d.damage > 0
        ORDER BY d.report_id, d.event_order, d.id
        """
    ).fetchall()

    samples: list[Sample] = []
    for row in rows:
        if as_float(row["target_hp_after"]) is None or as_float(row["target_hp_after"]) <= 0:
            continue
        hp = current_hp(row["source_context_json"], row["source_initial"])
        troop = troop_factor(hp)
        if troop is None:
            continue
        current_f = as_float(row["source_force"])
        current_i = as_float(row["source_intelligence"])
        target_command = as_float(row["target_command"])
        if current_f is None or current_i is None or target_command is None:
            continue

        source_bucket, target_bucket, *_ = damage_buckets(row)
        if source_bucket <= 0 or target_bucket <= 0:
            continue
        unit = unit_multiplier(row["source_unit"], row["target_unit"])
        source_grade = parse_grade(row["source_grade"], is_npc=bool(row["source_is_npc"]))
        target_grade = parse_grade(row["target_grade"], is_npc=bool(row["target_is_npc"]))
        grade_multiplier = (1.0 + source_grade / 100.0) * (1.0 - target_grade / 100.0)
        trigger, crit_ratio = current_trigger_ratio(
            row["source_context_json"], row["action_context_json"], int(row["event_order"])
        )
        special_target = target_special_multiplier(row)
        multiplier = (
            troop
            * unit
            * source_bucket
            * target_bucket
            * special_target
            * grade_multiplier
            * crit_ratio
        )
        if multiplier <= 0:
            continue

        pierce = pierce_pct(row)
        target_eff_command = target_command * (1.0 - pierce / 100.0)
        base = float(row["damage"]) / multiplier
        z = base * (target_eff_command + B_FIXED)

        off = off_attrs.get((int(row["report_id"]), str(row["source"])), {})
        setup = setup_attrs.get((int(row["report_id"]), str(row["source"])), {})
        off_f, off_i = as_float(off.get("武力")), as_float(off.get("智力"))
        setup_f, setup_i = as_float(setup.get("武力")), as_float(setup.get("智力"))
        if None in (off_f, off_i, setup_f, setup_i):
            continue
        group = "智力型" if float(off_i) >= float(off_f) else "武力型"
        samples.append(
            Sample(
                damage_id=int(row["id"]),
                report_id=int(row["report_id"]),
                report_key=str(row["report_key"]),
                source=str(row["source"]),
                target=str(row["target"]),
                group=group,
                config_key=config_attr_key(row, off, setup),
                z=z,
                base=base,
                target_eff_command=target_eff_command,
                current_f=float(current_f),
                current_i=float(current_i),
                current_m=max(float(current_f), float(current_i)),
                off_f=float(off_f),
                off_i=float(off_i),
                off_m=max(float(off_f), float(off_i)),
                setup_f=float(setup_f),
                setup_i=float(setup_i),
                setup_m=max(float(setup_f), float(setup_i)),
                source_hp=float(hp or 0),
                target_command=target_command,
                multiplier=multiplier,
                current_trigger=trigger,
                target_has_yijing_static="以静制动-静"
                in active_buffs(row["target_active_buffs_json"]),
            )
        )
    return samples


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def rmse_log(actual: list[float], predicted: list[float]) -> float:
    residuals = [math.log(a) - math.log(p) for a, p in zip(actual, predicted) if a > 0 and p > 0]
    return math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else math.nan


def median_abs_log(actual: list[float], predicted: list[float]) -> float:
    residuals = [abs(math.log(a) - math.log(p)) for a, p in zip(actual, predicted) if a > 0 and p > 0]
    return median(residuals) if residuals else math.nan


def fit_origin(samples: list[Sample], attr: str) -> dict[str, float]:
    xs = [getattr(s, attr) for s in samples]
    ys = [s.z for s in samples]
    denom = sum(x * x for x in xs)
    k = sum(x * y for x, y in zip(xs, ys)) / denom if denom > 0 else math.nan
    preds = [k * x for x in xs]
    return {"k": k, "rmse_log": rmse_log(ys, preds), "med_abs_log": median_abs_log(ys, preds)}


def fit_intercept(samples: list[Sample], attr: str) -> dict[str, float]:
    xs = [getattr(s, attr) for s in samples]
    ys = [s.z for s in samples]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    b = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom if denom else 0.0
    a = y_mean - b * x_mean
    preds = [a + b * x for x in xs]
    return {"a": a, "b": b, "rmse_log": rmse_log(ys, preds), "med_abs_log": median_abs_log(ys, preds)}


def fit_power(samples: list[Sample], attr: str) -> dict[str, float]:
    xs = [getattr(s, attr) for s in samples]
    ys = [s.z for s in samples]
    logx = [math.log(x) for x in xs]
    logy = [math.log(y) for y in ys]
    x_mean = sum(logx) / len(logx)
    y_mean = sum(logy) / len(logy)
    denom = sum((x - x_mean) ** 2 for x in logx)
    p = sum((x - x_mean) * (y - y_mean) for x, y in zip(logx, logy)) / denom if denom else 0.0
    logc = y_mean - p * x_mean
    preds = [math.exp(logc) * (x**p) for x in xs]
    return {"c": math.exp(logc), "p": p, "rmse_log": rmse_log(ys, preds), "med_abs_log": median_abs_log(ys, preds)}


def fit_per_config(samples: list[Sample], attr: str) -> dict[str, float]:
    groups: dict[tuple, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.config_key].append(sample)
    preds: list[float] = []
    actual: list[float] = []
    ks: list[float] = []
    used_groups = 0
    for group in groups.values():
        xs = [getattr(s, attr) for s in group]
        ys = [s.z for s in group]
        denom = sum(x * x for x in xs)
        if denom <= 0:
            continue
        k = sum(x * y for x, y in zip(xs, ys)) / denom
        ks.append(k)
        used_groups += 1
        preds.extend(k * x for x in xs)
        actual.extend(ys)
    return {
        "groups": float(used_groups),
        "k_median": median(ks) if ks else math.nan,
        "k_p10": percentile(ks, 0.1),
        "k_p90": percentile(ks, 0.9),
        "rmse_log": rmse_log(actual, preds),
        "med_abs_log": median_abs_log(actual, preds),
    }


def fit_per_hero(samples: list[Sample], attr: str) -> dict[str, float]:
    groups: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.source].append(sample)
    preds: list[float] = []
    actual: list[float] = []
    ks: list[float] = []
    for group in groups.values():
        xs = [getattr(s, attr) for s in group]
        ys = [s.z for s in group]
        denom = sum(x * x for x in xs)
        if denom <= 0:
            continue
        k = sum(x * y for x, y in zip(xs, ys)) / denom
        ks.append(k)
        preds.extend(k * x for x in xs)
        actual.extend(ys)
    return {
        "groups": float(len(ks)),
        "k_median": median(ks) if ks else math.nan,
        "rmse_log": rmse_log(actual, preds),
        "med_abs_log": median_abs_log(actual, preds),
    }


def corr_log(samples: list[Sample], attr: str) -> float:
    xs = [math.log(getattr(s, attr)) for s in samples]
    ys = [math.log(s.z) for s in samples]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    deny = math.sqrt(sum((y - y_mean) ** 2 for y in ys))
    return num / (denx * deny) if denx and deny else math.nan


def summarize_models(samples: list[Sample], title: str) -> None:
    attrs = [
        ("current_f", "当前武力"),
        ("current_i", "当前智力"),
        ("current_m", "当前M=max(F,I)"),
        ("off_f", "入场武力"),
        ("off_i", "入场智力"),
        ("off_m", "入场M"),
        ("setup_f", "首回合前武力"),
        ("setup_i", "首回合前智力"),
        ("setup_m", "首回合前M"),
    ]
    config_count = len({s.config_key for s in samples})
    hero_count = len({s.source for s in samples})
    print(f"\n## {title}")
    print(f"samples={len(samples)} heroes={hero_count} configs={config_count}")
    print("attr\tcorr_log\tglobal_k_rmse\tpower_p\tpower_rmse\tcfgK_rmse\tcfgK_med\tcfgK_p10~p90")
    for attr, label in attrs:
        origin = fit_origin(samples, attr)
        power = fit_power(samples, attr)
        cfg = fit_per_config(samples, attr)
        print(
            f"{label}\t{corr_log(samples, attr):.3f}\t"
            f"{origin['rmse_log']:.4f}\t{power['p']:.3f}\t{power['rmse_log']:.4f}\t"
            f"{cfg['rmse_log']:.4f}\t{cfg['k_median']:.1f}\t"
            f"{cfg['k_p10']:.1f}~{cfg['k_p90']:.1f}"
        )


def summarize_config_ks(samples: list[Sample], attr: str, title: str, limit: int = 80) -> None:
    groups: dict[tuple, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.config_key].append(sample)
    rows = []
    for key, group in groups.items():
        xs = [getattr(s, attr) for s in group]
        ys = [s.z for s in group]
        k = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)
        preds = [k * x for x in xs]
        first = group[0]
        rows.append(
            (
                first.group,
                first.source,
                len(group),
                first.off_f,
                first.off_i,
                first.setup_f,
                first.setup_i,
                k,
                rmse_log(ys, preds),
                min(s.current_f for s in group),
                max(s.current_f for s in group),
                min(s.current_i for s in group),
                max(s.current_i for s in group),
                first.report_key,
            )
        )
    rows.sort(key=lambda r: (r[0], r[1], r[3], r[4], r[7]))
    print(f"\n## {title}")
    print("type\thero\tn\toffF\toffI\tsetupF\tsetupI\tK\tcfg_rmse\tcurF_range\tcurI_range\treport_key")
    for row in rows[:limit]:
        print(
            f"{row[0]}\t{row[1]}\t{row[2]}\t{row[3]:.2f}\t{row[4]:.2f}\t"
            f"{row[5]:.2f}\t{row[6]:.2f}\t{row[7]:.1f}\t{row[8]:.4f}\t"
            f"{row[9]:.1f}-{row[10]:.1f}\t{row[11]:.1f}-{row[12]:.1f}\t{row[13]}"
        )


def summarize_hero(samples: list[Sample]) -> None:
    groups: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.source].append(sample)
    print("\n## hero medians")
    print(
        "type\thero\tn\tconfigs\tZ_med\tZ/curF\tZ/curI\tZ/curM\t"
        "offF\toffI\tsetupF\tsetupI"
    )
    for hero, group in sorted(groups.items(), key=lambda item: (item[1][0].group, item[0])):
        z_med = median([s.z for s in group])
        print(
            f"{group[0].group}\t{hero}\t{len(group)}\t{len({s.config_key for s in group})}\t"
            f"{z_med:.0f}\t{median([s.z/s.current_f for s in group]):.1f}\t"
            f"{median([s.z/s.current_i for s in group]):.1f}\t"
            f"{median([s.z/s.current_m for s in group]):.1f}\t"
            f"{median([s.off_f for s in group]):.1f}\t{median([s.off_i for s in group]):.1f}\t"
            f"{median([s.setup_f for s in group]):.1f}\t{median([s.setup_i for s in group]):.1f}"
        )


def compare_config_k_relation(samples: list[Sample], attr_for_k: str) -> None:
    groups: dict[tuple, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.config_key].append(sample)
    configs = []
    for key, group in groups.items():
        xs = [getattr(s, attr_for_k) for s in group]
        ys = [s.z for s in group]
        k = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)
        first = group[0]
        configs.append(
            {
                "group": first.group,
                "hero": first.source,
                "n": len(group),
                "k": k,
                "off_f": first.off_f,
                "off_i": first.off_i,
                "off_m": first.off_m,
                "setup_f": first.setup_f,
                "setup_i": first.setup_i,
                "setup_m": first.setup_m,
            }
        )

    print(f"\n## config K({attr_for_k}) vs fixed attributes")
    for group_name in ("武力型", "智力型"):
        subset = [row for row in configs if row["group"] == group_name and row["n"] >= 3]
        if len(subset) < 3:
            continue
        print(f"\n### {group_name} configs={len(subset)}")
        for attr, label in [
            ("off_f", "入场武力"),
            ("off_i", "入场智力"),
            ("off_m", "入场M"),
            ("setup_f", "首回合前武力"),
            ("setup_i", "首回合前智力"),
            ("setup_m", "首回合前M"),
        ]:
            xs = [row[attr] for row in subset]
            ys = [row["k"] for row in subset]
            x_mean = sum(xs) / len(xs)
            y_mean = sum(ys) / len(ys)
            denom = sum((x - x_mean) ** 2 for x in xs)
            b = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom if denom else 0.0
            a = y_mean - b * x_mean
            preds = [a + b * x for x in xs]
            rmse = math.sqrt(sum((y - p) ** 2 for y, p in zip(ys, preds)) / len(ys))
            print(f"K ~ {label}: a={a:.2f} b={b:.4f} rmseK={rmse:.2f}")


def fit_group_k(group: list[Sample], attr: str) -> tuple[float, float]:
    xs = [getattr(s, attr) for s in group]
    ys = [s.z for s in group]
    denom = sum(x * x for x in xs)
    if denom <= 0:
        return math.nan, math.nan
    k = sum(x * y for x, y in zip(xs, ys)) / denom
    preds = [k * x for x in xs]
    return k, rmse_log(ys, preds)


def config_k_rows(
    samples: list[Sample],
    attr: str,
    *,
    clean_hp_min: float | None = None,
    exclude_target_yijing_static: bool = False,
) -> list[dict[str, object]]:
    groups: dict[tuple, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.config_key].append(sample)
    rows: list[dict[str, object]] = []
    for group in groups.values():
        fit_group = group
        if clean_hp_min is not None:
            fit_group = [sample for sample in group if sample.source_hp >= clean_hp_min]
        if exclude_target_yijing_static:
            fit_group = [sample for sample in fit_group if not sample.target_has_yijing_static]
        if not fit_group:
            continue
        k, rmse = fit_group_k(fit_group, attr)
        first = group[0]
        rows.append(
            {
                "hero": first.source,
                "n": len(fit_group),
                "n_all": len(group),
                "offF": first.off_f,
                "offI": first.off_i,
                "setupF": first.setup_f,
                "setupI": first.setup_i,
                "K": k,
                "rmse": rmse,
                "hp_min": min(s.source_hp for s in fit_group),
                "hp_max": max(s.source_hp for s in fit_group),
                "target_yijing_rows": sum(1 for s in fit_group if s.target_has_yijing_static),
                "curF_min": min(s.current_f for s in fit_group),
                "curF_max": max(s.current_f for s in fit_group),
                "report": first.report_key,
            }
        )
    rows.sort(key=lambda row: (str(row["hero"]), float(row["offF"]), float(row["K"])))
    return rows


def export_physical_scatter(samples: list[Sample]) -> None:
    physical = [sample for sample in samples if sample.group == "武力型"]
    rows = config_k_rows(physical, "current_f")
    clean_rows = config_k_rows(physical, "current_f", clean_hp_min=CLEAN_HP_MIN)
    stable_rows = config_k_rows(
        physical,
        "current_f",
        clean_hp_min=CLEAN_HP_MIN,
        exclude_target_yijing_static=True,
    )
    PHYSICAL_SCATTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "hero",
        "n",
        "n_all",
        "offF",
        "offI",
        "setupF",
        "setupI",
        "K",
        "rmse",
        "hp_min",
        "hp_max",
        "target_yijing_rows",
        "curF_min",
        "curF_max",
        "report",
    ]
    with PHYSICAL_SCATTER_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with PHYSICAL_CLEAN_SCATTER_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in clean_rows:
            writer.writerow(row)
    with PHYSICAL_STABLE_SCATTER_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in stable_rows:
            writer.writerow(row)

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

    fig, ax = plt.subplots(figsize=(9.5, 6.2), dpi=180)
    heroes = sorted({str(row["hero"]) for row in rows})
    palette = plt.get_cmap("tab10")
    for index, hero in enumerate(heroes):
        hero_rows = [row for row in rows if row["hero"] == hero]
        color = palette(index % 10)
        sizes = [max(38, min(130, float(row["n"]) * 2.2)) for row in hero_rows]
        ax.scatter(
            [float(row["offF"]) for row in hero_rows],
            [float(row["K"]) for row in hero_rows],
            s=sizes,
            color=color,
            alpha=0.82,
            edgecolors="#222222",
            linewidths=0.45,
            label=hero,
        )
        for row in hero_rows:
            if hero == "马云禄" or len(hero_rows) <= 2:
                ax.annotate(
                    str(row["report"]).replace("battle_20260620_", ""),
                    (float(row["offF"]), float(row["K"])),
                    xytext=(5, 5),
                    textcoords="offset points",
                    fontsize=7.5,
                    color="#222222",
                )

    ax.set_title("Physical normal attack config K vs off-battle force")
    ax.set_xlabel("offF / 场外面板武力")
    ax.set_ylabel("K_config = Z / current_force")
    ax.grid(True, color="#dddddd", linewidth=0.8, alpha=0.8)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(PHYSICAL_SCATTER_PNG)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9.5, 6.2), dpi=180)
    clean_heroes = sorted({str(row["hero"]) for row in clean_rows})
    for index, hero in enumerate(clean_heroes):
        hero_rows = [row for row in clean_rows if row["hero"] == hero]
        color = palette(index % 10)
        sizes = [max(42, min(140, float(row["n"]) * 10.0)) for row in hero_rows]
        ax.scatter(
            [float(row["offF"]) for row in hero_rows],
            [float(row["K"]) for row in hero_rows],
            s=sizes,
            color=color,
            alpha=0.88,
            edgecolors="#222222",
            linewidths=0.5,
            label=hero,
        )
        for row in hero_rows:
            if hero == "马云禄" or len(hero_rows) <= 2:
                ax.annotate(
                    str(row["report"]).replace("battle_20260620_", ""),
                    (float(row["offF"]), float(row["K"])),
                    xytext=(5, 5),
                    textcoords="offset points",
                    fontsize=7.5,
                    color="#222222",
                )

    ax.set_title(f"Physical normal attack clean config K, hp >= {CLEAN_HP_MIN:.0f}")
    ax.set_xlabel("offF / 场外面板武力")
    ax.set_ylabel("K_config = Z / current_force")
    ax.grid(True, color="#dddddd", linewidth=0.8, alpha=0.8)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(PHYSICAL_CLEAN_SCATTER_PNG)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9.5, 6.2), dpi=180)
    stable_heroes = sorted({str(row["hero"]) for row in stable_rows})
    for index, hero in enumerate(stable_heroes):
        hero_rows = [row for row in stable_rows if row["hero"] == hero]
        color = palette(index % 10)
        sizes = [max(42, min(140, float(row["n"]) * 10.0)) for row in hero_rows]
        ax.scatter(
            [float(row["offF"]) for row in hero_rows],
            [float(row["K"]) for row in hero_rows],
            s=sizes,
            color=color,
            alpha=0.88,
            edgecolors="#222222",
            linewidths=0.5,
            label=hero,
        )
        for row in hero_rows:
            if hero == "马云禄" or len(hero_rows) <= 2:
                ax.annotate(
                    str(row["report"]).replace("battle_20260620_", ""),
                    (float(row["offF"]), float(row["K"])),
                    xytext=(5, 5),
                    textcoords="offset points",
                    fontsize=7.5,
                    color="#222222",
                )

    ax.set_title(f"Physical normal attack stable K, hp >= {CLEAN_HP_MIN:.0f}, no 以静制动-静")
    ax.set_xlabel("offF / 场外面板武力")
    ax.set_ylabel("K_config = Z / current_force")
    ax.grid(True, color="#dddddd", linewidth=0.8, alpha=0.8)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(PHYSICAL_STABLE_SCATTER_PNG)
    plt.close(fig)
    print(f"exported {PHYSICAL_SCATTER_CSV}")
    print(f"exported {PHYSICAL_SCATTER_PNG}")
    print(f"exported {PHYSICAL_CLEAN_SCATTER_CSV}")
    print(f"exported {PHYSICAL_CLEAN_SCATTER_PNG}")
    print(f"exported {PHYSICAL_STABLE_SCATTER_CSV}")
    print(f"exported {PHYSICAL_STABLE_SCATTER_PNG}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    samples = fetch_samples(conn)
    print(f"SQLite={DB_PATH}")
    print(f"definition: Z = stripped_base * (target_eff_command + {B_FIXED:.0f})")
    print(
        "stripped multipliers: source troop factor, unit counter, source damage bucket, "
        "target taken bucket, country directed damage/reduction, target special reductions, "
        "grade, current trigger crit"
    )
    print(f"normal_attack nonlethal samples={len(samples)}")
    summarize_hero(samples)
    summarize_models(samples, "全部样本")
    for group_name in ("武力型", "智力型"):
        summarize_models([s for s in samples if s.group == group_name], group_name)
    summarize_config_ks([s for s in samples if s.group == "武力型"], "current_f", "武力型 config K using 当前武力")
    summarize_config_ks([s for s in samples if s.group == "智力型"], "current_i", "智力型 config K using 当前智力")
    compare_config_k_relation(samples, "current_f")
    compare_config_k_relation(samples, "current_i")
    compare_config_k_relation(samples, "current_m")
    export_physical_scatter(samples)


if __name__ == "__main__":
    main()
