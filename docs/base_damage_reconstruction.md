# 伤害还原为 Base 的操作手册

日期：2026-06-22

本文档给 Codex 使用，目的不是记录某个阶段公式结论，而是规定以后如何把战报中的观测伤害还原成可拟合的 `Base`，避免把兵力、破甲、韬略、会心、以静制动、追伤等乘区漏掉或重复计算。

唯一事实源是 SQLite：

```text
data/sanmou_battles.sqlite
```

不要从 `reports/*.md` 反向解析战斗流水。

## Base 层级

必须先说明自己使用哪一层 Base。

```text
观测伤害 D_obs
  = Base_skill
  × 技能倍率
  × 兵力因子
  × 兵种克制
  × 攻方增伤桶
  × 目标受伤桶
  × 品级隐式项
  × 会心/奇谋/反击等触发倍率
  × 特殊乘区
```

如果把技能倍率也剥掉：

```text
Base_common = D_obs / 全部外部乘区 / 技能倍率
```

当前普攻分析里，普通攻击技能倍率视为 `1`，所以脚本里的 `base` 等同于 `Base_common`。分析战法时必须额外剥离战法说明中的伤害率，例如摧坚克难、红妆缭乱、纵马横枪追伤等。

固定 `B=160` 探索攻防项时，继续定义：

```text
Z = Base_common × (目标有效实时统率 + 160)
K_config = Z / 出手时实时主属性
```

其中武力型先用实时武力，智力型另行判断。

## 样本入口

优先读取：

```text
damage_contexts
participants
reports
game_statuses / game_basic_rules / game_tactics / report_skill_details
```

普通攻击样本的最低筛选：

```sql
d.skill = '普通攻击'
AND d.action_type = 'normal_attack'
AND d.damage_event_type = 'damage_raw'
AND d.damage > 0
```

默认排除：

```text
target_hp_after <= 0 的斩杀截断行
remain <= 0 的斩杀截断行
无法识别攻方/目标配置的行
特殊乘区无法还原的行
```

斩杀截断行永远不参与公式参数拟合。原因是观测伤害会被目标剩余兵力截断，只能说明真实伤害不低于该值，不能当作精确伤害。若以后要利用斩杀行，只能作为不等式约束单独建模，不能混入普通回归或 K_config 计算。

不要把以下行混进普通攻击：

```text
纵马横枪追伤：skill=纵马横枪，buff=纵马横枪-追伤，action_type=buff_exec
红妆缭乱追击：skill=红妆缭乱，通常在普通攻击之后单独出现 damage 行
反击/交换普攻/承担伤害：需要单独分类
```

## 还原公式

通用还原：

```text
Base_common =
    D_obs
  / M_skill
  / F_source_troop
  / M_unit_counter
  / M_source_damage
  / M_target_taken
  / M_grade
  / M_trigger
  / M_special
```

目标有效统率：

```text
target_eff_command = target_command × (1 - pierce_pct / 100)
```

破甲不是乘在伤害上的独立乘区，而是改目标统率。无视统率的追伤不要放进吃统率样本；若必须纳入，分母应按无视统率模型单独处理。

## 乘区取法

### 技能倍率

`M_skill` 来自战法说明，不来自伤害数字拟合硬猜。

普通攻击：

```text
M_skill = 1
```

红妆缭乱：

```text
每段按说明的兵刃伤害率剥离
额外段概率只决定是否出现额外 damage 行，不是单段倍率
```

纵马横枪追伤：

```text
按说明伤害率剥离
无视统率
目标持有负面状态时额外 ×1.20
```

玩家马超队伍里，若大乔存活且有相思文赋，国色施加的负面影响覆盖敌军全体，己方马超追伤可触发 `+20%`。NPC 马超则按目标 active_buffs 是否命中官方负面状态判断。

### 兵力因子

当前保守口径：

```text
F_source_troop(N) = 1                    , N >= 9000
F_source_troop(N) = (N / 9000) ^ 0.38     , N < 9000
```

`N` 优先取 `source_context_json.hp`，为空时取 `participants.initial_troops`。

注意：马云禄、连击、倒戈回血密集样本说明低兵力行会污染 `K_config`。判断配置 K 与入场属性关系时，优先使用：

```text
source_hp >= 9000
```

低兵力样本应单独用于拟合兵力因子，不要直接压进配置 K。

### 兵种克制

从兵种克制表读取；当前常用关系：

```text
盾克弓、弓克枪、枪克骑、骑克盾
攻方克制目标：1.15
攻方被目标克制：0.85
无克制：1.00
```

### 攻方增伤桶

当前脚本口径：

```text
M_source_damage =
  1 + (source_damage_pct + 对{目标国家}武将伤害提升) / 100
```

`source_damage_pct` 读 `damage_contexts.source_damage_pct`。

国家定向增伤从 `source_context_json.props["对{目标国家}武将伤害提升"]` 取。

不要把扁平列和 JSON props 中同名字段无脑相加。若 flat/json 是同一个状态的重复展开，只能计一次。

### 目标受伤桶

当前脚本口径：

```text
M_target_taken =
  1 + (target_damage_taken_pct - 受到{攻方国家}武将伤害降低) / 100
```

