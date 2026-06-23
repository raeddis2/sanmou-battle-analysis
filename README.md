# 三国谋定天下 — SQLite-first battle report analysis

一个 **SQLite-first** 的《三国谋定天下》战报分析工具。从原始战斗流水抓取，到配置补全、结构化入库、伤害公式验证，全程以 SQLite 为准。

## 安装

```bash
pip install -e .
```

如需抓取功能（依赖 Frida）：

```bash
pip install -e ".[capture]"
```

开发依赖（测试等）：

```bash
pip install -e ".[dev]"
```

## 目录结构

```
data/                   # 唯一事实源：SQL dump 和原始捕捉
src/sanmou/             # Python 包
  db.py                 # 数据库连接与基础查询
  analysis/             # 分析逻辑
scripts/                # 命令行脚本入口
reports/                # 人工检查用 Markdown（不入库）
docs/                   # 文档、公式推导、口径记录
```

## 核心约束

- 分析只从 SQLite 读取，不从战报 Markdown 反向解析
- `docs/codex_lessons.jsonl` 记录取数口径和踩坑修正
- `docs/codex_milestones.jsonl` 记录阶段性结论和下一步
- NPC 武将（初始兵力 = 16000 或任意时刻兵力 > 11000）红度/品级/金印/战法红度固定为 0，韬略固定为「无韬略」

## 推荐工作流

1. 抓取战报流水 → `data/raw_captures/`
2. 生成 Markdown 供人工检查 → `reports/`
3. 交互补全配置并导入 SQLite
4. 从 SQLite 运行分析脚本
5. 文档和口径更新 → `docs/`

## 许可证

MIT
