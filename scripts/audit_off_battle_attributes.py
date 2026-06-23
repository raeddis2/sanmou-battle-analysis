from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sanmou.analysis.off_battle_attributes import (  # noqa: E402
    fetch_off_battle_attribute_audit,
    fetch_off_battle_attributes,
)
from sanmou.db import DEFAULT_DB_PATH, connect  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="审计武将场外属性是否能从 SQLite 准确反推")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 数据库路径")
    parser.add_argument("--sample", type=int, default=12, help="显示样例行数")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with connect(args.db) as conn:
        summary = fetch_off_battle_attribute_audit(conn)
        rows = fetch_off_battle_attributes(conn)

    print(f"Database: {Path(args.db).resolve()}")
    print("Definition: 武将场外属性 = 武将进战斗前的面板武力/智力/统率/先攻")
    print("Derivation: 第0回合首条四维属性变化；提升=result-value，降低=result+value，保持不变=result")
    print(f"participants: {summary['participant_count']}")
    print(f"high_confidence: {summary['high_confidence_count']}")
    print(f"medium_confidence: {summary['medium_confidence_count']}")
    print(f"missing: {summary['missing_count']}")
    print(f"ambiguous_same_hero_name: {summary['ambiguous_same_hero_name_count']}")
    print(f"report_order_risk: {summary['report_order_risk_count']}")

    bad_count = (
        int(summary["medium_confidence_count"] or 0)
        + int(summary["missing_count"] or 0)
        + int(summary["ambiguous_same_hero_name_count"] or 0)
        + int(summary["report_order_risk_count"] or 0)
    )
    if bad_count:
        print("FAIL: 当前数据库存在场外属性反推风险。")
        for row in rows:
            if row.confidence != "high":
                print(
                    "  "
                    f"{row.report_key} {row.side or ''}/{row.team_id or ''} {row.hero}: "
                    f"confidence={row.confidence}"
                )
        return 1

    print("PASS: 当前数据库可高置信度反推所有参战武将的场外属性。")
    if args.sample > 0:
        print("\nSample:")
        for row in rows[: args.sample]:
            npc = "NPC" if row.is_npc else "玩家"
            print(
                f"- {row.report_key} {row.side or ''}/{row.team_id or ''} "
                f"{row.hero}({npc}): "
                f"武力={row.off_battle_force:.2f}, "
                f"智力={row.off_battle_intelligence:.2f}, "
                f"统率={row.off_battle_command:.2f}, "
                f"先攻={row.off_battle_initiative:.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

