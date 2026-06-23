#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture NSLG battle strings from xLua via Frida."""

from __future__ import annotations

import argparse
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

import frida

from sanmou import battle_store, report_parser
from sanmou.report_config import auto_previous_report, inherit_config
from sanmou.review_prompt import review_markdown_interactively

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CAPTURED = PROJECT_ROOT / "data" / "raw_captures"
REPORTS = PROJECT_ROOT / "reports"
DEFAULT_DB = PROJECT_ROOT / "data" / "sanmou_battles.sqlite"
CAPTURED.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

SESSION_LOCK = threading.RLock()
SESSION_ID = ""
SESSION_FILE: Path | None = None
SESSION_RECORDS = 0
PARSED_FILES: set[Path] = set()


def setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def new_session() -> tuple[str, Path]:
    global SESSION_ID, SESSION_FILE, SESSION_RECORDS
    with SESSION_LOCK:
        SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
        SESSION_FILE = CAPTURED / f"battle_{SESSION_ID}.txt"
        SESSION_FILE.touch()
        SESSION_RECORDS = 0
        return SESSION_ID, SESSION_FILE


JS = r"""
var captured = 0;
var keywordsOnly = __KEYWORDS_ONLY__;

var battleKeywords = [
    '兵力', '开始行动', '发动普通攻击', '进行反击', '进行连击',
    '行动顺序判断', '当前补给值', '强化效果', '发动战法', '获得战法',
    '执行来自', '效果已施加', '效果已消失', '效果已刷新', '已叠加', '已满层',
    '触发', '承担伤害', '抵御机会', '无法行动', '暂时失效',
    '治疗效果', '造成伤害', '受到伤害',
    '武力', '智力', '统率', '先攻', '攻心', '倒戈', '会心', '奇谋',
    '连击率', '破甲', '规避',
    '战报', '武将', '抵御', '战船', '嘲讽', '技穷', '断粮', '畏惧',
    '兵种加成', '盾兵', '弓兵', '枪兵', '骑兵',
    '胜利', '失败', '平局'
];

var templateMarkers = [
    'heroName', 'targetHeroName', 'skillName', 'buffName',
    'propertyName', 'propertyValue', 'resultPropertyValue',
    'changeSoldierNum', 'resultSoldierNum', 'damageColor',
    'damageType', 'fatal%', 'ratio%', 'curNum', 'teamName',
    'moral', 'damageChangePercent', 'propName', 'relationName',
    'changeNum', 'provideHeroName', 'applyHeroName', 'resistRate%',
    'soldierNum', 'changeDesc', 'winCampType'
];

function hasAny(str, items) {
    for (var i = 0; i < items.length; i++) {
        if (str.indexOf(items[i]) !== -1) return true;
    }
    return false;
}

try {
    var xlua = Process.getModuleByName('xlua.dll');
    var targets = [
        'xlua_pushstring',
        'lua_pushstring',
        'lua_pushlstring',
    ];

    xlua.enumerateExports().forEach(function(exp) {
        if (exp.type !== 'function') return;
        var name = exp.name;
        if (targets.indexOf(name) === -1) return;

        try {
            Interceptor.attach(exp.address, {
                onEnter: function(args) {
                    try {
                        var str = null;
                        if (name.indexOf('pushlstring') !== -1) {
                            var len = args[2].toInt32();
                            if (len > 0 && len <= 20000) {
                                str = args[1].readUtf8String(len);
                            }
                        } else {
                            str = args[1].readUtf8String();
                        }
                        if (!str || str.length <= 0) return;
                        if (!/[\u4e00-\u9fff]/.test(str)) return;
                        if (hasAny(str, templateMarkers)) return;
                        if (keywordsOnly && !hasAny(str, battleKeywords)) return;
                        captured++;
                        send({t: 'str', x: str, n: captured});
                    } catch(e) {}
                }
            });
        } catch(e) {}
    });

    console.log('[xlua] hooks ready. Open a battle report.');
} catch(e) {
    console.log('[xlua] error: ' + e);
}
"""


def on_msg(msg, data, preview_len: int, progress_every: int) -> None:
    global SESSION_RECORDS
    if msg["type"] == "send":
        payload = msg.get("payload", {})
        if payload.get("t") != "str":
            return
        text = payload["x"]
        with SESSION_LOCK:
            SESSION_RECORDS += 1
            session_file = SESSION_FILE
            session_records = SESSION_RECORDS

        if session_file is None:
            return

        if progress_every > 0 and session_records % progress_every == 0:
            preview = re.sub(r"<[^>]+>", "", text)[:preview_len]
            print(f"[{session_records:5d}] {preview}", flush=True)

        with session_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"{text}\n---\n")
        return

    if msg["type"] == "log":
        level = msg.get("level", "info")
        payload = str(msg.get("payload", ""))
        print(f"[Frida:{level}] {payload[:500]}", flush=True)
        return

    desc = str(msg.get("description", ""))
    if desc:
        print(f"[Frida] {desc[:500]}", flush=True)


