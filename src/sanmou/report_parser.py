#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parse NSLG xLua captured battle strings into a readable Markdown report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sanmou.rules import NPC_INITIAL_TROOPS, NPC_TACTICS, PLAYER_MAX_TROOPS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPTURED = PROJECT_ROOT / "data" / "raw_captures"
REPORTS = PROJECT_ROOT / "reports"

HTML_TAG = re.compile(r"<[^>]+>")
LEADING_FRAGMENT = re.compile(r"^[0-9a-zA-Z_/=>\s]+")
PROGRESSIVE_TAIL = re.compile(r"(?:_[0-9]+>|[A-Za-z_][0-9A-Za-z_=/.\-#%()>]{1,})")
TRAILING_JUNK = re.compile(r"[A-Za-z_][0-9A-Za-z_=/.\-#%()>]{1,}$")
HERO = r"\[([^\[\]]{1,8})\]"
COMMA = r"[,，]"
TEMPLATE_MARKERS = {
    "applyHeroName",
    "changeNum",
    "heroName",
    "provideHeroName",
    "targetHeroName",
    "skillName",
    "buffName",
    "propertyName",
    "propertyValue",
    "resultPropertyValue",
    "changeSoldierNum",
    "resultSoldierNum",
    "damageColor",
    "damageChangePercent",
    "damageType",
    "equipName",
    "fatal%",
    "changeDesc",
    "propName",
    "relationName",
    "resistRate%",
    "ratio%",
    "curNum",
    "moral",
    "soldierNum",
    "teamName",
    "winCampType",
}
ACTION_TYPES = {
    "turn_start",
    "normal_attack",
    "combo",
    "counter",
    "damage",
    "damage_raw",
    "heal",
    "block",
    "death",
    "cannot_act",
}
KNOWN_TRIGGERS = {
    "会心",
    "奇谋",
    "倒戈",
    "攻心",
    "连击",
    "反击",
    "规避",
}
BATTLE_TERMS = {
    "兵力",
    "行动",
    "普通攻击",
    "反击",
    "连击",
    "战法",
    "效果",
    "伤害",
    "治疗",
    "抵御",
    "嘲讽",
    "技穷",
    "断粮",
    "畏惧",
}
TACTIC_NAME = re.compile(r"《[^》]+》(?:手抄|善本)?")
UNIT_TYPES = ("盾兵", "弓兵", "枪兵", "骑兵")
UNIT_TYPE_ALIASES = {
    "盾": "盾兵",
    "盾兵": "盾兵",
    "弓": "弓兵",
    "弓兵": "弓兵",
    "枪": "枪兵",
    "枪兵": "枪兵",
    "骑": "骑兵",
    "骑兵": "骑兵",
}
UNIT_COUNTERS = {
    "盾兵": "弓兵",
    "弓兵": "枪兵",
    "枪兵": "骑兵",
    "骑兵": "盾兵",
}
HERO_UNIT_TYPE_DEFAULTS = {
    "黄月英": "弓兵",
    "黄盖": "枪兵",
    "黄忠": "弓兵",
    "吕布": "骑兵",
    "马超": "骑兵",
    "大乔": "枪兵",
    "马云禄": "骑兵",
}
UNKNOWN_TEAM_ID = "__unknown__"
ROUND_TITLE = re.compile(
    r"^(?:第[零一二三四五六七八九十百0-9]+|[零一二三四五六七八九十百]+)回合$"
)


@dataclass
class ParsedReport:
    raw_count: int
    parsed_count: int
    event_count: int
    dedup_mode: str
    teams: dict[str, list[str]]
    unit_types: dict[str, dict[str, str]]
    initial_troops: dict[str, int]
    max_observed_troops: dict[str, int]
    tactics: dict[str, list[str]]
    sections: list[tuple[str, list[dict]]]


def setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def strip_markup(raw: str) -> str:
    text = HTML_TAG.sub("", raw)
    text = text.replace("</link1>", "").replace("</link2>", "").replace("</link>", "")
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def first_meaningful_fragment(raw: str) -> str:
    text = strip_markup(raw)
    match = re.search(r"\[[^\[\]]{1,8}\]", text)
    if match:
        text = text[match.start():]
    else:
        text = LEADING_FRAGMENT.sub("", text)
    text = strip_progressive_tail(text)
    text = strip_trailing_junk(text)
    text = text.strip()
    return text


def has_template(text: str) -> bool:
    return any(marker in text for marker in TEMPLATE_MARKERS)


def too_messy(text: str) -> bool:
    if has_template(text):
        return True
    if not text or len(text) < 3:
        return True
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    if ascii_letters / max(len(text), 1) > 0.22:
        return True
    return False


def clean_name(name: str) -> str:
    return name.strip().strip("[]")


def valid_name(name: str | None) -> bool:
    if not name:
        return False
    name = clean_name(name)
    if has_template(name):
        return False
    if len(name) > 8:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", name))


def valid_names(*names: str | None) -> bool:
    return all(valid_name(name) for name in names)


def num_int(value: str | None) -> int:
    if not value:
        return 0
    return int(float(value))


