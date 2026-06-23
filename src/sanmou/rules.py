"""Project-wide battle data rules."""

from __future__ import annotations

import re

NPC_INITIAL_TROOPS = 16000
PLAYER_MAX_TROOPS = 11000
DEFAULT_HERO_LEVEL = 50
NPC_REDNESS = "0红"
NPC_GRADE = "0"
NPC_GOLD_SEALS = "0印"
NPC_TACTICS = "无韬略"


def progression_count(value: int | str | None) -> int | None:
    """Parse a Sanmou progression count from values like 2, 2印, or 2红."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)\s*(?:印|红)?", text)
    return int(match.group(1)) if match else None


def normalize_gold_seals(value: int | str | None, default: str = "") -> str:
    count = progression_count(value)
    if count is not None:
        return f"{count}印"
    text = "" if value is None else str(value).strip()
    return text or default


def normalize_progression_redness(value: int | str | None, default: str = "") -> str:
    count = progression_count(value)
    if count is not None:
        return f"{count}红"
    text = "" if value is None else str(value).strip()
    return text or default


def derive_grade(
    grade: int | str | None = None,
    gold_seals: int | str | None = None,
    innate_skill_redness: int | str | None = None,
    default: str = "",
) -> str:
    """Derive hero grade from the Sanmou rule: grade = seals = innate skill level."""
    for candidate in (gold_seals, innate_skill_redness, grade):
        count = progression_count(candidate)
        if count is not None:
            return str(count)
    text = "" if grade is None else str(grade).strip()
    return text or default


def is_npc_initial_troops(value: int | str | None) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        return int(text) == NPC_INITIAL_TROOPS
    except ValueError:
        return False


def is_npc_observed_troops(value: int | str | None) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        return int(text) > PLAYER_MAX_TROOPS
    except ValueError:
        return False


def is_npc_row(row: dict) -> bool:
    return is_npc_initial_troops(row.get("initial_troops")) or is_npc_observed_troops(row.get("max_observed_troops"))


def apply_npc_defaults(row: dict) -> dict:
    """Force NPC-only fields when battle data identifies a unit as NPC."""
    if not is_npc_row(row):
        return row
    row["redness"] = NPC_REDNESS
    row["grade"] = NPC_GRADE
    row["gold_seals"] = NPC_GOLD_SEALS
    row["innate_skill_redness"] = NPC_REDNESS
    row["tactics_text"] = NPC_TACTICS
    if row.get("skills_text"):
        row["skills_text"] = force_skill_text_zero_red(str(row["skills_text"]))
    payload = row.get("payload_json")
    if isinstance(payload, dict):
        if is_npc_initial_troops(row.get("initial_troops")):
            payload["npc_rule"] = "initial_troops=16000"
        else:
            payload["npc_rule"] = f"max_observed_troops>{PLAYER_MAX_TROOPS}"
        payload["redness"] = NPC_REDNESS
        payload["grade"] = NPC_GRADE
        payload["gold_seals"] = NPC_GOLD_SEALS
        payload["innate_skill_redness"] = NPC_REDNESS
        payload["tactics_text"] = NPC_TACTICS
        payload.pop("inferred_tactics_text", None)
    return row


def force_skill_text_zero_red(value: str) -> str:
    value = re.sub(r"（\s*\d+红\s*）", "（0红）", value)
    value = re.sub(r"\(\s*\d+红\s*\)", "（0红）", value)
    return value
