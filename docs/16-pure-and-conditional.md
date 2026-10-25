# 纯表达式与持久条件分支

更新日期为 2026 年 10 月 25 日。本轮加入字段读取、记录构造、封闭纯运算和布尔条件分支，覆盖 IR、Python SDK、`.al0` 源码与同一解释器。条件分支可以包含带效应的计划，且首次选择和显式输入一起持久化。

## 数据操作

新增三个 IR 节点：

| 节点 | 输入 | 输出规则 |
| --- | --- | --- |
| `field` | 一个已声明记录 schema 的值及静态字段名 | 字段 schema；不能读取 Intent、Permit、Operation 或任意 Json 对象内部 |
| `record` | 目标记录 schema 和逐字段引用 | 字段必须完整、名称和 schema 精确匹配 |
| `compute` | 封闭 operator 名及引用列表 | 按 operator 校验数量、输入 schema 并推导输出 |

运算符包括 Int 的 add、sub、mul、div、mod、neg，Bool 的 and、or、not，Text 的 concat、length，以及 eq、lt、le、gt、ge。eq 要求相同 schema，按规范化 JSON 比较，因而 Json 中的 true 不等于 1。排序只接受 Int 或 Text；Text 采用 Unicode 码点序。length 计 Unicode 码点，不计 UTF-8 字节或用户感知字符。

整数仍使用 `[-(2^63-1), 2^63-1]` 域。div 向负无穷取整，mod 与它满足 `a = (a div b) * b + (a mod b)`。例如 `-7 div 3 = -3`，`-7 mod 3 = 2`。没有浮点舍入、自动大整数扩展或隐式 Bool/Int 转换。溢出、除零及纯值超限抛出数据最小化的 `EvaluationError / AL6002`。

每个纯运算输入及生成结果上限为 1 MiB 的展开 JSON，深度最多 64。记录重复引用同一个大值时，按展开后的每份数据计量，不能通过共享宿主对象逃过限制。record 在大小检查后创建独立的 JSON 数据；concat 也先检查可能的长度增长。这是局部数据上限，不是完整进程内存或 CPU fuel 配额；后者仍需实现。

and 与 or 接收已经求值的两个节点，不提供隐式短路效果。需要只执行其中一个带效应计算时，使用显式 if 分支。纯操作不能访问网络、时间、随机数、文件系统、反射或任意宿主函数。

## 类型、证据和标签

纯操作只接受 Data、Candidate、Observation、Checked。派生结果遵循：有 Candidate 输入则为 Candidate；否则有 Observation 则为 Observation；其余为 Data。输出数据 schema 由操作确定。

从 Candidate 读取字段、拼接或重建记录，不能把它变成普通可信数据。对 Checked 做投影或变换不会把原对象的谓词回执附加到新结果；新的值没有原对象的检查证据。即使变换后碰巧与原值相等，也不自动继承证据。

标签规则独立于这套类型分类。输入的保密限制和 taint 合并到结果及当前控制上下文；成功的纯计算不会允许后续常量发送抹去已读取的敏感依赖。重新检查、比较为 true 或进入某个分支，都不是解密或完整性提升操作。

## 条件分支

```text
let base = field(tested, "base_version");
let expected: Int = 7;
let allowed = eq(base, expected);
let answer = if(allowed, apply, reject, {patch: tested});
```

apply 与 reject 是声明的 workflow；其输入通过最后的捕获映射绑定。分支参数可以只使用映射中自己声明的部分，但映射中的每项必须被至少一个分支声明。不同于 match_task，if 不保留名为 result 的自动参数。

IR 必须同时包含 Then 和 Else。条件 schema 必须为 Bool，可以是 Data、Candidate、Observation 或 Checked 包装；没有 truthiness 转换。允许按候选布尔值选择路线并不等于认可它：条件标签进入 pc，带模型 taint 的条件不能通过常量分支绕过要求严格完整性的 sink。

两个分支分别通过共享检查器，效应必须落在父边界内，推测策略递归继承。两个分支返回的类型、schema 和谓词集合必须一致。分支可创建并处理自己的许可、操作和子任务；返回 Operation 时父计划仍须处理或显式返回它。

