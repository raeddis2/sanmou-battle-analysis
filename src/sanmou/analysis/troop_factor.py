"""Fit troop-count damage factors from SQLite damage contexts."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Literal


VARIABLE_OR_COMPOSITE_SKILLS: frozenset[str] = frozenset(
    {
        "七进七出",  # 龙胆同回合连续触发时伤害系数递减。
        "上兵伐谋",  # 伤害随回合数提升。
        "文治武功",  # 同一战法名下同时存在兵刃/谋略两段，当前字段未拆伤害类型。
        "千里突袭",  # 额外受先攻差和位置影响。
        "骁勇无前",  # 普攻互击与额外伤害混合。
    }
)


@dataclass(frozen=True)
class DamageSample:
    id: int
    report_id: int
    report_key: str
    event_order: int
    round_no: int | None
    source: str
    target: str
    skill: str
    buff: str
    damage: int
    target_hp_after: float | None
    source_hp: float | None
    target_hp_before: float | None
    source_initial: float | None
    target_initial: float | None
    source_unit: str
    target_unit: str
    source_country: str
    target_country: str
    action_type: str
    damage_event_type: str
    action_signature: str
    source_props_signature: str
    target_props_signature: str
    source_buffs_signature: str
    target_buffs_signature: str
    current_trigger: str

    @property
    def stable_key(self) -> tuple[object, ...]:
        """Return a grouping key intended to keep non-troop factors fixed."""
        return (
            self.report_id,
            self.source,
            self.target,
            self.skill,
            self.buff,
            self.action_type,
            self.damage_event_type,
            self.action_signature,
            normalize_unit_type(self.source_unit),
            normalize_unit_type(self.target_unit),
            self.source_country,
            self.target_country,
            self.source_props_signature,
            self.target_props_signature,
            self.source_buffs_signature,
            self.target_buffs_signature,
            self.current_trigger,
        )


@dataclass(frozen=True)
class FitGroupSummary:
    report_key: str
    source: str
    target: str
    skill: str
    count: int
    min_hp: float
    max_hp: float
    min_damage: int
    max_damage: int
    slope: float
    rmse_log: float
    event_orders: tuple[int, ...]


@dataclass(frozen=True)
class FitResult:
    varying: Literal["source", "target"]
    cap: float
    rows: int
    groups: int
    alpha: float
    rmse_log: float
    r2: float | None
    group_summaries: tuple[FitGroupSummary, ...]


@dataclass(frozen=True)
class CandidateEvaluation:
    alpha: float
    rows: int
    groups: int
    rmse_log: float
    median_abs_log: float
    p80_abs_log: float


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


def normalize_unit_type(value: str | None) -> str:
    if not value:
        return ""
    return str(value)[0]


def current_hp(context_json: str | None, initial_troops: object) -> float | None:
    context = safe_json_loads(context_json)
    hp = as_float(context.get("hp"))
    return hp if hp is not None else as_float(initial_troops)


def props_signature(context_json: str | None) -> str:
    context = safe_json_loads(context_json)
    props = context.get("props")
    if not isinstance(props, dict):
        return ""
    parts: list[str] = []
    for prop, data in props.items():
        if isinstance(data, dict):
            parts.append(f"{prop}={data.get('value')}")
    return "|".join(sorted(parts))


def active_buffs_signature(active_buffs_json: str | None) -> str:
    buffs = safe_json_loads(active_buffs_json)
    parts: list[str] = []
    for buff, data in buffs.items():
        if isinstance(data, dict) and not data.get("active", True):
            continue
        if isinstance(data, dict):
            parts.append(f"{buff}:{data.get('count')}:{data.get('full')}")
        else:
            parts.append(str(buff))
    return "|".join(sorted(parts))


def action_signature(action_context_json: str | None) -> str:
    action = safe_json_loads(action_context_json)
    return "|".join(
        str(action.get(key) or "")
        for key in ("type", "skill", "buff", "target")
    )


def current_trigger(
    source_context_json: str | None,
    action_context_json: str | None,
    damage_event_order: int,
) -> str:
    """Return only a trigger that happened during the current action.

    The replay context can still remember an older trigger.  For troop-factor
    fitting that stale value must not mark later non-critical damage as critical.
    """
    source_context = safe_json_loads(source_context_json)
    trigger = source_context.get("recent_trigger")
    if not isinstance(trigger, dict):
        return ""
    action = safe_json_loads(action_context_json)
    trigger_order = as_float(trigger.get("event_order"))
    action_order = as_float(action.get("event_order"))
    if trigger_order is None or action_order is None:
        return ""
    if action_order <= trigger_order < damage_event_order:
        return str(trigger.get("trigger") or "")
    return ""


def fetch_damage_samples(conn: sqlite3.Connection) -> list[DamageSample]:
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
            d.id,
            d.report_id,
            r.report_key,
            d.event_order,
            d.round_no,
            d.source,
            d.target,
            d.skill,
            d.buff,
            d.damage,
            d.target_hp_before,
            d.target_hp_after,
            d.source_context_json,
            d.target_context_json,
            d.source_active_buffs_json,
            d.target_active_buffs_json,
            d.action_context_json,
            d.action_type,
            d.damage_event_type,
            sp.initial_troops AS source_initial,
            tp.initial_troops AS target_initial,
            sp.unit_type AS source_unit,
            tp.unit_type AS target_unit,
            sp.country AS source_country,
            tp.country AS target_country
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
        WHERE d.damage > 0
        ORDER BY d.report_id, d.event_order, d.id
        """
    ).fetchall()

    samples: list[DamageSample] = []
    for row in rows:
        source_hp = current_hp(row["source_context_json"], row["source_initial"])
        target_hp_before = as_float(row["target_hp_before"])
        if target_hp_before is None:
            target_hp_before = current_hp(row["target_context_json"], row["target_initial"])
        event_order = int(row["event_order"])
        samples.append(
            DamageSample(
                id=int(row["id"]),
                report_id=int(row["report_id"]),
                report_key=str(row["report_key"]),
                event_order=event_order,
                round_no=row["round_no"],
                source=str(row["source"] or ""),
                target=str(row["target"] or ""),
                skill=str(row["skill"] or ""),
                buff=str(row["buff"] or ""),
                damage=int(row["damage"]),
                target_hp_after=as_float(row["target_hp_after"]),
                source_hp=source_hp,
                target_hp_before=target_hp_before,
                source_initial=as_float(row["source_initial"]),
                target_initial=as_float(row["target_initial"]),
                source_unit=str(row["source_unit"] or ""),
                target_unit=str(row["target_unit"] or ""),
                source_country=str(row["source_country"] or ""),
                target_country=str(row["target_country"] or ""),
                action_type=str(row["action_type"] or ""),
                damage_event_type=str(row["damage_event_type"] or ""),
                action_signature=action_signature(row["action_context_json"]),
                source_props_signature=props_signature(row["source_context_json"]),
                target_props_signature=props_signature(row["target_context_json"]),
                source_buffs_signature=active_buffs_signature(
                    row["source_active_buffs_json"]
                ),
                target_buffs_signature=active_buffs_signature(
                    row["target_active_buffs_json"]
                ),
                current_trigger=current_trigger(
                    row["source_context_json"],
                    row["action_context_json"],
                    event_order,
                ),
            )
        )
    return samples