def import_to_database(capture_path: Path, db_path: Path, report=None) -> int:
    conn = battle_store.connect(db_path)
    try:
        battle_store.setup_schema(conn)
        return battle_store.import_capture(
            conn,
            capture_path,
            reports_dir=REPORTS,
            dedup="local",
            parsed_report=report,
        )
    finally:
        conn.close()


def inherit_markdown_config(md_path: Path, previous: str = "auto") -> tuple[bool, str]:
    previous_path = auto_previous_report(md_path) if previous == "auto" else Path(previous)
    text = inherit_config(previous_path, md_path)
    md_path.write_text(text, encoding="utf-8")
    return True, f"配置已继承: {previous_path.name}"


def parse_and_save(
    session_id: str | None = None,
    session_file: Path | None = None,
    force: bool = False,
    import_db: bool = True,
    db_path: Path = DEFAULT_DB,
    inherit_config: bool = False,
    previous_config: str = "auto",
    review: bool = True,
) -> Path | None:
    with SESSION_LOCK:
        sid = session_id or SESSION_ID
        path = session_file or SESSION_FILE

    if path is None:
        return None
    if path in PARSED_FILES and not force:
        return None

    raw_count = path.read_text(encoding="utf-8").count("---")
    if raw_count < 10:
        print(f"\n数据太少（{raw_count} 条），跳过解析")
        return None

    report = report_parser.parse_capture(path, dedup="local")
    if report.event_count == 0:
        print(f"\n没有识别到可解析战报流水（原始 {report.raw_count} 条），跳过 Markdown 生成")
        PARSED_FILES.add(path)
        return None
    md_path = REPORTS / f"battle_{sid}.md"
    report_parser.prompt_for_missing_unit_types(report)
    md_path.write_text(report_parser.format_markdown(report), encoding="utf-8")
    PARSED_FILES.add(path)

    inherit_message: str | None = None
    if inherit_config:
        try:
            _changed, inherit_message = inherit_markdown_config(md_path, previous_config)
        except Exception as exc:
            inherit_message = f"配置继承失败: {exc}"
            import_db = False
    elif review:
        try:
            reviewed = review_markdown_interactively(md_path, report, db_path)
            if not reviewed:
                import_db = False
        except Exception as exc:
            print(f"交互补全失败: {exc}")
            import_db = False

    db_report_id: int | None = None
    db_error: Exception | None = None
    if import_db:
        try:
            db_report_id = import_to_database(path, db_path, report=report)
        except Exception as exc:
            db_error = exc

    print("\n" + "=" * 60)
    print(f"战报已生成: {md_path}")
    print(f"原始捕获: {path}")
    if inherit_message:
        print(inherit_message)
    if import_db and db_report_id is not None:
        print(f"数据库已更新: {db_path}（report_id={db_report_id}）")
    elif import_db and db_error is not None:
        print(f"数据库导入失败: {db_error}")
    else:
        print("数据库未更新：可稍后运行 scripts\\import_reviewed_report.py 导入")
    print(f"原始 {report.raw_count} 条，可解析 {report.parsed_count} 条，输出 {report.event_count} 条，去重: {report.dedup_mode}，严格按原始抓取顺序")
    print("=" * 60)
    return md_path


def find_process(device, process_name: str):
    keyword = process_name.lower()
    matches = [p for p in device.enumerate_processes() if keyword in p.name.lower()]
    if not matches:
        return None
    matches.sort(key=lambda p: (p.name.lower() != process_name.lower(), p.name.lower()))
    return matches[0]


def list_processes(device, keyword: str) -> None:
    keyword = keyword.lower()
    rows = [p for p in device.enumerate_processes() if keyword in p.name.lower()]
    for p in rows:
        print(f"{p.pid:>8}  {p.name}")
    if not rows:
        print(f"没有找到包含 {keyword!r} 的进程")


