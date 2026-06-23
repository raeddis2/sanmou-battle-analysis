# 三国谋定天下项目规则

## 当前项目定位

- 这是一个重启后的干净项目。
- 旧项目已归档在同级目录 `C:\Users\raeddis\Documents\三国谋定天下_legacy_20260622`。
- 除非用户明确要求查旧资料，不要读取、搜索、导入或复用 legacy 目录里的代码。

## 唯一事实源

- 当前项目的战报数据唯一事实源是 SQLite 数据库：`data/sanmou_battles.sqlite`。
- 分析、查询、报告生成默认都必须从 SQLite 读取。
- 不要把人工检查 Markdown、原始捕获文本、历史 CSV 当作当前数据源。
- 不要新增“从战报 Markdown 直接解析战斗数据”的日常入口。

## Markdown 战报定位

- 当前项目允许生成 Markdown 战报，但只放在 `reports/`，用途是人工检查、补全和校对。
- `reports/*.md` 可以包含红度、金印、兵种、战法、韬略、战法详情等人工确认配置。
- `reports/*.md` 不是事实源；业务分析不得从 Markdown 反向读取战斗流水。
- 如果需要把人工补全的配置写回 SQLite，必须使用明确的同步脚本，并说明同步字段。
- 抓取流程可以生成 Markdown 供检查，可以在 PowerShell 中交互补全配置，也可以自动沿用上一份 Markdown 的配置块，但最终结构化数据仍应写入 SQLite。

## NPC 判定规则

- 如果一个武将的初始兵力为 `16000`，则该武将判定为 NPC，不是玩家武将。
- 玩家武将任意时刻兵力不可能超过 `11000`；如果流水中观测到某武将兵力大于 `11000`，该武将也判定为 NPC。
- NPC 的武将红度、品级、金印数、自带战法红度、所有战法红度永远按 `0` 处理，韬略永远按 `无韬略` 处理。
- NPC 证据和初始兵力应优先从战报流水自动识别；只有流水没有可靠信号时，才在 PowerShell 中询问用户。
- 交互补全和导入 SQLite 时都必须强制应用该规则，即使 Markdown 中写了其他值也要纠正。

## 武将场外属性

- “武将场外属性”指武将进战斗前面板上的四维：场外武力、场外智力、场外统率、场外先攻。
- 场外属性可从 SQLite 的 `state_changes` 反推，默认口径是读取每份战报、每名武将、每个四维属性在第 0 回合的首条 `property` 变化。
- 反推公式：`提升` 时为 `result_num - value_num`；`降低` 时为 `result_num + value_num`；`保持不变` 时为 `result_num`。
- 不要把 `damage_contexts.source_force/target_force` 当作场外属性；这些字段是造成伤害时的战斗中实时属性快照。
- 如果同一份战报出现双方同名武将，必须先消除 `state_changes.hero` 无法区分 side/team 的歧义，再使用场外属性。
- 新增或导入战报后，运行 `python scripts/audit_off_battle_attributes.py` 确认场外属性覆盖率和置信度。

## 旧数据迁移

- 如果数据库缺数据，只能写明确的一次性迁移脚本，命名放在 `scripts/import_legacy_*_once.py`。
- 一次性迁移脚本必须在文件头说明来源、目标表、运行时机。
- 迁移完成后，业务分析代码不得依赖 legacy 文件。

## 推荐工作流

1. 抓取原始战报流水到 `data/raw_captures/`。
2. 同步生成 `reports/*.md` 作为人工检查件。
3. 如果本份战报配置与上一份相同，可用配置继承脚本自动填充 Markdown 的配置块。
4. 新配置优先通过流水自动识别 NPC 证据、初始兵力和韬略，再在 PowerShell 中交互补全红度、品级、兵种和战法红度；NPC 自动按规则置 0 且韬略为 `无韬略`；战法说明按“战法名 + 红度”从 SQLite 知识表补齐。
5. 补全后导入 SQLite。
6. 从 SQLite 查询结构化数据。
7. 在 `src/sanmou/analysis/` 中实现分析逻辑。
8. 在 `scripts/` 中放命令行入口。
9. 在 `docs/` 中记录当前口径和结论。

## Codex 工作记忆

- `docs/codex_lessons.jsonl` 记录分析过程中的经验、踩坑、脚本错误、取数错误和修正办法，只写以后要避免再犯的内容。
- `docs/codex_milestones.jsonl` 记录阶段性成果、已确认结论、当前口径和下一步。
- 两个文件都使用 UTF-8 JSONL，每行一个 JSON 对象，便于追加和 `rg` 检索；不要改成长篇 Markdown。
- `codex_lessons` 建议字段：`date`、`type`、`topic`、`symptom`、`cause`、`fix`、`prevention`、`files`、`tags`。
- `codex_milestones` 建议字段：`date`、`stage`、`status`、`summary`、`decisions`、`next`、`confidence`。
- 这两个文件是 Codex 工作记忆，不是战斗事实源；战斗分析、统计和报告生成仍必须从 SQLite 读取。
- 开始分析前，优先用 `rg` 按主题检索这两个文件；只有需要回顾近期上下文时才完整读取。

## 文件与编码

- 默认使用 UTF-8。
- 可以正常使用中文文件名和中文 Markdown 内容。
- 不要创建大体积中间产物，除非用户明确要求。