def is_base_clean_sample(
    sample: DamageSample,
    exclude_variable_or_composite_skills: bool = True,
) -> bool:
    if sample.damage <= 0:
        return False
    if sample.source_hp is None or sample.source_hp <= 0:
        return False
    if sample.target_hp_before is None or sample.target_hp_before <= 0:
        return False
    if sample.target_hp_after is None or sample.target_hp_after <= 0:
        return False
    if sample.current_trigger:
        return False
    if (
        exclude_variable_or_composite_skills
        and sample.skill in VARIABLE_OR_COMPOSITE_SKILLS
    ):
        return False
    return True


def troop_transform(hp: float, cap: float) -> float:
    """Log transform for F(N)=1 above cap, (N/cap)^alpha below cap."""
    if hp >= cap:
        return 0.0
    return math.log(hp / cap)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _percentile(values: list[float], q: float) -> float:
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


def _group_slope(
    samples: list[DamageSample],
    varying: Literal["source", "target"],
    cap: float,
) -> tuple[float, float] | None:
    hp_values = [
        sample.source_hp if varying == "source" else sample.target_hp_before
        for sample in samples
    ]
    if any(value is None for value in hp_values):
        return None
    x_values = [troop_transform(float(value), cap) for value in hp_values]
    y_values = [math.log(sample.damage) for sample in samples]
    x_mean = _mean(x_values)
    y_mean = _mean(y_values)
    x_centered = [x - x_mean for x in x_values]
    y_centered = [y - y_mean for y in y_values]
    denominator = sum(x * x for x in x_centered)
    if denominator <= 1e-12:
        return None
    slope = sum(x * y for x, y in zip(x_centered, y_centered)) / denominator
    residuals = [
        y - slope * x for x, y in zip(x_centered, y_centered)
    ]
    rmse = math.sqrt(sum(value * value for value in residuals) / len(residuals))
    return slope, rmse


def grouped_samples(
    samples: Iterable[DamageSample],
) -> dict[tuple[object, ...], list[DamageSample]]:
    groups: dict[tuple[object, ...], list[DamageSample]] = defaultdict(list)
    for sample in samples:
        groups[sample.stable_key].append(sample)
    return groups


