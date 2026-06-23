#!/usr/bin/env python3
"""Import one reviewed Markdown report and its raw capture into SQLite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sanmou import battle_store

DEFAULT_DB = ROOT / "data" / "sanmou_battles.sqlite"
RAW_DIR = ROOT / "data" / "raw_captures"
REPORTS_DIR = ROOT / "reports"
BLOCKING_PLACEHOLDERS = ("待手动补充", "待截图确认", "待补战法名")


def default_capture_for_report(report_path: Path) -> Path:
    return RAW_DIR / f"{report_path.stem}.txt"


def find_blocking_placeholders(report_path: Path) -> list[str]:
    text = report_path.read_text(encoding="utf-8-sig")
    return [placeholder for placeholder in BLOCKING_PLACEHOLDERS if placeholder in text]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入已人工校对的 reports/*.md 到 SQLite")
    parser.add_argument("report", help="已校对的 Markdown，例如 reports/battle_YYYYMMDD_HHMMSS.md")
    parser.add_argument("--capture", help="对应原始捕获；默认 data/raw_captures/<同名>.txt")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--allow-placeholders", action="store_true", help="允许仍含待补占位符时导入")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        parser.error(f"report does not exist: {report_path}")

    capture_path = Path(args.capture) if args.capture else default_capture_for_report(report_path)
    if not capture_path.exists():
        parser.error(f"capture does not exist: {capture_path}")

    placeholders = find_blocking_placeholders(report_path)
    if placeholders and not args.allow_placeholders:
        joined = ", ".join(placeholders)
        raise SystemExit(
            f"仍有待补字段：{joined}\n"
            "请先补全 Markdown，或确认可接受后加 --allow-placeholders。"
        )

    conn = battle_store.connect(Path(args.db))
    try:
        battle_store.setup_schema(conn)
        report_id = battle_store.import_capture(
            conn,
            capture_path,
            reports_dir=report_path.parent,
            dedup="local",
        )
    finally:
        conn.close()

    print(f"已导入 SQLite: report_id={report_id}")
    print(f"Markdown: {report_path}")
    print(f"原始捕获: {capture_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