后续已扩展 Intent、Permit、Operation 和 OpRef 捕获：Permit 与 Operation 在每条路径上显式移入分支，Operation 可返回，OpRef 可复制观察。规则见 [分支资源传递](18-resource-branches.md)。还已加入 [Budget 分支移动与工作流调用](20-budget-workflow-calls.md)；跨任务资源与 [固定次数资源循环](38-checked-affine-loops.md) 后续已接通；Task/Scope 的一般参数传递仍待实现。

## 恢复语义：冻结选择和输入

条件可能来自会更新的外部操作快照。例如首次执行时 work 是 Unknown，稍后回执把它变为 Applied。若每次重放都重新决定 if，程序可能在同一运行里先走失败路线，再执行另一条成功路线；若只保存布尔值而重读捕获，则原路线还可能收到不同参数。

新表 `branch_choices` 以 `(run, activation, node)` 为键，原子保存：

- 首次 Then/Else 选择及带标签的条件值；
- 捕获的数据、证据引用及标签；
- 选择时的控制标签。

保存成功后才进入所选分支。事务提交前崩溃不会留下选择或分支效果，恢复可按新的观察选择；提交后崩溃必须复用原选择和捕获。后来的当前控制标签仍与历史标签合并，恢复不会借旧记录放宽信息流限制。

子任务用稳定 task ID 作为 activation；嵌套条件分支沿用带分支路径的稳定节点位置。不同任务中的同名 if、父子分支中的同名工具节点不会混用决策或操作。取消及旧 worker 代次检查也应用于新的分支记录；已经结束的 run 不能创建新的决策。

这是“首次观察后固定”的业务流程语义。需要基于新事实重新规划，应创建明确的新程序/激活或未来的重新规划协议，不能通过重放旧 if 达成。运行时仍会重建分支之前的节点；该决策记录不是整个程序的通用 checkpoint，也不取消此前节点的错误检查或未决责任。

schema 升至 9，新增表不改写既有账本。支持从原有受支持版本 2 至 8 迁移；未含新节点的既有合法计划指纹保持原样。新节点不能由旧检查器接受，因此没有把新程序降级到旧运行时的兼容承诺。

## 三个入口

```python
base = b.field('base', tested, 'base_version')
expected = b.literal('expected', 'Int', 7)
allowed = b.compute('allowed', 'eq', base, expected)
answer = b.if_else('answer', allowed, then=apply_plan,
                   otherwise=reject_plan, inputs={'patch': tested})
```

源码使用前缀运算，如 add(a,b)、eq(a,b)、not(flag)、concat(a,b)，并将它们 lowering 到 compute。字段与记录写为 `field(value,"name")`、`record("Row",{n: number,text: text})`。这些表达式仍引用已命名节点；内联表达式树、运算符优先级及通用纯函数尚未实现。

可运行示例：

```powershell
python -m al0.frontend examples/conditional_repair.al0 --demo-catalog --entry conditional-repair
python -m examples.conditional_repair
```

示例源码与 SDK 的规范化 IR 相同，结果为 Done，分支选择记录为 1，确认费用为 4，模拟仓库修改一次。用 SDK 恢复源码执行的同一 run，新增事件为 0。

## 验证范围

`tests/test_control.py` 覆盖全部纯运算、schema 与参数数量拒绝、整数边界、重复引用的记录体积、候选传播、检查证据失效、敏感控制流、未选分支不执行、嵌套位置、不同任务 activation、分支返回操作的责任、推测写入拒绝和 v8 迁移。

状态变化测试先从 Unknown 选择 Else 并捕获 before，再写入 Applied 回执，重放仍使用原分支及 before，没有重复工具调用。独立进程在 `before_branch_commit` 和 `after_branch_commit` 用 `os._exit(77)` 退出：前者恢复后可以选择新观察对应的 Then/after，后者必须保留 Else/before；两个场景都只执行一次分支日志调用。

这些测试验证具体实现的边界和恢复行为，不构成整个条件语言的形式证明。后续已加入 [纯函数、有界循环和共享步数额度](17-functions-loops-and-fuel.md)；一般仿射资源捕获/返回、完整内存管理及组合故障模型仍待完成。完整测试结果见 [实现状态](08-implementation-status.md)。
