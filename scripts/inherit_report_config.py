#!/usr/bin/env python3
"""Reuse verified config blocks between human-review Markdown reports.

This script only edits Markdown in reports/ for manual review. It must not be
used as a source of battle event data for analysis.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sanmou.report_config import inherit_config, validate_text, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="沿用上一份人工检查 Markdown 的武将/战法/兵种等配置块"
    )
    parser.add_argument("--source", required=True, help="配置来源 Markdown")
    parser.add_argument("--targets", nargs="+", required=True, help="要填充配置的目标 Markdown")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写入")
    parser.add_argument(
        "--full-prefix",
        action="store_true",
        help="复制来源 ## 解析统计 之前的完整内容，包括战斗专属补充",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    source = Path(args.source)
    targets = [Path(path) for path in args.targets]
    if not source.exists():
        parser.error(f"source does not exist: {source}")
    for target in targets:
        if not target.exists():
            parser.error(f"target does not exist: {target}")

    had_issues = False
    for target in targets:
        new_text = inherit_config(source, target, full_prefix=args.full_prefix)
        issues = validate_text(target, new_text)
        if issues:
            had_issues = True
            for issue in issues:
                print(f"WARN: {issue}", file=sys.stderr)
        if not args.dry_run:
            write_text(target, new_text)
        action = "checked" if args.dry_run else "updated"
        print(f"{action}: {target}")

    return 1 if had_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
