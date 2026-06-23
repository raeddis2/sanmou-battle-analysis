"""Helpers for human-review Markdown report configuration blocks."""

from __future__ import annotations

import re
from pathlib import Path


STATS_HEADING = "## 解析统计"
PLACEHOLDERS = ("待手动补充", "待截图确认", "阵营 A", "阵营 B")
DROP_SECTIONS_FOR_CONFIG_ONLY = {
    "总览补充",
    "战法统计补充",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")


def split_at_stats(text: str, path: Path) -> tuple[str, str]:
    match = re.search(r"(?m)^## 解析统计\s*$", text)
    if not match:
        raise ValueError(f"{path} does not contain '{STATS_HEADING}'")
    return text[: match.start()].rstrip(), text[match.start() :]


def markdown_path(path: Path) -> str:
    return path.resolve().as_posix()


def rewrite_source_references(config: str, source_ref: str) -> str:
    config = re.sub(r"(?<!继续)沿用 `[^`]+`", f"沿用 `{source_ref}`", config)
    config = re.sub(
        r"来源配置：\s*\n\s*\n- `[^`]+`",
        f"来源配置：\n\n- `{source_ref}`",
        config,
    )
    config = re.sub(
        r"本战报武将、国家、兵种、战法顺序、战法红度与战法详情按用户说明沿用 `[^`]+`",
        f"本战报武将、国家、兵种、战法顺序、战法红度与战法详情按用户说明沿用 `{source_ref}`",
        config,
    )
    return config


def split_markdown_sections(config: str) -> list[tuple[str | None, str]]:
    matches = list(re.finditer(r"(?m)^## (.+?)\s*$", config))
    if not matches:
        return [(None, config.rstrip())]

    sections: list[tuple[str | None, str]] = []
    lead = config[: matches[0].start()].rstrip()
    if lead:
        sections.append((None, lead))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(config)
        sections.append((match.group(1).strip(), config[match.start() : end].rstrip()))
    return sections


def make_config_only(config: str, source_ref: str) -> str:
    kept = [
        text
        for heading, text in split_markdown_sections(config)
        if heading not in DROP_SECTIONS_FOR_CONFIG_ONLY
    ]
    config = "\n\n".join(kept).rstrip()
    config = config.replace("本次截图确认", f"沿用 `{source_ref}`")
    return rewrite_source_references(config, source_ref)


def inherit_config(source: Path, target: Path, *, full_prefix: bool = False) -> str:
    source_prefix, _source_stats = split_at_stats(read_text(source), source)
    _target_prefix, target_stats = split_at_stats(read_text(target), target)
    source_ref = markdown_path(source)

    if full_prefix:
        inherited = rewrite_source_references(source_prefix, source_ref)
    else:
        inherited = make_config_only(source_prefix, source_ref)

    return f"{inherited.rstrip()}\n\n{target_stats.lstrip()}"


def validate_text(path: Path, text: str) -> list[str]:
    issues: list[str] = []
    if STATS_HEADING not in text:
        issues.append(f"{path}: missing {STATS_HEADING}")
    for placeholder in PLACEHOLDERS:
        if placeholder in text:
            issues.append(f"{path}: still contains placeholder {placeholder!r}")
    return issues


def auto_previous_report(target: Path, reports_dir: Path | None = None) -> Path:
    directory = reports_dir or target.parent
    candidates = sorted(
        path for path in directory.glob("battle_*.md") if path.resolve() != target.resolve()
    )
    previous = [path for path in candidates if path.name < target.name]
    if previous:
        return previous[-1]
    if candidates:
        return candidates[-1]
    raise FileNotFoundError(f"No previous report found in {directory}")
