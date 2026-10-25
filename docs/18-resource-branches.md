# 分支资源传递与操作观察借用

更新日期为 2026 年 10 月 25 日。本轮为 `if` 与 `match_task` 增加 Intent、Permit、Operation 和 OpRef 参数，连接既有授权、接纳和未决责任检查。源码与 SDK 仍生成同一受检 IR。

## 描述、许可、责任与观察引用

| 当前类型 | 分支输入的含义 | 父作用域在分支后 | 可用操作 |
| --- | --- | --- | --- |
| `Intent<"send">` | 复制不可变动作描述，保留参数、检查回执和标签 | 仍可使用 | authorize、与匹配 Permit 一同 admit |
| `Permit<request>` | 移动绑定于 request 的许可 | 原引用不可再用 | admit；也允许仿射丢弃 |
| `Operation<"send">` | 移动操作处理责任接口 | 原接口不可再用 | inspect、await、reconcile、借出 OpRef、作为结果返回 |
| `OpRef<"send">` | 复制只读观察引用 | 仍可使用，也可复制给多个参数 | inspect、再次借用、作为结果返回 |

`Operation` 是当前 IR 对操作处理责任的显式表示；`OpRef` 对应核心草案中的可复制观察引用。前者的移动不转移数据库资金，也不删除外部操作。后者不能派发请求、启动付费对账或清除其来源 Operation 的处理义务。使用 `borrow_operation(operation)` 创建只读引用，不存在从 Json 或字符串构造保护类型的语法。

这一区分允许已有观察者在责任接口移入另一分支后继续查询状态。移动接口与取消远端工作是不同操作；不论接口放在哪个分支，已暴露 attempt 的预留和迟到收费仍由原账本负责。

## 依赖于 Intent 的 Permit 参数

```text
workflow submit(request: Intent<"send">, permission: Permit<request>)
-> Operation<"send"> uses {"admit:send@output"} {
    let operation = admit(request, permission);
    return operation;
}
```

Permit 的类型实参是同一声明中更早的 Intent 参数名。它不只是工具名：两个针对 send 的不同 Intent 不能交换许可。静态检查把分支内的参数绑定替换为父捕获的 Intent 身份，再核对 Permit 的来源绑定。接纳事务继续核对实际 intent digest、主体、run、授权记录和许可消费状态。

IR 保留已有 parameter 节点结构。资源参数的 kind 为 Intent、Permit、Operation 或 OpRef，type 分别存工具名或依赖的 Intent 参数名，predicates 必须为空。保护类型不会因在参数表中声明就获得宿主授权。根入口与任务输入仍只接收数据和检查证据；资源参数只由受检分支捕获建立。

SDK 提供 `intent_parameter`、`permit_parameter`、`operation_parameter`、`opref_parameter` 和 `borrow_operation`。源码支持相同类型及借用操作，错误仍由共享检查器定位。

## 分支资源合并

设分支捕获集合为 C，其中 Permit 和 Operation 组成移动集合 M。

1. 捕获必须引用当前未移动的绑定。M 中不能让同一引用以两个参数名进入分支。
2. 每个分支都必须显式声明 M 中的参数。这样控制流不会隐式地把未选择路径的许可留在父作用域中。
3. 对各分支独立检查类型、传递效应和责任。Operation 参数必须通过穷尽业务状态匹配处理，或作为该分支结果返回；Permit 可以消费或丢弃。
4. 各分支返回相同类型与证据。返回 Operation 时，父作用域在分支结果绑定上重新承担处理义务。
5. 检查通过后，从父的可用接口中移除 M。已移走的引用不能用于后续操作或 return。

数据、Intent 和 OpRef 可复制。它们可由部分分支接收，但每项捕获至少被一个分支使用。对 OpRef 的观察结果仍要穷尽处理；这不代替来源 Operation 的责任处理。

若父作用域已经生成一个尚未匹配的 Snapshot，必须先处理它，再移动关联 Operation。这避免把未处理的旧状态与新责任归属混在一起。Borrow 的 Snapshot 和原 Operation 的 Snapshot 具有不同静态责任归属。

在抽象判断中，可将 Permit 放入独占环境 Δ，将 Operation 的责任放入独立环境 O，将 Intent 与 OpRef 放入可复制环境 Γ。分支入口分割 Δ 和 O，分支返回的 Operation 在父的 O 中建立新的绑定。现有检查器分别以 consumed、pending_operations 和 pending_statuses 实现这些约束。它尚未构成带别名、异常、并发和持久状态的完整 preservation 证明。

## 持久化和恢复

Bool 分支首次选择时，在同一 branch_choices 事务中保存条件、资源捕获和控制标签：Intent 保存规范化描述，Permit 和 Operation/OpRef 保存既有保护引用。Intent 中的检查回执和信息流标签原样保留。恢复只在静态 Intent 参数位置重新构造描述；普通 Json 参数不触发保护对象构造。

分支的接纳 site 由静态路径决定。恢复从授权记录取得同一许可，再按分支快照进入同一路径；若许可已消费，接纳先找到原 slot 对应的 operation，不会再次消费或创建另一笔预留。TaskOutcome 分支沿用已持久化的 outcome 和固定输入恢复机制。

schema 仍为 10；这次扩展使用既有 JSON 分支记录，不增加数据库表。既有合法程序的 IR 和指纹保持不变；新增类型和操作会进入新程序的指纹。

资源捕获不会提升信息流权限。若 Intent 在较弱控制标签下准备，随后进入敏感条件分支，admit 仍会拒绝不兼容的控制上下文。检查证据不去除标签；观察状态带来的控制标签也继续限制后续派发。

## 验证与复现

```powershell
python -m examples.resource_branches
python -m unittest tests.test_resources -q
```

示例的源码位于 `examples/resource_branches.al0`。先把许可移入提交分支，再把返回的 Operation 移入处理分支。源码与 SDK 的 IR 完全相同：结果为 Done，外部发送一次，确认费用 1、预留 0，两个分支选择，恢复新增事件 0。

专项测试覆盖静态重复消费和重复捕获、Intent 错配、移动后返回、未处理 Snapshot、丢失操作责任、许可丢弃、只读引用复制与权限限制、嵌套移动、TaskOutcome、任务内分支、检查回执、保密标签、Unknown 的费用预留，以及根输入和任务输入中的保护对象伪造拒绝。

四处独立进程崩溃点分别为 before_branch_commit、after_branch_commit、after_permit_consumption 和 after_admit_commit。恢复后的独立调用日志均只有 hello 一行，确认费用为 1，预留为 0。测试验证当前受检程序和单存储实现；完整测试数与耗时见实现状态文档。

## 后续范围

后续已实现 [Admission 与 ReturnedPermit 分支](19-admission-outcomes.md)，以及 [Budget 参数、分支移动、返回和带资源工作流调用](20-budget-workflow-calls.md)。后续已实现 Cap 参数、跨任务仿射参数和 [具名资源包正常返回](31-checked-resource-bundles.md)；固定次数资源循环后续已在 [受检资源循环](38-checked-affine-loops.md) 接通；Task/Scope 的一般参数传递仍未实现。当前 Permit 的丢弃禁止程序继续使用该引用，不自动撤销宿主数据库中的授权记录。一般业务 sum type 后续已在 [业务 Variant](34-checked-business-variants.md) 接通；完整的资源退出协议仍属于语言目标。

完整 CPU/内存隔离、ContextView、授权解密与完整性提升、组合语义证明，以及同运行时 SDK 的收益实验继续保留在总体路线中。
