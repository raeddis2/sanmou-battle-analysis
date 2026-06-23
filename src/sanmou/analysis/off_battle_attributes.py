"""Derive hero off-battle panel attributes from SQLite battle state changes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

OFF_BATTLE_PROPS: Final[tuple[str, ...]] = ("武力", "智力", "统率", "先攻")


@dataclass(frozen=True)
class OffBattleAttributeRow:
    report_id: int
    report_key: str
    side: str | None
    team_id: str | None
    hero: str
    initial_troops: int | None
    is_npc: bool
    off_battle_force: float | None
    off_battle_intelligence: float | None
    off_battle_command: float | None
    off_battle_initiative: float | None
    first_force_event_order: int | None
    first_intelligence_event_order: int | None
    first_command_event_order: int | None
    first_initiative_event_order: int | None
    confidence: str


DERIVED_ATTRIBUTES_SQL: Final[str] = """
WITH participant_one AS (
    SELECT
        p.*,
        ROW_NUMBER() OVER (
            PARTITION BY p.report_id, p.side, p.team_id, p.hero
            ORDER BY p.id
        ) AS participant_rn
    FROM participants p
),
hero_name_duplicates AS (
    SELECT report_id, hero, COUNT(*) AS hero_name_count
    FROM participant_one
    WHERE participant_rn = 1
    GROUP BY report_id, hero
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
),
derived AS (
    SELECT
        p.report_id,
        r.report_key,
        p.side,
        p.team_id,
        p.hero,
        p.initial_troops,
        CASE
            WHEN p.initial_troops = 16000 THEN 1
            ELSE 0
        END AS is_npc,
        f.prop,
        f.event_order,
        f.round_no,
        f.direction,
        f.value_num,
        f.result_num,
        CASE f.direction
            WHEN '提升' THEN ROUND(f.result_num - f.value_num, 2)
            WHEN '降低' THEN ROUND(f.result_num + f.value_num, 2)
            WHEN '保持不变' THEN ROUND(f.result_num, 2)
            ELSE NULL
        END AS off_battle_value,
        h.hero_name_count
    FROM participant_one p
    JOIN reports r ON r.id = p.report_id
    JOIN hero_name_duplicates h
      ON h.report_id = p.report_id
     AND h.hero = p.hero
    LEFT JOIN first_props f
      ON f.report_id = p.report_id
     AND f.hero = p.hero
     AND f.prop_rn = 1
    WHERE p.participant_rn = 1
)
SELECT
    report_id,
    report_key,
    side,
    team_id,
    hero,
    initial_troops,
    is_npc,
    MAX(CASE WHEN prop = '武力' THEN off_battle_value END) AS off_battle_force,
    MAX(CASE WHEN prop = '智力' THEN off_battle_value END) AS off_battle_intelligence,
    MAX(CASE WHEN prop = '统率' THEN off_battle_value END) AS off_battle_command,
    MAX(CASE WHEN prop = '先攻' THEN off_battle_value END) AS off_battle_initiative,
    MAX(CASE WHEN prop = '武力' THEN event_order END) AS first_force_event_order,
    MAX(CASE WHEN prop = '智力' THEN event_order END) AS first_intelligence_event_order,
    MAX(CASE WHEN prop = '统率' THEN event_order END) AS first_command_event_order,
    MAX(CASE WHEN prop = '先攻' THEN event_order END) AS first_initiative_event_order,
    CASE
        WHEN hero_name_count > 1 THEN 'ambiguous_same_hero_name'
        WHEN COUNT(off_battle_value) = 4
         AND SUM(CASE WHEN round_no = 0 THEN 1 ELSE 0 END) = 4
         AND SUM(CASE WHEN direction IN ('提升', '降低', '保持不变') THEN 1 ELSE 0 END) = 4
            THEN 'high'
        WHEN COUNT(off_battle_value) = 4 THEN 'medium'
        ELSE 'missing'
    END AS confidence
FROM derived
GROUP BY report_id, report_key, side, team_id, hero, initial_troops, is_npc, hero_name_count
ORDER BY report_key, side, team_id, hero
"""


AUDIT_SUMMARY_SQL: Final[str] = """
WITH attrs AS (
    SELECT * FROM ({derived_sql})
),
report_first_action AS (
    SELECT report_id, MIN(event_order) AS first_action_order
    FROM events
    WHERE event_type IN (
        'turn_start', 'normal_attack', 'skill_cast', 'damage', 'damage_raw',
        'heal', 'death', 'counter', 'combo', 'cannot_act', 'skill_blocked',
        'skill_miss', 'effect_miss'
    )
    GROUP BY report_id
),
report_last_first_attr AS (
    SELECT
        report_id,
        MAX(max_first_attr_order) AS last_first_attr_order
    FROM (
        SELECT report_id, first_force_event_order AS max_first_attr_order FROM attrs
        UNION ALL SELECT report_id, first_intelligence_event_order FROM attrs
        UNION ALL SELECT report_id, first_command_event_order FROM attrs
        UNION ALL SELECT report_id, first_initiative_event_order FROM attrs
    )
    GROUP BY report_id
)
SELECT
    COUNT(*) AS participant_count,
    SUM(CASE WHEN confidence = 'high' THEN 1 ELSE 0 END) AS high_confidence_count,
    SUM(CASE WHEN confidence = 'medium' THEN 1 ELSE 0 END) AS medium_confidence_count,
    SUM(CASE WHEN confidence = 'missing' THEN 1 ELSE 0 END) AS missing_count,
    SUM(CASE WHEN confidence = 'ambiguous_same_hero_name' THEN 1 ELSE 0 END) AS ambiguous_same_hero_name_count,
    SUM(
        CASE
            WHEN a.first_action_order IS NULL THEN 0
            WHEN l.last_first_attr_order < a.first_action_order THEN 0
            ELSE 1
        END
    ) AS report_order_risk_count
FROM attrs x
LEFT JOIN report_first_action a ON a.report_id = x.report_id
LEFT JOIN report_last_first_attr l ON l.report_id = x.report_id
""".format(derived_sql=DERIVED_ATTRIBUTES_SQL)


def fetch_off_battle_attributes(conn: sqlite3.Connection) -> list[OffBattleAttributeRow]:
    """Return one derived off-battle attribute row per participant."""
    rows = conn.execute(DERIVED_ATTRIBUTES_SQL).fetchall()
    return [
        OffBattleAttributeRow(
            report_id=int(row["report_id"]),
            report_key=str(row["report_key"]),
            side=row["side"],
            team_id=row["team_id"],
            hero=str(row["hero"]),
            initial_troops=row["initial_troops"],
            is_npc=bool(row["is_npc"]),
            off_battle_force=row["off_battle_force"],
            off_battle_intelligence=row["off_battle_intelligence"],
            off_battle_command=row["off_battle_command"],
            off_battle_initiative=row["off_battle_initiative"],
            first_force_event_order=row["first_force_event_order"],
            first_intelligence_event_order=row["first_intelligence_event_order"],
            first_command_event_order=row["first_command_event_order"],
            first_initiative_event_order=row["first_initiative_event_order"],
            confidence=str(row["confidence"]),
        )
        for row in rows
    ]


def fetch_off_battle_attribute_audit(conn: sqlite3.Connection) -> sqlite3.Row:
    """Summarize whether the current database can derive off-battle attributes."""
    return conn.execute(AUDIT_SUMMARY_SQL).fetchone()