def fit_troop_factor(
    samples: Iterable[DamageSample],
    *,
    cap: float = 9000,
    varying: Literal["source", "target"] = "source",
    exclude_variable_or_composite_skills: bool = True,
    require_other_at_or_above_cap: bool = True,
    skill: str | None = None,
) -> FitResult | None:
    clean = [
        sample
        for sample in samples
        if is_base_clean_sample(sample, exclude_variable_or_composite_skills)
        and (skill is None or sample.skill == skill)
    ]

    x_values: list[float] = []
    y_values: list[float] = []
    summaries: list[FitGroupSummary] = []

    for group in grouped_samples(clean).values():
        if require_other_at_or_above_cap:
            if varying == "source":
                group = [
                    sample
                    for sample in group
                    if sample.target_hp_before is not None
                    and sample.target_hp_before >= cap
                ]
            else:
                group = [
                    sample
                    for sample in group
                    if sample.source_hp is not None and sample.source_hp >= cap
                ]
        if len(group) < 2:
            continue

        hp_values = [
            sample.source_hp if varying == "source" else sample.target_hp_before
            for sample in group
        ]
        if any(value is None for value in hp_values):
            continue
        transformed = [troop_transform(float(value), cap) for value in hp_values]
        if max(transformed) - min(transformed) <= 1e-12:
            continue
        log_damage = [math.log(sample.damage) for sample in group]
        x_mean = _mean(transformed)
        y_mean = _mean(log_damage)
        x_centered = [x - x_mean for x in transformed]
        y_centered = [y - y_mean for y in log_damage]
        x_values.extend(x_centered)
        y_values.extend(y_centered)

        group_slope = _group_slope(group, varying, cap)
        if group_slope:
            hp_numeric = [float(value) for value in hp_values if value is not None]
            damages = [sample.damage for sample in group]
            summaries.append(
                FitGroupSummary(
                    report_key=group[0].report_key,
                    source=group[0].source,
                    target=group[0].target,
                    skill=group[0].skill,
                    count=len(group),
                    min_hp=min(hp_numeric),
                    max_hp=max(hp_numeric),
                    min_damage=min(damages),
                    max_damage=max(damages),
                    slope=group_slope[0],
                    rmse_log=group_slope[1],
                    event_orders=tuple(sample.event_order for sample in group),
                )
            )

    denominator = sum(x * x for x in x_values)
    if not x_values or denominator <= 1e-12:
        return None
    alpha = sum(x * y for x, y in zip(x_values, y_values)) / denominator
    residuals = [y - alpha * x for x, y in zip(x_values, y_values)]
    sse = sum(value * value for value in residuals)
    y_mean = _mean(y_values)
    sst = sum((value - y_mean) ** 2 for value in y_values)
    return FitResult(
        varying=varying,
        cap=cap,
        rows=len(x_values),
        groups=len(summaries),
        alpha=alpha,
        rmse_log=math.sqrt(sse / len(residuals)),
        r2=(1 - sse / sst) if sst else None,
        group_summaries=tuple(
            sorted(
                summaries,
                key=lambda item: (
                    item.skill,
                    item.report_key,
                    item.source,
                    item.target,
                    item.event_orders,
                ),
            )
        ),
    )


def evaluate_candidate_alphas(
    samples: Iterable[DamageSample],
    candidate_alphas: Iterable[float],
    *,
    cap: float = 9000,
    varying: Literal["source", "target"] = "source",
    exclude_variable_or_composite_skills: bool = True,
    require_other_at_or_above_cap: bool = True,
    skill: str | None = None,
) -> list[CandidateEvaluation]:
    clean = [
        sample
        for sample in samples
        if is_base_clean_sample(sample, exclude_variable_or_composite_skills)
        and (skill is None or sample.skill == skill)
    ]
    prepared_groups: list[tuple[list[float], list[float]]] = []

    for group in grouped_samples(clean).values():
        if require_other_at_or_above_cap:
            if varying == "source":
                group = [
                    sample
                    for sample in group
                    if sample.target_hp_before is not None
                    and sample.target_hp_before >= cap
                ]
            else:
                group = [
                    sample
                    for sample in group
                    if sample.source_hp is not None and sample.source_hp >= cap
                ]
        if len(group) < 2:
            continue
        hp_values = [
            sample.source_hp if varying == "source" else sample.target_hp_before
            for sample in group
        ]
        if any(value is None for value in hp_values):
            continue
        transformed = [troop_transform(float(value), cap) for value in hp_values]
        if max(transformed) - min(transformed) <= 1e-12:
            continue
        log_damage = [math.log(sample.damage) for sample in group]
        x_mean = _mean(transformed)
        y_mean = _mean(log_damage)
        prepared_groups.append(
            (
                [x - x_mean for x in transformed],
                [y - y_mean for y in log_damage],
            )
        )

    evaluations: list[CandidateEvaluation] = []
    for alpha in candidate_alphas:
        residuals: list[float] = []
        for x_centered, y_centered in prepared_groups:
            residuals.extend(
                y - alpha * x for x, y in zip(x_centered, y_centered)
            )
        if not residuals:
            continue
        abs_residuals = [abs(value) for value in residuals]
        evaluations.append(
            CandidateEvaluation(
                alpha=float(alpha),
                rows=len(residuals),
                groups=len(prepared_groups),
                rmse_log=math.sqrt(
                    sum(value * value for value in residuals) / len(residuals)
                ),
                median_abs_log=median(abs_residuals),
                p80_abs_log=_percentile(abs_residuals, 0.8),
            )
        )
    return evaluations


def count_clean_samples(
    samples: Iterable[DamageSample],
    *,
    exclude_variable_or_composite_skills: bool = True,
) -> int:
    return sum(
        1
        for sample in samples
        if is_base_clean_sample(sample, exclude_variable_or_composite_skills)
    )