def num_int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def normalize_number(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("−", "-")


def strip_trailing_junk(text: str) -> str:
    """Remove xLua progressive-fill tails that contain no Chinese text."""
    tail = TRAILING_JUNK.search(text)
    if not tail:
        return text
    prefix = text[: tail.start()]
    if not prefix or not re.search(r"[\u4e00-\u9fff]", prefix):
        return text
    return prefix.rstrip(" ,，。；;:：")


def strip_progressive_tail(text: str) -> str:
    """Cut a later UI fill residue that starts with ASCII/template fragments."""
    for match in PROGRESSIVE_TAIL.finditer(text):
        prefix = text[: match.start()]
        if re.search(r"[\u4e00-\u9fff]", prefix):
            return prefix.rstrip(" ,，。；;:：")
    return text


def clean_tail(text: str, end: int) -> str:
    tail = text[end:].strip().strip(" ,，。；;:：")
    if tail and (not re.search(r"[\u4e00-\u9fff]", tail) or re.match(r"[)>_0-9A-Za-z=/.\-#%]+", tail)):
        return ""
    return tail


def clean_match(pattern: str, text: str) -> re.Match[str] | None:
    m = re.match(pattern, text)
    if not m:
        return None
    return m if not clean_tail(text, m.end()) else None


def valid_value_pair(value: str, result: str | None) -> bool:
    if not result:
        return True
    return value.endswith("%") == result.endswith("%")


def parse_line(raw: str) -> dict | None:
    text = first_meaningful_fragment(raw)
    if not text or len(text) < 3:
        return None

    if ROUND_TITLE.match(text):
        return {"type": "round_heading", "name": text, "raw": text}

    m = re.match(rf"{HERO}开始行动", text)
    if m and valid_name(m.group(1)):
        return {"type": "turn_start", "hero": clean_name(m.group(1)), "raw": text}

    m = re.match(rf"{HERO}对{HERO}发动普通攻击", text)
    if m and valid_names(m.group(1), m.group(2)):
        return {
            "type": "normal_attack",
            "attacker": clean_name(m.group(1)),
            "target": clean_name(m.group(2)),
            "raw": text,
        }

    m = clean_match(rf"{HERO}进行连击", text)
    if m and valid_name(m.group(1)):
        return {"type": "combo", "hero": clean_name(m.group(1)), "raw": text}

    m = re.match(rf"{HERO}进行反击", text)
    if m and valid_name(m.group(1)):
        return {"type": "counter", "hero": clean_name(m.group(1)), "raw": text}

    m = re.match(rf"{HERO}兵力为0(?:[,，]?\s*无法再战)?", text)
    if m and valid_name(m.group(1)):
        return {"type": "death", "hero": clean_name(m.group(1)), "raw": text}

    if too_messy(text):
        return None

    m = clean_match(
        rf"{HERO}由于{HERO}(?:的)?(?:【(.+?)】)?(?:的)?"
        rf"(?:「(.+?)」效果|【(.+?)】的伤害|伤害)?{COMMA}?\s*"
        rf"损失了兵力{COMMA}?\s*([0-9.]+)(?:\(([0-9]+)\))?",
        text,
    )
    if m and valid_names(m.group(1), m.group(2)):
        target, source, skill, buff, skill2, damage, remain = m.groups()
        return {
            "type": "damage",
            "target": clean_name(target),
            "source": clean_name(source),
            "skill": skill or skill2 or "",
            "buff": buff or "",
            "damage": num_int(damage),
            "remain": num_int_or_none(remain),
            "raw": text,
        }

    m = clean_match(rf"{HERO}损失了兵力{COMMA}?\s*([0-9.]+)(?:\(([0-9]+)\))?", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "damage_raw",
            "target": clean_name(m.group(1)),
            "damage": num_int(m.group(2)),
            "remain": num_int_or_none(m.group(3)),
            "raw": text,
        }

    m = clean_match(rf"{HERO}恢复了兵力{COMMA}?\s*([0-9.]+)(?:\(([0-9]+)\))?", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "heal",
            "hero": clean_name(m.group(1)),
            "heal": num_int(m.group(2)),
            "remain": num_int_or_none(m.group(3)),
            "raw": text,
        }

    m = clean_match(rf"{HERO}消耗([0-9]+)次抵御机会{COMMA}?\s*此次伤害减少([0-9.]+)%", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "block",
            "hero": clean_name(m.group(1)),
            "count": num_int(m.group(2)),
            "reduce": normalize_number(m.group(3)),
            "raw": text,
        }

    m = clean_match(
        rf"{HERO}由于{HERO}(?:【(.+?)】)?(?:的)?(?:「(.+?)」效果)?{HERO}无法行动",
        text,
    )
    if m and valid_names(m.group(1), m.group(2), m.group(5)):
        return {
            "type": "cannot_act",
            "hero": clean_name(m.group(5)),
            "target": clean_name(m.group(1)),
            "source": clean_name(m.group(2)),
            "skill": m.group(3) or "",
            "buff": m.group(4) or "",
            "raw": text,
        }

    m = clean_match(
        rf"{HERO}由于{HERO}(?:【(.+?)】)?(?:的)?「(.+?)」效果治疗效果(?:降低为|降为)([0-9.]+%)",
        text,
    )
    if m and valid_names(m.group(1), m.group(2)):
        return {
            "type": "heal_limit",
            "target": clean_name(m.group(1)),
            "source": clean_name(m.group(2)),
            "skill": m.group(3) or "",
            "buff": m.group(4),
            "value": m.group(5),
            "raw": text,
        }

    m = clean_match(rf"{HERO}由于{HERO}(?:【(.+?)】)?(?:的)?(?:「(.+?)」效果)?", text)
    if m and valid_names(m.group(1), m.group(2)):
        return {
            "type": "effect_source",
            "target": clean_name(m.group(1)),
            "source": clean_name(m.group(2)),
            "skill": m.group(3) or "",
            "buff": m.group(4) or "",
            "raw": text,
        }

    m = clean_match(r"行动顺序判断完毕(?:【判断结果】)?", text)
    if m:
        return {"type": "order_check", "raw": text}

    m = clean_match(rf"{HERO}队当前补给值为([0-9.]+)，造成伤害降低([0-9.]+%?)", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "team_morale",
            "team": clean_name(m.group(1)),
            "moral": m.group(2),
            "damage_down": m.group(3),
            "raw": text,
        }

    m = clean_match(rf"{HERO}队获得【(.+?)】强化效果(?:，(.+))?", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "team_buff",
            "team": clean_name(m.group(1)),
            "buff": m.group(2),
            "detail": m.group(3) or "",
            "raw": text,
        }

    m = clean_match(rf"{HERO}队获得兵种强化效果", text)
    if m and valid_name(m.group(1)):
        return {"type": "team_buff", "team": clean_name(m.group(1)), "buff": "兵种强化", "detail": "", "raw": text}

    m = clean_match(rf"{HERO}队获得建筑科技强化效果", text)
    if m and valid_name(m.group(1)):
        return {"type": "team_buff", "team": clean_name(m.group(1)), "buff": "建筑科技", "detail": "", "raw": text}

    m = clean_match(rf"{HERO}队由于【(.+?)】的效果", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "team_effect",
            "team": clean_name(m.group(1)),
            "effect": m.group(2),
            "raw": text,
        }

    m = clean_match(rf"{HERO}(?:发动|获得)战法【(.+?)】", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "skill_cast" if "发动战法" in text else "skill_gain",
            "hero": clean_name(m.group(1)),
            "skill": m.group(2),
            "raw": text,
        }

    m = clean_match(rf"{HERO}因几率未发动战法【(.+?)】", text)
    if m and valid_name(m.group(1)):
        return {"type": "skill_miss", "hero": clean_name(m.group(1)), "skill": m.group(2), "raw": text}

    m = clean_match(rf"{HERO}因几率未触发【(.+?)】的「(.+?)」效果", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "effect_miss",
            "hero": clean_name(m.group(1)),
            "skill": m.group(2),
            "buff": m.group(3),
            "raw": text,
        }

    m = clean_match(rf"{HERO}战法【(.+?)】无法释放", text)
    if m and valid_name(m.group(1)):
        return {"type": "skill_blocked", "hero": clean_name(m.group(1)), "skill": m.group(2), "raw": text}

    m = clean_match(rf"{HERO}的(?:【(.+?)】|\[(.+?)\])?\s*(提升|降低|保持不变)\s*([-0-9.]+%?)(?:\(([-0-9.]+%?)\))?", text)
    if m and valid_name(m.group(1)):
        hero, prop1, prop2, direction, value, result = m.groups()
        value = normalize_number(value)
        result = normalize_number(result)
        if not valid_value_pair(value, result):
            return None
        return {
            "type": "property",
            "hero": clean_name(hero),
            "prop": prop1 or prop2 or "",
            "direction": direction,
            "value": value,
            "result": result or "",
            "raw": text,
        }

    m = clean_match(rf"{HERO}执行来自(?:【(.+?)】)?的?(?:「(.+?)」)?效果", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_exec",
            "hero": clean_name(m.group(1)),
            "skill": m.group(2) or "",
            "buff": m.group(3) or "",
            "raw": text,
        }

    m = clean_match(rf"{HERO}执行(?:【(.+?)】)?\s*(?:「(.+?)」)?\s*效果", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_exec",
            "hero": clean_name(m.group(1)),
            "skill": m.group(2) or "",
            "buff": m.group(3) or "",
            "raw": text,
        }

    m = clean_match(rf"{HERO}的「(.+?)」(?:已叠加|已满层)\s*([0-9.]+)次", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_stack",
            "hero": clean_name(m.group(1)),
            "buff": m.group(2),
            "count": num_int(m.group(3)),
            "full": "满层" in text,
            "raw": text,
        }

    m = clean_match(rf"{HERO}的「(.+?)」效果已消失", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_expire",
            "hero": clean_name(m.group(1)),
            "buff": m.group(2),
            "raw": text,
        }

    m = clean_match(rf"{HERO}的「(.+?)」效果已刷新", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_refresh",
            "hero": clean_name(m.group(1)),
            "buff": m.group(2),
            "raw": text,
        }

    m = clean_match(rf"{HERO}的「(.+?)」效果已施加", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_apply",
            "hero": clean_name(m.group(1)),
            "buff": m.group(2),
            "raw": text,
        }

    m = clean_match(rf"{HERO}持有(.+?){COMMA}\s*(.+?)暂时失效", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "buff_temporarily_invalid",
            "hero": clean_name(m.group(1)),
            "status": m.group(2),
            "buff": m.group(3),
            "raw": text,
        }

    m = clean_match(rf"{HERO}触发([一-鿿]+)(?:[,，]\s*([一-鿿]+)伤害为([-0-9.]+%))?", text)
    if m and valid_name(m.group(1)):
        trigger = m.group(2)
        if trigger in KNOWN_TRIGGERS:
            return {
                "type": "trigger",
                "hero": clean_name(m.group(1)),
                "trigger": trigger,
                "damage_ratio": normalize_number(m.group(4)),
                "raw": text,
            }

    m = clean_match(rf"{HERO}为{HERO}承担伤害", text)
    if m and valid_names(m.group(1), m.group(2)):
        return {
            "type": "guard",
            "guardian": clean_name(m.group(1)),
            "target": clean_name(m.group(2)),
            "raw": text,
        }

    m = clean_match(rf"{HERO}承担伤害", text)
    if m and valid_name(m.group(1)):
        return {"type": "guard_self", "hero": clean_name(m.group(1)), "raw": text}

    m = clean_match(rf"{HERO}造成伤害(降低|提升)\s*([0-9.]+%)", text)
    if m and valid_name(m.group(1)):
        return {
            "type": "damage_mod",
            "hero": clean_name(m.group(1)),
            "direction": m.group(2),
            "value": m.group(3),
            "raw": text,
        }

    if "胜利" in text or "失败" in text or "平局" in text:
        return {"type": "battle_end", "result": text, "raw": text}

    if re.search(HERO, text) and any(term in text for term in BATTLE_TERMS):
        return {"type": "raw_battle", "raw": text}

    return None


def event_key(ev: dict) -> tuple:
    t = ev["type"]
    if t == "property":
        return (t, ev.get("hero"), ev.get("prop"), ev.get("direction"), ev.get("value"), ev.get("result"))
    if t == "damage":
        return (t, ev.get("target"), ev.get("source"), ev.get("skill"), ev.get("buff"), ev.get("damage"), ev.get("remain"))
    if t == "damage_raw":
        return (t, ev.get("target"), ev.get("damage"), ev.get("remain"))
    if t == "heal":
        return (t, ev.get("hero"), ev.get("heal"), ev.get("remain"))
    if t == "turn_start":
        return (t, ev.get("hero"))
    if t == "normal_attack":
        return (t, ev.get("attacker"), ev.get("target"))
    if t in {
        "buff_exec",
        "buff_stack",
        "buff_expire",
        "buff_apply",
        "buff_refresh",
        "buff_temporarily_invalid",
        "trigger",
        "combo",
        "counter",
        "block",
        "death",
        "cannot_act",
        "guard",
        "damage_mod",
        "effect_source",
        "order_check",
        "team_morale",
        "team_buff",
        "team_effect",
        "round_heading",
        "skill_cast",
        "skill_gain",
        "skill_miss",
        "effect_miss",
        "skill_blocked",
        "guard_self",
    }:
        return tuple((k, v) for k, v in ev.items() if k not in {"raw", "_raw_index"})
    if t == "battle_end":
        return (t, ev.get("result", "")[:80])
    if t == "raw_battle":
        return (t, ev.get("raw", ""))
    return tuple((k, v) for k, v in ev.items() if k not in {"raw", "_raw_index"})


def exact_event_key(ev: dict) -> tuple | None:
    t = ev["type"]
    if t == "damage":
        return (
            t,
            ev.get("target"),
            ev.get("source"),
            ev.get("skill"),
            ev.get("buff"),
            ev.get("damage"),
            ev.get("remain"),
        )
    if t == "damage_raw":
        return (t, ev.get("target"), ev.get("damage"), ev.get("remain"))
    if t == "heal":
        return (t, ev.get("hero"), ev.get("heal"), ev.get("remain"))
    if t == "property":
        return (
            t,
            ev.get("hero"),
            ev.get("prop"),
            ev.get("direction"),
            ev.get("value"),
            ev.get("result"),
        )
    if t == "buff_stack":
        return (t, ev.get("hero"), ev.get("buff"), ev.get("count"), ev.get("full"))
    if t == "buff_expire":
        return (t, ev.get("hero"), ev.get("buff"))
    if t == "buff_refresh":
        return (t, ev.get("hero"), ev.get("buff"))
    if t == "battle_end":
        return (t, ev.get("result", "")[:120])
    if t == "raw_battle":
        return (t, ev.get("raw", ""))
    return None


def completeness_score(ev: dict) -> tuple[int, int]:
    score = 0
    raw = ev.get("raw", "")
    if "resultSoldierNum" not in raw and "propertyValue" not in raw:
        score += 2
    if ev.get("remain"):
        score += 3
    if ev.get("result"):
        score += 2
    return score, len(raw)


def same_event_without_result(prev: dict, cur: dict) -> bool:
    if prev["type"] != cur["type"]:
        return False
    if prev["type"] == "property":
        fields = ("hero", "prop", "direction", "value")
        return all(prev.get(f) == cur.get(f) for f in fields)
    if prev["type"] == "damage":
        fields = ("target", "source", "skill", "buff", "damage")
        return all(prev.get(f) == cur.get(f) for f in fields)
    if prev["type"] == "damage_raw":
        return prev.get("target") == cur.get("target") and prev.get("damage") == cur.get("damage")
    if prev["type"] == "heal":
        return prev.get("hero") == cur.get("hero") and prev.get("heal") == cur.get("heal")
    return False


def is_progressive_fill(prev: dict, cur: dict) -> bool:
    """Detect a UI placeholder being replaced by its final value."""
    if not same_event_without_result(prev, cur):
        return False
    if prev["type"] == "property":
        return bool(cur.get("result")) and not prev.get("result")
    if prev["type"] in {"damage", "damage_raw", "heal"}:
        prev_remain = prev.get("remain")
        cur_remain = cur.get("remain")
        return (prev_remain is None or prev_remain == 0) and cur_remain not in {None, 0}
    return False


def is_progressive_duplicate(prev: dict, cur: dict) -> bool:
    if prev["type"] != cur["type"]:
        return False
    if prev["type"] == "turn_start":
        return prev.get("hero") == cur.get("hero")
    if event_key(prev) == event_key(cur):
        return True
    if is_progressive_fill(prev, cur):
        return True
    return False


def deduplicate(events: Iterable[dict]) -> list[dict]:
    """Collapse only adjacent progressive UI fills.

    xLua repeatedly pushes the same UI text while replacing placeholders and
    while repainting the same line with different colors. Those updates arrive
    as adjacent records. Battle skills, however, can legitimately repeat a few
    records later with the same visible text, so non-adjacent events are kept.
    """
    result: list[dict] = []
    for ev in events:
        if result and is_progressive_duplicate(result[-1], ev):
            if completeness_score(ev) >= completeness_score(result[-1]):
                result[-1] = ev
            continue
        result.append(ev)
    return result


def truncate_after_first_result(events: Iterable[dict]) -> list[dict]:
    result = []
    for ev in events:
        result.append(ev)
        if ev["type"] == "battle_end":
            break
    return result


def extract_teams(raw_lines: Iterable[str]) -> dict[str, list[str]]:
    teams: dict[str, list[str]] = {}
    pair_pat = re.compile(r"link=(-?\d+)_(\d+)[^>]*><u>\[([^\[\]]{1,8})\](?:</u>)?")
    for raw in raw_lines:
        if has_template(raw):
            continue
        for team_id, _hero_id, hero in pair_pat.findall(raw):
            if valid_name(hero):
                clean_hero = clean_name(hero)
                heroes = teams.setdefault(team_id, [])
                if clean_hero not in heroes:
                    heroes.append(clean_hero)
    return dict(teams)


def normalize_unit_type(value: str) -> str | None:
    text = value.strip().replace(" ", "")
    return UNIT_TYPE_ALIASES.get(text)


def collect_report_heroes(report: ParsedReport) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    if len(report.teams) >= 2:
        items = sorted(report.teams.items(), key=lambda item: len(item[1]), reverse=True)[:2]
        for side, (team_id, heroes) in zip(("阵营 A", "阵营 B"), items):
            for hero in heroes:
                key = (team_id, hero)
                if key not in seen:
                    rows.append((side, team_id, hero))
                    seen.add(key)
    else:
        heroes = set()
        for _name, events in report.sections:
            for ev in events:
                for key in ("hero", "target", "source", "attacker", "guardian"):
                    value = ev.get(key)
                    if value:
                        heroes.add(value)
        for hero in sorted(heroes):
            key = (UNKNOWN_TEAM_ID, hero)
            rows.append(("未识别", UNKNOWN_TEAM_ID, hero))
            seen.add(key)
    return rows


def extract_unit_types(raw_lines: Iterable[str]) -> dict[str, dict[str, str]]:
    # Do not infer per-hero unit type from "兵种加成-弓兵/盾兵/枪兵/骑兵" logs.
    # Those lines describe a same-unit-type team bonus being applied, not the
    # individual unit type of every hero receiving the effect.
    return {}


def infer_initial_troops(events: Iterable[dict]) -> dict[str, int]:
    """Infer initial troops only when the battle flow gives a strong signal.

    A 16000 initial troop count is a specific NPC marker. Damage can prove it
    when the first HP observation has damage + remaining troops equal to
    16000; healing can prove it when the first HP observation keeps the hero at
    16000 with zero restored troops.
    """
    inferred: dict[str, int] = {}
    observed_hp: set[str] = set()
    for ev in events:
        event_type = ev.get("type")
        if event_type in {"damage", "damage_raw"}:
            hero = ev.get("target")
            remain = ev.get("remain")
            damage = ev.get("damage")
            if not hero or not isinstance(remain, int):
                continue
            first_observation = hero not in observed_hp
            observed_hp.add(hero)
            if isinstance(damage, int) and remain + damage == NPC_INITIAL_TROOPS:
                if not first_observation:
                    continue
                inferred[hero] = NPC_INITIAL_TROOPS
        elif event_type == "heal":
            hero = ev.get("hero")
            remain = ev.get("remain")
            heal = ev.get("heal")
            if not hero or not isinstance(remain, int):
                continue
            first_observation = hero not in observed_hp
            observed_hp.add(hero)
            if first_observation and heal == 0 and remain == NPC_INITIAL_TROOPS:
                inferred[hero] = NPC_INITIAL_TROOPS
    return inferred


def infer_max_observed_troops(events: Iterable[dict]) -> dict[str, int]:
    observed: dict[str, int] = {}

    def remember(hero: str | None, value: int | None) -> None:
        if not hero or value is None:
            return
        observed[hero] = max(observed.get(hero, 0), value)

    for ev in events:
        event_type = ev.get("type")
        if event_type in {"damage", "damage_raw"}:
            hero = ev.get("target")
            remain = ev.get("remain")
            damage = ev.get("damage")
            remember(hero, remain if isinstance(remain, int) else None)
            if isinstance(remain, int) and isinstance(damage, int):
                remember(hero, remain + damage)
        elif event_type == "heal":
            hero = ev.get("hero")
            remain = ev.get("remain")
            remember(hero, remain if isinstance(remain, int) else None)
    return observed


def is_tactic_name(value: str | None) -> bool:
    return bool(value and TACTIC_NAME.fullmatch(value.strip()))


def infer_tactics(events: Iterable[dict]) -> dict[str, list[str]]:
    tactics: dict[str, list[str]] = {}
    for ev in events:
        if ev.get("type") != "skill_gain":
            continue
        hero = ev.get("hero")
        skill = str(ev.get("skill") or "").strip()
        if not hero or not is_tactic_name(skill):
            continue
        items = tactics.setdefault(hero, [])
        if skill not in items:
            items.append(skill)
    return tactics


def unit_type_for(report: ParsedReport, team_id: str, hero: str) -> str | None:
    if team_id in report.unit_types and hero in report.unit_types[team_id]:
        return report.unit_types[team_id][hero]
    if hero in HERO_UNIT_TYPE_DEFAULTS:
        return HERO_UNIT_TYPE_DEFAULTS[hero]
    hero_rows = [(row_team_id, row_hero) for _side, row_team_id, row_hero in collect_report_heroes(report)]
    is_unique_hero = sum(1 for _row_team_id, row_hero in hero_rows if row_hero == hero) == 1
    if not is_unique_hero:
        return None
    if hero in report.unit_types.get(UNKNOWN_TEAM_ID, {}):
        return report.unit_types[UNKNOWN_TEAM_ID][hero]
    matches = [
        units[hero]
        for current_team_id, units in report.unit_types.items()
        if current_team_id != UNKNOWN_TEAM_ID and hero in units
    ]
    if len(set(matches)) == 1:
        return matches[0]
    return None


def initial_troops_for(report: ParsedReport, team_id: str, hero: str) -> int | None:
    value = report.initial_troops.get(hero)
    if value is None:
        return None
    hero_rows = [(row_team_id, row_hero) for _side, row_team_id, row_hero in collect_report_heroes(report)]
    if sum(1 for _row_team_id, row_hero in hero_rows if row_hero == hero) == 1:
        return value
    return None


def max_observed_troops_for(report: ParsedReport, team_id: str, hero: str) -> int | None:
    value = report.max_observed_troops.get(hero)
    if value is None:
        return None
    hero_rows = [(row_team_id, row_hero) for _side, row_team_id, row_hero in collect_report_heroes(report)]
    if sum(1 for _row_team_id, row_hero in hero_rows if row_hero == hero) == 1:
        return value
    return None


def tactics_for(report: ParsedReport, team_id: str, hero: str) -> list[str]:
    values = report.tactics.get(hero, [])
    if not values:
        return []
    hero_rows = [(row_team_id, row_hero) for _side, row_team_id, row_hero in collect_report_heroes(report)]
    if sum(1 for _row_team_id, row_hero in hero_rows if row_hero == hero) == 1:
        return values
    return []


def tactics_text_for(report: ParsedReport, team_id: str, hero: str) -> str:
    return "、".join(tactics_for(report, team_id, hero))


def is_npc_by_report(report: ParsedReport, team_id: str, hero: str) -> bool:
    initial_troops = initial_troops_for(report, team_id, hero)
    if initial_troops == NPC_INITIAL_TROOPS:
        return True
    max_observed_troops = max_observed_troops_for(report, team_id, hero)
    return max_observed_troops is not None and max_observed_troops > PLAYER_MAX_TROOPS


def missing_unit_type_rows(report: ParsedReport) -> list[tuple[str, str, str]]:
    return [
        (side, team_id, hero)
        for side, team_id, hero in collect_report_heroes(report)
        if not unit_type_for(report, team_id, hero)
    ]


def set_unit_type(report: ParsedReport, team_id: str, hero: str, unit_type: str) -> None:
    report.unit_types.setdefault(team_id, {})[hero] = unit_type


def prompt_for_missing_unit_types(report: ParsedReport) -> None:
    missing = missing_unit_type_rows(report)
    if not missing:
        return
    print("\n以下武将未能从战报中识别兵种，请补充。可输入：盾/弓/枪/骑 或 盾兵/弓兵/枪兵/骑兵。")
    for side, team_id, hero in missing:
        while True:
            answer = input(f"{side} {hero} 的兵种：").strip()
            unit_type = normalize_unit_type(answer)
            if unit_type:
                set_unit_type(report, team_id, hero, unit_type)
                break
            print("无法识别该兵种，请输入 盾、弓、枪、骑 之一。")


def format_counter_result(my_unit: str | None, opponent_units: set[str]) -> str:
    if not my_unit:
        return "待判断"
    if not opponent_units:
        return "无对方兵种，待判断"
    counters = sorted(unit for unit in opponent_units if UNIT_COUNTERS.get(my_unit) == unit)
    countered_by = sorted(unit for unit in opponent_units if UNIT_COUNTERS.get(unit) == my_unit)
    neutral = sorted(opponent_units - set(counters) - set(countered_by))
    parts = []
    if counters:
        parts.append(f"克制{','.join(counters)}")
    if countered_by:
        parts.append(f"被{','.join(countered_by)}克制")
    if neutral:
        parts.append(f"对{','.join(neutral)}无克制")
    return "；".join(parts) if parts else "无克制"


def split_sections(events: list[dict]) -> list[tuple[str, list[dict]]]:
    sections: list[tuple[str, list[dict]]] = []
    current_name = "列队布阵"
    current: list[dict] = []
    round_index = 0
    has_round_headings = any(ev["type"] == "round_heading" for ev in events)

    for ev in events:
        if ev["type"] == "round_heading":
            if current:
                sections.append((current_name, current))
            current_name = ev.get("name", f"第{chinese_round(round_index + 1)}回合")
            current = []
            continue
        if ev["type"] == "order_check" and not has_round_headings and current:
            if current:
                sections.append((current_name, current))
            round_index += 1
            current_name = f"第{chinese_round(round_index)}回合"
            current = [ev]
            continue
        if ev["type"] == "battle_end":
            if current:
                sections.append((current_name, current))
            sections.append(("战斗结果", [ev]))
            current = []
            current_name = "战斗结束后"
            continue
        current.append(ev)

    if current:
        if current_name == "战前准备" and not current:
            pass
        else:
            sections.append((current_name, current))

    return [(name, evs) for name, evs in sections if evs]


def chinese_round(index: int) -> str:
    digits = "零一二三四五六七八九"
    if 0 < index < 10:
        return digits[index]
    if index == 10:
        return "十"
    if 10 < index < 20:
        return "十" + digits[index % 10]
    if index < 100:
        tens, ones = divmod(index, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    return str(index)


def format_raw_event(ev: dict, include_index: bool = True) -> str:
    prefix = f"`#{ev['_raw_index']:05d}` " if include_index and "_raw_index" in ev else ""
    return f"{prefix}{ev.get('raw', '').strip()}"


def fmt_remain(ev: dict) -> str:
    return f"（余 {ev['remain']}）" if ev.get("remain") is not None else ""


def format_event(ev: dict) -> str | None:
    prefix = f"- `#{ev['_raw_index']:05d}` " if "_raw_index" in ev else "- "
    t = ev["type"]
    if t == "order_check":
        return f"{prefix}行动顺序判断完毕"
    if t == "team_morale":
        return f"{prefix}**{ev['team']}** 队当前补给值为 {ev['moral']}，造成伤害降低 {ev['damage_down']}"
    if t == "team_buff":
        detail = f"，{ev['detail']}" if ev.get("detail") else ""
        return f"{prefix}**{ev['team']}** 队获得【{ev['buff']}】强化效果{detail}"
    if t == "team_effect":
        return f"{prefix}**{ev['team']}** 队由于【{ev['effect']}】的效果"
    if t == "round_heading":
        return f"{prefix}{ev['name']}"
    if t == "skill_cast":
        return f"{prefix}**{ev['hero']}** 发动战法【{ev['skill']}】"
    if t == "skill_gain":
        return f"{prefix}**{ev['hero']}** 获得战法【{ev['skill']}】"
    if t == "skill_miss":
        return f"{prefix}**{ev['hero']}** 因几率未发动战法【{ev['skill']}】"
    if t == "effect_miss":
        return f"{prefix}**{ev['hero']}** 因几率未触发【{ev['skill']}】「{ev['buff']}」效果"
    if t == "skill_blocked":
        return f"{prefix}**{ev['hero']}** 战法【{ev['skill']}】无法释放"
    if t == "turn_start":
        return f"{prefix}**{ev['hero']}** 开始行动"
    if t == "normal_attack":
        return f"{prefix}**{ev['attacker']}** 对 **{ev['target']}** 发动普通攻击"
    if t == "combo":
        return f"{prefix}**{ev['hero']}** 进行连击"
    if t == "counter":
        return f"{prefix}**{ev['hero']}** 进行反击"
    if t == "damage":
        cause = ""
        if ev.get("skill"):
            cause += f"【{ev['skill']}】"
        if ev.get("buff"):
            cause += f"「{ev['buff']}」"
        cause = f" 的 {cause}效果" if cause else ""
        return f"{prefix}**{ev['target']}** 因 **{ev['source']}**{cause}，损失兵力 **{ev['damage']}**{fmt_remain(ev)}"
    if t == "damage_raw":
        return f"{prefix}**{ev['target']}** 损失兵力 **{ev['damage']}**{fmt_remain(ev)}"
    if t == "heal":
        return f"{prefix}**{ev['hero']}** 恢复兵力 **{ev['heal']}**{fmt_remain(ev)}"
    if t == "block":
        return f"{prefix}**{ev['hero']}** 消耗 {ev['count']} 次抵御，伤害减免 {ev['reduce']}%"
    if t == "death":
        return f"{prefix}**{ev['hero']}** 兵力为 0，无法再战"
    if t == "cannot_act":
        cause = ""
        if ev.get("skill"):
            cause += f"【{ev['skill']}】"
        if ev.get("buff"):
            cause += f"「{ev['buff']}」"
        cause = f" 的 {cause}效果" if cause else ""
        return f"{prefix}**{ev['hero']}** 由于 **{ev['source']}**{cause}，无法行动"
    if t == "heal_limit":
        cause = f"【{ev['skill']}】" if ev.get("skill") else ""
        if ev.get("buff"):
            cause += f"「{ev['buff']}」"
        return f"{prefix}**{ev['target']}** 由于 **{ev['source']}** 的 {cause}效果，治疗效果降为 {ev['value']}"
    if t == "effect_source":
        cause = f"【{ev['skill']}】" if ev.get("skill") else ""
        if ev.get("buff"):
            cause += f"「{ev['buff']}」"
        return f"{prefix}**{ev['target']}** 由于 **{ev['source']}** 的 {cause}效果"
    if t == "property":
        arrow = {"提升": "↑", "降低": "↓", "保持不变": "→"}.get(ev["direction"], ev["direction"])
        result = f"（{ev['result']}）" if ev.get("result") else ""
        return f"{prefix}**{ev['hero']}** 【{ev['prop']}】{arrow} **{ev['value']}**{result}"
    if t == "buff_exec":
        skill = f"【{ev['skill']}】" if ev.get("skill") else ""
        buff = f"「{ev['buff']}」" if ev.get("buff") else ""
        return f"{prefix}**{ev['hero']}** 执行 {skill}{buff} 效果"
    if t == "buff_stack":
        label = "满层" if ev.get("full") else "叠加"
        return f"{prefix}**{ev['hero']}** 「{ev['buff']}」{label} {ev['count']} 层"
    if t == "buff_expire":
        return f"{prefix}**{ev['hero']}** 「{ev['buff']}」效果消失"
    if t == "buff_refresh":
        return f"{prefix}**{ev['hero']}** 「{ev['buff']}」效果刷新"
    if t == "buff_apply":
        return f"{prefix}**{ev['hero']}** 被施加「{ev['buff']}」"
    if t == "buff_temporarily_invalid":
        return f"{prefix}**{ev['hero']}** 持有{ev['status']}，{ev['buff']}暂时失效"
    if t == "trigger":
        ratio = f"，{ev['trigger']}伤害为 {ev['damage_ratio']}" if ev.get("damage_ratio") else ""
        return f"{prefix}**{ev['hero']}** 触发 **{ev['trigger']}**{ratio}"
    if t == "guard":
        return f"{prefix}**{ev['guardian']}** 为 **{ev['target']}** 承担伤害"
    if t == "guard_self":
        return f"{prefix}**{ev['hero']}** 承担伤害"
    if t == "damage_mod":
        return f"{prefix}**{ev['hero']}** 造成伤害{ev['direction']} **{ev['value']}**"
    if t == "battle_end":
        return f"{prefix}**{ev['result']}**"
    if t == "raw_battle":
        return f"{prefix}{ev.get('raw', '').strip()}"
    return None


def format_markdown(report: ParsedReport) -> str:
    lines = ["# 三国谋定天下战报", ""]
    hero_rows = collect_report_heroes(report)
    lines.append("## 武将红度与金印")
    lines.append("")
    lines.append("| 阵营 | 武将 | 红度 | 金印数 | 自带战法 | 自带战法红度 | 截图可见战法/统计顺序 | 备注 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    if hero_rows:
        for side, _team_id, hero in hero_rows:
            is_npc = is_npc_by_report(report, _team_id, hero)
            redness = "0红" if is_npc else "待手动补充"
            gold_seals = "0印" if is_npc else "待手动补充"
            innate_skill_redness = "0红" if is_npc else "待手动补充"
            max_observed = max_observed_troops_for(report, _team_id, hero)
            note = f"流水识别 NPC；最高兵力 {max_observed}" if is_npc and max_observed else "新战报需人工确认"
            lines.append(
                f"| {side} | {hero} | {redness} | {gold_seals} | 待手动补充 | "
                f"{innate_skill_redness} | 待手动补充 | {note} |"
            )
    else:
        lines.append("| 待补 | 待补 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 | 新战报需人工确认 |")
    lines.append("")

    lines.append("## 对阵")
    if len(report.teams) >= 2:
        items = sorted(report.teams.items(), key=lambda item: len(item[1]), reverse=True)[:2]
        lines.append(f"- **阵营 A**: {', '.join(items[0][1])}")
        lines.append(f"- **阵营 B**: {', '.join(items[1][1])}")
    else:
        heroes = set()
        for _name, events in report.sections:
            for ev in events:
                for key in ("hero", "target", "source", "attacker", "guardian"):
                    value = ev.get(key)
                    if value:
                        heroes.add(value)
        lines.append(f"- **武将**: {', '.join(sorted(heroes)) if heroes else '未识别'}")
    lines.append("")

    lines.append("## 总览补充")
    lines.append("")
    lines.append("| 阵营 | 武将 | 国家 | 等级 | 品级 | 兵种 | 初始兵力 | 自带战法 | 战法顺序与红度 | 韬略 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    if hero_rows:
        for side, _team_id, hero in hero_rows:
            unit_type = unit_type_for(report, _team_id, hero) or "待手动补充"
            initial_troops_value = initial_troops_for(report, _team_id, hero)
            is_npc = is_npc_by_report(report, _team_id, hero)
            initial_troops = str(initial_troops_value or "待手动补充")
            grade = "0" if is_npc else "待手动补充"
            tactics_text = NPC_TACTICS if is_npc else (tactics_text_for(report, _team_id, hero) or "待手动补充")
            lines.append(
                f"| {side} | {hero} | 待手动补充 | 待手动补充 | {grade} | "
                f"{unit_type} | {initial_troops} | 待手动补充 | 待手动补充 | {tactics_text} |"
            )
    else:
        lines.append("| 待补 | 待补 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 | 待手动补充 |")
    lines.append("")

    lines.append("## 战法详情补充")
    lines.append("")
    lines.append("新战报请为本次出现且需要分析的战法补充详情；同一配置可用 `--inherit-config` 沿用上一份。")
    lines.append("")
    lines.append("### 待补战法名")
    lines.append("")
    lines.append("- 战法红度：待手动补充")
    lines.append("- 战法类型：待手动补充")
    lines.append("- 适用兵种：待手动补充")
    lines.append("- 说明：待手动补充")
    lines.append("")

    lines.append("## 兵种克制核对")
    lines.append("")
    lines.append("- 兵种克制关系：盾克弓、弓克枪、枪克骑、骑克盾。")
    lines.append("- 克制目标时伤害独立乘区 `×1.15`；被目标克制时独立乘区 `×0.85`；无克制 `×1.00`。")
    lines.append("- 该增减伤通常不在战报展示的“造成伤害/受到伤害”数值中体现，分析伤害前先补全兵种并判断克制。")
    lines.append("")
    lines.append("| 阵营 | 武将 | 兵种 | 克制/被克制核对 |")
    lines.append("| --- | --- | --- | --- |")
    if hero_rows:
        units_by_side: dict[str, set[str]] = {}
        for side, team_id, hero in hero_rows:
            unit_type = unit_type_for(report, team_id, hero)
            if unit_type:
                units_by_side.setdefault(side, set()).add(unit_type)
        for side, team_id, hero in hero_rows:
            unit_type = unit_type_for(report, team_id, hero)
            opponent_units = set()
            for other_side, units in units_by_side.items():
                if other_side != side:
                    opponent_units.update(units)
            counter_result = format_counter_result(unit_type, opponent_units)
            lines.append(f"| {side} | {hero} | {unit_type or '待手动补充'} | {counter_result} |")
    else:
        lines.append("| 待补 | 待补 | 待手动补充 | 待判断 |")
    lines.append("")

    lines.append("## 解析统计")
    lines.append(f"- 原始记录: {report.raw_count}")
    lines.append(f"- 可解析事件: {report.parsed_count}")
    lines.append(f"- 输出事件: {report.event_count}")
    lines.append(f"- 去重: {report.dedup_mode}")
    lines.append("- 排列方式: 严格按原始抓取顺序")
    lines.append("- 正文格式: 去除游戏富文本标签后保留原始流水")
    lines.append("")

    for name, events in report.sections:
        lines.append(f"## {name}")
        lines.append("")
        for ev in events:
            lines.append(format_raw_event(ev))
            lines.append("")
        lines.append("")

    lines.append("---")
    lines.append(f"*由 sanmou.report_parser 生成 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    return "\n".join(lines)


def parse_capture(path: Path, dedup: str = "local") -> ParsedReport:
    raw_text = path.read_text(encoding="utf-8")
    raw_lines = [part.strip() for part in raw_text.split("---") if part.strip()]
    parsed = []
    for raw_index, raw in enumerate(raw_lines, 1):
        ev = parse_line(raw)
        if ev:
            ev["_raw_index"] = raw_index
            parsed.append(ev)
    if dedup == "local":
        events = deduplicate(parsed)
        dedup_mode = "局部去重"
    elif dedup == "none":
        events = parsed
        dedup_mode = "无"
    else:
        raise ValueError(f"unsupported dedup mode: {dedup}")
    events = truncate_after_first_result(events)
    teams = extract_teams(raw_lines)
    unit_types = extract_unit_types(raw_lines)
    initial_troops = infer_initial_troops(events)
    max_observed_troops = infer_max_observed_troops(events)
    tactics = infer_tactics(events)
    sections = split_sections(events) if events else []
    event_count = sum(len(section_events) for _name, section_events in sections)
    return ParsedReport(
        raw_count=len(raw_lines),
        parsed_count=len(parsed),
        event_count=event_count,
        dedup_mode=dedup_mode,
        teams=teams,
        unit_types=unit_types,
        initial_troops=initial_troops,
        max_observed_troops=max_observed_troops,
        tactics=tactics,
        sections=sections,
    )


def report_json(report: ParsedReport) -> str:
    return json.dumps(
        {
            "raw_count": report.raw_count,
            "parsed_count": report.parsed_count,
            "event_count": report.event_count,
            "dedup_mode": report.dedup_mode,
            "teams": report.teams,
            "unit_types": report.unit_types,
            "initial_troops": report.initial_troops,
            "max_observed_troops": report.max_observed_troops,
            "tactics": report.tactics,
            "sections": [
                {"name": name, "events": [{k: v for k, v in ev.items() if k != "raw"} for ev in events]}
                for name, events in report.sections
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    setup_stdio()
    parser = argparse.ArgumentParser(description="解析 NSLG Frida/xLua 捕获文本并生成 Markdown")
    parser.add_argument("--file", "-f", required=True, help="输入 data/raw_captures/battle_xxx.txt")
    parser.add_argument("--md", "-m", help="输出 Markdown 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--dedup",
        choices=("none", "local"),
        default="local",
        help="去重策略：默认 local 只合并相邻 UI 渐进填充；none 保留每次可解析刷新",
    )
    args = parser.parse_args()

    input_path = Path(args.file)
    if not input_path.exists():
        raise SystemExit(f"找不到输入文件: {input_path}")

    report = parse_capture(input_path, dedup=args.dedup)
    if args.json:
        output = report_json(report)
        if args.md:
            Path(args.md).write_text(output, encoding="utf-8")
            print(f"已保存: {args.md}")
        else:
            print(output)
        return

    if report.event_count == 0:
        print(f"没有识别到可解析战报流水: {input_path}")
        print(f"原始 {report.raw_count} 条，可解析 {report.parsed_count} 条，跳过 Markdown 生成")
        return

    REPORTS.mkdir(exist_ok=True)
    output_path = Path(args.md) if args.md else REPORTS / f"{input_path.stem}.md"
    prompt_for_missing_unit_types(report)
    output_path.write_text(format_markdown(report), encoding="utf-8")
    print(f"已保存: {output_path}")
    print(f"原始 {report.raw_count} 条，可解析 {report.parsed_count} 条，输出 {report.event_count} 条，去重: {report.dedup_mode}")
    print("排列方式: 严格按原始抓取顺序")


if __name__ == "__main__":
    main()