def main() -> None:
    setup_stdio()
    parser = argparse.ArgumentParser(description="三国谋定天下战报捕获器（Frida/xLua -> 交互补全 -> SQLite + reports/*.md）")
    parser.add_argument("--pid", type=int, help="直接指定 Frida 要附加的 PID")
    parser.add_argument("--process", default="com.bilibili.nslg", help="进程名关键字")
    parser.add_argument("--list-processes", action="store_true", help="列出匹配进程后退出")
    parser.add_argument("--no-parse", action="store_true", help="只保存原始捕获，不生成 Markdown 或导入 SQLite")
    parser.add_argument("--no-review", action="store_true", help="不在 PowerShell 中交互补全，只生成待校对 Markdown")
    parser.add_argument("--no-db", action="store_true", help="不自动导入 SQLite")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径，默认 data/sanmou_battles.sqlite")
    parser.add_argument("--inherit-config", action="store_true", help="生成 Markdown 后尝试沿用上一份战报配置，再导入数据库")
    parser.add_argument("--previous-config", default="auto", help="配置继承来源；默认 auto 按文件名选择上一份 Markdown")
    parser.add_argument("--all-chinese", action="store_true", help="捕获所有中文字符串，仅用于排查关键词漏抓；默认只捕获战报关键词")
    parser.add_argument("--preview-len", type=int, default=120, help="终端预览长度")
    parser.add_argument("--progress-every", type=int, default=1, help="每捕获 N 条打印一次预览；0 表示不打印")
    args = parser.parse_args()

    print("=" * 60)
    print("三国谋定天下战报捕获 - Frida/xLua -> 交互补全 -> SQLite")
    print("=" * 60)

    device = frida.get_local_device()
    if args.list_processes:
        list_processes(device, args.process)
        return

    if args.pid:
        pid = args.pid
        print(f"PID: {pid}")
    else:
        proc = find_process(device, args.process)
        if not proc:
            print(f"未找到游戏进程（关键字: {args.process}）。优先找 com.bilibili.nslg，不是 NSLG.exe。")
            raise SystemExit(1)
        pid = proc.pid
        print(f"进程: {proc.name} (PID {pid})")

    sid, session_file = new_session()
    print(f"会话: {sid}")
    print(f"临时文件: {session_file}")
    print(f"捕获模式: {'所有中文字符串（过滤模板占位符）' if args.all_chinese else '关键词过滤'}")
    if args.no_parse:
        print("输出流程: 原始抓取")
    elif args.no_review:
        suffix = " -> 配置继承" if args.inherit_config else ""
        db_suffix = "" if args.no_db else "（不入库，待校对）"
        print(f"输出流程: 原始抓取 -> Markdown{suffix}{db_suffix}")
    else:
        suffix = " -> 配置继承" if args.inherit_config else ""
        db_suffix = "" if args.no_db else f" -> SQLite ({Path(args.db)})"
        review_suffix = "" if args.inherit_config else " -> PowerShell补全"
        print(f"输出流程: 原始抓取 -> Markdown{suffix}{review_suffix}{db_suffix}")
    print()

    session = device.attach(pid)
    script_source = JS.replace("__KEYWORDS_ONLY__", "false" if args.all_chinese else "true")
    script = session.create_script(script_source)
    script.on("message", lambda msg, data: on_msg(msg, data, args.preview_len, args.progress_every))
    script.load()

    print("在游戏中打开一份战报，等待播放完毕。")
    print("按 Enter 保存当前战报并开启下一份；按 Ctrl+C 退出。")
    print()

    try:
        while True:
            line = input()
            if line.strip() == "":
                with SESSION_LOCK:
                    current_id, current_file, current_count = SESSION_ID, SESSION_FILE, SESSION_RECORDS
                if args.no_parse:
                    print(f"\n已保存原始捕获: {current_file}（{current_count} 条）")
                else:
                    parse_and_save(
                        current_id,
                        current_file,
                        import_db=not args.no_db and not args.no_review,
                        db_path=Path(args.db),
                        inherit_config=args.inherit_config,
                        previous_config=args.previous_config,
                        review=not args.no_review,
                    )
                _next_id, next_file = new_session()
                print(f"\n已开启下一份: {next_file.name}\n")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except Exception:
            pass

    if not args.no_parse:
        print("\n正在解析当前会话...")
        parse_and_save(
            import_db=not args.no_db and not args.no_review,
            db_path=Path(args.db),
            inherit_config=args.inherit_config,
            previous_config=args.previous_config,
            review=not args.no_review,
        )
    else:
        with SESSION_LOCK:
            print(f"\n原始捕获已保存: {SESSION_FILE}（{SESSION_RECORDS} 条）")
    print("完成。")


if __name__ == "__main__":
    main()