`target_damage_taken_pct` 通常已经包含畏惧、国色等“受到伤害提升/降低”的结果。

国家定向减伤从 `target_context_json.props["受到{攻方国家}武将伤害降低"]` 取。

### 品级隐式项

当前口径：

```text
M_grade = (1 + source_grade / 100) × (1 - target_grade / 100)
```

NPC 强制按 0 品处理。不要从 `state_changes` 的第 0 回合显式“造成伤害/受到伤害”变化反推品级；品级/金印是隐式项。

### 会心/奇谋/当前触发

不要直接相信 `recent_trigger` 字段名，它可能残留倒戈等上一段触发。

当前伤害触发倍率应满足：

```text
action_event_order <= trigger_event_order < damage_event_order
```

且 `recent_trigger.damage_ratio` 有有效倍率。常见：

```text
150% -> 1.50
160% -> 1.60
```

倒戈的 `damage_ratio` 为空，不是增伤乘区，只是回血状态。

韬略铁骑令等会心伤害提升要体现在会心倍率里；不能按固定 `1.50` 写死。

### 破甲

破甲读法：

```text
flat = damage_contexts.source_pierce_pct
embedded = source_context_json.props["破甲"]
```

若 flat 与 embedded 相等，认为是同一状态重复展开，只取一次。若不一致，保守取较大值并单独审计。

使用方式：

```text
target_eff_command = target_command × (1 - pierce_pct / 100)
```

不要把破甲既改统率又当伤害乘区。

### 特殊乘区

特殊乘区按技能文字和伤害类型标签处理，不要只按 buff 名套一个固定值。

以静制动-静：

```text
描述：奇数回合受到兵刃伤害、普通攻击伤害、追击战法伤害减少 35%
普通攻击行：按普通攻击伤害只剥离一层 0.65
追击战法行：按追击战法伤害先剥离一层 0.65
纯兵刃非普攻非追击行：通常先按 0.65，需逐条核对
```

不要把 `damage_raw` / `damage` 当作“兵刃伤害”标签。它们是事件类型，不足以证明本次伤害应同时吃“兵刃伤害减少”和“普通攻击伤害减少”两层。若后续官方文字或重复样本证明存在叠乘，再单独更新口径。

虚弱：

```text
造成的最终伤害降低 70%，按特殊乘区 ×0.30 或剔除
```

抵御、完璧、云身、协防/承担、传递伤害等：

```text
能还原则单独建模
不能还原则剔除污染样本
```

妖术：

```text
会心和奇谋伤害降低 15%，只影响会心/奇谋触发后的倍率，不是普通无触发伤害乘区
```

## 必查审计清单

每次给出 A/B/C/K 结论前，至少抽查异常行并输出：

```text
report_key
damage_contexts.id
event_order / round_no
source / target
skill / buff / action_type / damage_event_type
damage / target_hp_before / target_hp_after / remain
source_force / source_intelligence
target_command / pierce_pct / target_eff_command
source_hp / troop_factor
source_damage_pct / target_damage_taken_pct
国家定向增伤/减伤
source_grade / target_grade / NPC 判定
current_trigger / recent_trigger
source_active_buffs / target_active_buffs
raw_text
反推 Base_common / Z / K
```

异常残差先按这个清单排查，再讨论隐藏系数或策划暗改。

## 已知高危坑

1. 同名武将不能直接合并。马超有 NPC/玩家版本，吕布有多种场外武力，必须按 NPC、初始兵力、场外属性、首回合前属性、品级和配置拆分。
2. 场外属性不要用 `damage_contexts.source_force/target_force`。它们是战斗中实时属性快照。
3. 低兵力行不能默认用于配置 K。马云禄 all rows 的 K 分叉主要来自低兵力/倒戈/连击污染。
4. 破甲字段可能 flat/json 重复，不能相加。
5. 红妆缭乱的概率不是倍率。
6. 以静制动-静对普攻先按一层 0.65 处理；不要因为事件类型是 `damage_raw` 就叠乘第二层。
7. 斩杀行会被剩余兵力截断，永远不参与普通拟合；只能作为不等式约束单独建模。
8. 目标负面状态的“受到伤害提升”通常已经进 `target_damage_taken_pct`，不要再额外乘一次；但某些技能条件增伤，如纵马横枪追伤的负面状态 `+20%`，需要单独处理。

## 当前普攻/Z 分析脚本

当前实现参考：

```text
scripts/analyze_z_attribute_candidates.py
```

该脚本当前剥离：

```text
兵力因子
兵种克制
攻方增伤桶
目标受伤桶
国家定向增伤/减伤
品级隐式项
当前会心/奇谋触发倍率
破甲后的目标有效统率
以静制动-静特殊减伤，普攻只剥一层
```

并导出：

```text
docs/z_k_config_vs_off_force_physical_20260622.csv
docs/z_k_config_vs_off_force_physical_20260622.png
docs/z_k_config_vs_off_force_physical_clean_hp9000_20260622.csv
docs/z_k_config_vs_off_force_physical_clean_hp9000_20260622.png
```

以后若修改还原口径，必须同步更新本文档和脚本注释。
