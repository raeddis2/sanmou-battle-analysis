from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sanmou.analysis.troop_factor import (  # noqa: E402
    count_clean_samples,
    evaluate_candidate_alphas,
    fetch_damage_samples,
    fit_troop_factor,
)
from sanmou.db import DEFAULT_DB_PATH, connect  # noqa: E402


DEFAULT_CANDIDATES = (0.30, 1 / 3, 0.35, 0.38, 0.40, 0.45, 0.50, 0.5441)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 SQLite 战报拟合兵力因子")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 数据库路径")
    parser.add_argument("--cap", type=float, default=9000, help="无衰减兵力阈值")
    parser.add_argument("--sample-groups", type=int, default=12, help="显示重复组样例数量")
    parser.add_argument("--skill", help="只拟合某个战法/普攻")
    parser.add_argument(
        "--include-variable-skills",
        action="store_true",
        help="包含七进七出、上兵伐谋等内含变化倍率的样本",
    )
    parser.add_argument(
        "--fit-target",
        action="store_true",
        help="同时尝试拟合防守方当前兵力因子",
    )
    return parser


def print_fit(result, *, sample_groups: int) -> None:
    if result is None:
        print("fit: no usable repeated-state groups")
        return
    r2_text = f"{result.r2:.6f}" if result.r2 is not None else "NA"
    print(
        "fit: "
        f"varying={result.varying}, cap={result.cap:g}, rows={result.rows}, "
        f"groups={result.groups}, alpha={result.alpha:.6f}, "
        f"rmse_log={result.rmse_log:.6f}, "
        f"r2={r2_text}"
    )
    if sample_groups <= 0:
        return
    print("groups:")
    for group in result.group_summaries[:sample_groups]:
        orders = ",".join(str(value) for value in group.event_orders)
        print(
            "  "
            f"{group.report_key} {group.source}->{group.target} {group.skill}: "
            f"n={group.count}, slope={group.slope:.3f}, "
            f"hp={group.min_hp:.0f}-{group.max_hp:.0f}, "
            f"damage={group.min_damage}-{group.max_damage}, "
            f"orders={orders}"
        )


def main() -> int:
    args = build_parser().parse_args()
    exclude_variable = not args.include_variable_skills
    with connect(args.db) as conn:
        samples = fetch_damage_samples(conn)

    print(f"Database: {Path(args.db).resolve()}")
    print(f"damage_samples: {len(samples)}")
    print(f"clean_samples: {count_clean_samples(samples, exclude_variable_or_composite_skills=exclude_variable)}")
    print(
        "clean filter: nonlethal, source/target hp known, no current critical/strategy trigger"
    )
    if exclude_variable:
        print("excluded variable/composite skills: 七进七出、上兵伐谋、文治武功、千里突袭、骁勇无前")
    print(
        "model: F(N)=1 when N>=cap, otherwise F(N)=(N/cap)^alpha; "
        "stable groups keep source/target/skill/buffs/properties fixed"
    )
    print()

    print("Source troop factor, target hp kept >= cap:")
    source_fit = fit_troop_factor(
        samples,
        cap=args.cap,
        varying="source",
        exclude_variable_or_composite_skills=exclude_variable,
        require_other_at_or_above_cap=True,
        skill=args.skill,
    )
    print_fit(source_fit, sample_groups=args.sample_groups)
    print()

    print("Candidate alpha comparison:")
    evaluations = evaluate_candidate_alphas(
        samples,
        DEFAULT_CANDIDATES,
        cap=args.cap,
        varying="source",
        exclude_variable_or_composite_skills=exclude_variable,
        require_other_at_or_above_cap=True,
        skill=args.skill,
    )
    for item in evaluations:
        print(
            "  "
            f"alpha={item.alpha:.6f}: rows={item.rows}, groups={item.groups}, "
            f"rmse_log={item.rmse_log:.6f}, "
            f"median_abs_log={item.median_abs_log:.6f}, "
            f"p80_abs_log={item.p80_abs_log:.6f}"
        )

    if args.fit_target:
        print()
        print("Target troop factor, source hp kept >= cap:")
        target_fit = fit_troop_factor(
            samples,
            cap=args.cap,
            varying="target",
            exclude_variable_or_composite_skills=exclude_variable,
            require_other_at_or_above_cap=True,
            skill=args.skill,
        )
        print_fit(target_fit, sample_groups=args.sample_groups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
