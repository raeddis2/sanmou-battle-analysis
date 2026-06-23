"""Plot visual diagnostics for the forward force-type damage formula.

The plotting data is rebuilt from SQLite through forward_force_damage_formula.py
instead of reading the previously exported CSV.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

import forward_force_damage_formula as formula


OUT_DIR = Path("docs")
OUT_SCATTER = OUT_DIR / "forward_formula_pred_vs_actual_20260623.png"
OUT_RATIO_BOX = OUT_DIR / "forward_formula_obs_pred_ratio_by_skill_20260623.png"
OUT_COVERAGE = OUT_DIR / "forward_formula_coverage_by_skill_20260623.png"
OUT_HEATMAP = OUT_DIR / "forward_formula_hero_skill_error_heatmap_20260623.png"
OUT_HP_ERROR = OUT_DIR / "forward_formula_hp_vs_error_20260623.png"
OUT_INDEX = OUT_DIR / "forward_force_damage_formula_visuals_20260623.md"


SKILL_ORDER = [
    "普通攻击",
    "摧坚克难",
    "纵马横枪",
    "定军扬威",
    "红妆缭乱",
    "万人之敌",
    "水淹七军",
    "辕门射戟",
    "骁勇无前",
]

HERO_ORDER = ["吕布", "张飞", "赵云", "马云禄", "马超", "黄忠"]

SKILL_COLORS = {
    "普通攻击": "#3b82f6",
    "摧坚克难": "#10b981",
    "纵马横枪": "#ef4444",
    "定军扬威": "#f59e0b",
    "红妆缭乱": "#ec4899",
    "万人之敌": "#8b5cf6",
    "水淹七军": "#06b6d4",
    "辕门射戟": "#84cc16",
    "骁勇无前": "#64748b",
}


def configure_matplotlib():
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
    plt.rcParams["figure.facecolor"] = "white"
    plt.rcParams["axes.facecolor"] = "white"
    return plt


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def rel_error(sample: formula.ForwardSample) -> float:
    return abs(sample.observed_damage - sample.pred_final) / sample.observed_damage


def obs_pred_ratio(sample: formula.ForwardSample) -> float:
    return sample.observed_damage / sample.pred_final if sample.pred_final > 0 else math.nan


def group_by(samples: list[formula.ForwardSample], attr: str) -> dict[str, list[formula.ForwardSample]]:
    grouped: dict[str, list[formula.ForwardSample]] = defaultdict(list)
    for sample in samples:
        grouped[str(getattr(sample, attr))].append(sample)
    return grouped


def coverage(samples: list[formula.ForwardSample], threshold: float) -> float:
    if not samples:
        return math.nan
    return sum(rel_error(sample) <= threshold for sample in samples) / len(samples)


def plot_pred_vs_actual(samples: list[formula.ForwardSample], plt) -> None:
    fig, ax = plt.subplots(figsize=(9.6, 7.2), dpi=180)

    max_damage = max(max(sample.observed_damage, sample.pred_final) for sample in samples)
    upper = math.ceil(max_damage / 100) * 100
    xs = [0, upper]
    ax.fill_between(xs, [0, upper * 0.9], [0, upper * 1.1], color="#dbeafe", alpha=0.55, label="±10% 区间")
    ax.plot(xs, xs, color="#111827", linewidth=1.3, label="完全命中")
    ax.plot(xs, [value * 1.1 for value in xs], color="#94a3b8", linewidth=0.9, linestyle="--")
    ax.plot(xs, [value * 0.9 for value in xs], color="#94a3b8", linewidth=0.9, linestyle="--")

    for skill in SKILL_ORDER:
        typed = [sample for sample in samples if sample.skill == skill]
        if not typed:
            continue
        ax.scatter(
            [sample.pred_final for sample in typed],
            [sample.observed_damage for sample in typed],
            s=18 if skill != "纵马横枪" else 26,
            color=SKILL_COLORS.get(skill, "#6b7280"),
            alpha=0.48 if skill != "纵马横枪" else 0.72,
            edgecolors="#111827" if skill == "纵马横枪" else "none",
            linewidths=0.25,
            label=f"{skill} n={len(typed)}",
        )

    ax.set_xlim(0, upper)
    ax.set_ylim(0, upper)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title("正向公式预测 vs 战报实际损兵（非斩杀）", fontsize=14, pad=12)
    ax.set_xlabel("公式预测损兵")
    ax.set_ylabel("战报实际损兵")
    ax.grid(True, color="#e5e7eb", linewidth=0.8)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(OUT_SCATTER)
    plt.close(fig)


def plot_ratio_box(samples: list[formula.ForwardSample], plt) -> None:
    grouped = group_by(samples, "skill")
    labels = [skill for skill in SKILL_ORDER if skill in grouped]
    data = [
        [obs_pred_ratio(sample) for sample in grouped[skill] if math.isfinite(obs_pred_ratio(sample))]
        for skill in labels
    ]

    fig, ax = plt.subplots(figsize=(10.6, 6.4), dpi=180)
    ax.axvspan(0.9, 1.1, color="#dcfce7", alpha=0.55, label="±10% 区间")
    ax.axvline(1.0, color="#111827", linewidth=1.1)
    ax.axvline(0.9, color="#94a3b8", linewidth=0.9, linestyle="--")
    ax.axvline(1.1, color="#94a3b8", linewidth=0.9, linestyle="--")

    box = ax.boxplot(
        data,
        vert=False,
        tick_labels=[f"{skill}  n={len(grouped[skill])}" for skill in labels],
        widths=0.62,
        patch_artist=True,
        showfliers=True,
        flierprops={
            "marker": ".",
            "markersize": 2.2,
            "markerfacecolor": "#475569",
            "markeredgecolor": "none",
            "alpha": 0.35,
        },
        medianprops={"color": "#111827", "linewidth": 1.2},
        whiskerprops={"color": "#64748b", "linewidth": 0.9},
        capprops={"color": "#64748b", "linewidth": 0.9},
    )
    for patch, skill in zip(box["boxes"], labels):
        patch.set_facecolor(SKILL_COLORS.get(skill, "#94a3b8"))
        patch.set_alpha(0.58)
        patch.set_edgecolor("#334155")

    ax.set_xlim(0.72, 1.32)
    ax.set_title("各技能实际/预测比值分布（1 表示完全贴合）", fontsize=14, pad=12)
    ax.set_xlabel("战报实际损兵 / 公式预测损兵")
    ax.grid(True, axis="x", color="#e5e7eb", linewidth=0.8)
    ax.legend(loc="lower right", frameon=False)
    fig.tight_layout()
    fig.savefig(OUT_RATIO_BOX)
    plt.close(fig)


def plot_coverage(samples: list[formula.ForwardSample], plt) -> None:
    grouped = group_by(samples, "skill")
    items: list[tuple[str, list[formula.ForwardSample]]] = [
        ("全部非斩杀", samples),
        ("普攻", [sample for sample in samples if sample.sample_type == "normal_attack"]),
        ("兵刃战法", [sample for sample in samples if sample.sample_type == "physical_skill"]),
        ("去纵马横枪", [sample for sample in samples if sample.skill != "纵马横枪"]),
    ]
    items.extend((skill, grouped[skill]) for skill in SKILL_ORDER if skill in grouped)
    items = [(label, group) for label, group in items if group]

    labels = [label for label, _ in items]
    within5 = [coverage(group, 0.05) for _, group in items]
    within10 = [coverage(group, 0.10) for _, group in items]
    y_positions = list(range(len(items)))

    fig, ax = plt.subplots(figsize=(10.6, 7.4), dpi=180)
    bar_h = 0.36
    ax.barh(
        [y - bar_h / 2 for y in y_positions],
        within10,
        height=bar_h,
        color="#60a5fa",
        label="10% 内",
    )
    ax.barh(
        [y + bar_h / 2 for y in y_positions],
        within5,
        height=bar_h,
        color="#14b8a6",
        label="5% 内",
    )

    for y, value, (_, group) in zip(y_positions, within10, items):
        ax.text(min(value + 0.012, 1.02), y - bar_h / 2, f"{pct(value)}  n={len(group)}", va="center", fontsize=8)
    for y, value in zip(y_positions, within5):
        ax.text(min(value + 0.012, 1.02), y + bar_h / 2, pct(value), va="center", fontsize=8)

    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.08)
    ax.set_title("不同切片的误差覆盖率", fontsize=14, pad=12)
    ax.set_xlabel("样本占比")
    ax.xaxis.set_major_formatter(lambda value, _pos: f"{value:.0%}")
    ax.grid(True, axis="x", color="#e5e7eb", linewidth=0.8)
    ax.legend(loc="lower right", frameon=False)
    fig.tight_layout()
    fig.savefig(OUT_COVERAGE)
    plt.close(fig)


def plot_hero_skill_heatmap(samples: list[formula.ForwardSample], plt) -> None:
    grouped: dict[tuple[str, str], list[formula.ForwardSample]] = defaultdict(list)
    for sample in samples:
        grouped[(sample.source, sample.skill)].append(sample)

    heroes = [hero for hero in HERO_ORDER if any(key[0] == hero for key in grouped)]
    skills = [skill for skill in SKILL_ORDER if any(key[1] == skill for key in grouped)]
    matrix: list[list[float]] = []
    text_matrix: list[list[str]] = []
    for hero in heroes:
        row: list[float] = []
        text_row: list[str] = []
        for skill in skills:
            group = grouped.get((hero, skill), [])
            if group:
                med = median(rel_error(sample) for sample in group)
                row.append(med)
                text_row.append(f"{med * 100:.1f}%\nn={len(group)}")
            else:
                row.append(math.nan)
                text_row.append("")
        matrix.append(row)
        text_matrix.append(text_row)

    fig, ax = plt.subplots(figsize=(11.4, 5.5), dpi=180)
    masked = [[value if math.isfinite(value) else math.nan for value in row] for row in matrix]
    image = ax.imshow(masked, cmap="RdYlGn_r", vmin=0.0, vmax=0.16)

    ax.set_xticks(range(len(skills)))
    ax.set_xticklabels(skills, rotation=35, ha="right")
    ax.set_yticks(range(len(heroes)))
    ax.set_yticklabels(heroes)
    ax.set_title("武将 × 技能：中位相对误差热力图", fontsize=14, pad=12)

    for y, row in enumerate(text_matrix):
        for x, label in enumerate(row):
            if not label:
                continue
            value = matrix[y][x]
            color = "white" if math.isfinite(value) and value >= 0.115 else "#111827"
            ax.text(x, y, label, ha="center", va="center", fontsize=7.2, color=color)

    ax.set_xticks([value - 0.5 for value in range(1, len(skills))], minor=True)
    ax.set_yticks([value - 0.5 for value in range(1, len(heroes))], minor=True)
    ax.grid(which="minor", color="white", linestyle="-", linewidth=1.4)
    ax.tick_params(which="minor", bottom=False, left=False)
    cbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.025)
    cbar.ax.set_ylabel("中位相对误差", rotation=90)
    cbar.ax.yaxis.set_major_formatter(lambda value, _pos: f"{value:.0%}")
    fig.tight_layout()
    fig.savefig(OUT_HEATMAP)
    plt.close(fig)


def plot_hp_vs_error(samples: list[formula.ForwardSample], plt) -> None:
    fig, ax = plt.subplots(figsize=(10.2, 6.6), dpi=180)
    ax.axhspan(0, 0.10, color="#dcfce7", alpha=0.45, label="10% 内")
    ax.axhline(0.05, color="#14b8a6", linewidth=0.9, linestyle="--", label="5%")
    ax.axhline(0.10, color="#64748b", linewidth=0.9, linestyle="--", label="10%")
    ax.axvline(formula.TROOP_CAP, color="#111827", linewidth=1.0, linestyle=":", label="兵力阈值 9000")

    for skill in SKILL_ORDER:
        typed = [sample for sample in samples if sample.skill == skill]
        if not typed:
            continue
        ax.scatter(
            [sample.source_hp for sample in typed],
            [rel_error(sample) for sample in typed],
            s=18 if skill != "纵马横枪" else 26,
            color=SKILL_COLORS.get(skill, "#6b7280"),
            alpha=0.44 if skill != "纵马横枪" else 0.70,
            edgecolors="#111827" if skill == "纵马横枪" else "none",
            linewidths=0.25,
            label=skill,
        )

    ax.set_ylim(0, 0.32)
    ax.set_xlim(0, max(sample.source_hp for sample in samples) * 1.03)
    ax.set_title("攻击方兵力与相对误差", fontsize=14, pad=12)
    ax.set_xlabel("攻击方当前兵力")
    ax.set_ylabel("相对误差 abs(实际-预测)/实际")
    ax.yaxis.set_major_formatter(lambda value, _pos: f"{value:.0%}")
    ax.grid(True, color="#e5e7eb", linewidth=0.8)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(OUT_HP_ERROR)
    plt.close(fig)


def write_index(samples: list[formula.ForwardSample]) -> None:
    no_zongma = [sample for sample in samples if sample.skill != "纵马横枪"]
    normal = [sample for sample in samples if sample.sample_type == "normal_attack"]
    physical = [sample for sample in samples if sample.sample_type == "physical_skill"]

    lines = [
        "# 武力型正向公式可视化",
        "",
        "日期：2026-06-23",
        "",
        "这些图从 SQLite 重新计算样本，不从导出的 CSV 反读。",
        "",
        "## 关键读法",
        "",
        f"- 非斩杀总样本：n={len(samples)}，10% 内 {pct(coverage(samples, 0.10))}。",
        f"- 普攻：n={len(normal)}，10% 内 {pct(coverage(normal, 0.10))}。",
        f"- 兵刃战法：n={len(physical)}，10% 内 {pct(coverage(physical, 0.10))}。",
        f"- 去掉纵马横枪：n={len(no_zongma)}，10% 内 {pct(coverage(no_zongma, 0.10))}。",
        "",
        "## 图片",
        "",
        f"![预测 vs 实际]({OUT_SCATTER.as_posix()})",
        "",
        f"![实际预测比值]({OUT_RATIO_BOX.as_posix()})",
        "",
        f"![覆盖率]({OUT_COVERAGE.as_posix()})",
        "",
        f"![武将技能热力图]({OUT_HEATMAP.as_posix()})",
        "",
        f"![兵力与误差]({OUT_HP_ERROR.as_posix()})",
        "",
    ]
    OUT_INDEX.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    plt = configure_matplotlib()
    conn = sqlite3.connect(formula.DB_PATH)
    conn.row_factory = sqlite3.Row
    samples, _skip_reasons = formula.fetch_samples(conn)
    nonlethal = [sample for sample in samples if not sample.is_lethal]
    if not nonlethal:
        raise SystemExit("no nonlethal samples to plot")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_pred_vs_actual(nonlethal, plt)
    plot_ratio_box(nonlethal, plt)
    plot_coverage(nonlethal, plt)
    plot_hero_skill_heatmap(nonlethal, plt)
    plot_hp_vs_error(nonlethal, plt)
    write_index(nonlethal)

    print(f"nonlethal_samples={len(nonlethal)}")
    for path in [OUT_SCATTER, OUT_RATIO_BOX, OUT_COVERAGE, OUT_HEATMAP, OUT_HP_ERROR, OUT_INDEX]:
        print(path)


if __name__ == "__main__":
    main()
