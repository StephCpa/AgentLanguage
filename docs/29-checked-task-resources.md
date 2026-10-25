# 受检任务的仿射参数与单资源返回

更新日期：2026 年 10 月 26 日。

[上一阶段](28-durable-resource-custody.md) 实现了可信 Store 的资源转移与返回暂存。本阶段将其中一条完整路径接入共享 IR、Python SDK、源码和 TaskRunner：任务可以同时接收多个命名资源，并通过显式 join 返回一个 Operation 或 Budget。后续 [受检资源包](31-checked-resource-bundles.md) 已支持多资源返回和包内未消费 Permit；资源结果分支仍待实现。

## 已接通的接口

| 边界 | 已实现行为 |
| --- | --- |
| spawn / spawn_with / spawn_caps | 数据、检查证据、Cap 和 Intent 可作为显式输入；Permit、Operation、Budget 按名称原子移动 |
| Permit 参数 | 必须引用同一子程序较早声明的 Intent 参数；父方检查具体 Intent 身份，运行时检查 Permit 绑定的完整 Intent |
| 子任务返回 | 数据、证据、Cap 沿既有路径返回；Operation / Budget 发布到 Escrow |
| 显式 join | 原子接收资源并返回带新 generation 的 Value；新增父方的操作或预算处理义务 |
| 嵌套任务 | 转移和接收使用父 TaskLease；固定权限表继续约束动作资源 |
| 重放 | 重建原输入代次和历史接收回执，不查询当前 owner 来刷新旧引用 |

原有语法足以表达这条路径，不增加仅为演示服务的专用语句：

```text
let task = spawn_with(group, funded, {}, {
    funds: funds, request: request, permission: permission
});
let operation = join(task);
```

任务接收后，父程序不能再次使用已经移动的 funds 或 permission。共享检查器同时拒绝重复别名、错误 Intent 配对、未处理快照下的操作移动，以及 join 后遗留的 Operation / Budget 义务。只读 OpRef 仍不能跨任务传递。

Python 的 `CheckedPlan[T]`、`Task[T]` 和 `join -> Ref[T]` 保留返回类型；新增严格类型正例覆盖 `Task[Operation[str]]`。命名输入字典的精确参数配对、仿射单次使用和 Intent 身份由共享 IR 检查器验证，不把它们计作 mypy 的保证。

## 持久值与执行权限

`Value` 可以附带 ResourceRef。其资源种类、ID 和 generation 可进入持久调用帧及分支记录；ID 必须与值一致，资源记录不能附带数据检查回执。普通数据参数不能通过添加该字段变成资源参数。

任务的普通输入和资源移动记录仍分别保存。TaskRunner 根据受检参数类型重建 Intent，并从转移记录重建资源 Value；Store 对照这一固定输入集合检查恢复请求。原始 task_inputs 和 task_resource_inputs 查询接口保持各自含义。

解释器仅为具有新任务资源边界的计划启用 custody 执行路径。它保留创建、输入和接收产生的引用，在每个节点执行时呈交；Store 继续在实际事务内核对 actor、TaskLease、generation、grant 和资金。内部 authorize→admit 调用可以使用同一事务创建的 generation 0 引用；历史创建记录也只重建 generation 0，不提升到资源当前代次。

TaskRunner 完成任务时发布资源。直接读取 Completed 结果只得到历史观察，其中的子方引用不能越过 Escrow。TaskScope.join 接收后用固定接收回执替换返回引用。返回资源再次移走后，重放旧 join 仍返回旧回执。

Admission 的 Returned 分支保留原 Permit 的转移代次；它可以再次传入下一级任务。此前简单地重建裸 ID 会丢失该代次，现已覆盖这条组合路径。

预算 split 的固定键重放、已完成 budget join 的重放先读取历史结果，再判断是否产生新工作。读取不会重新划拨资金或授予当前控制权；新的 split 仍检查现有引用。数据库结构保持 schema 17，既有合法计划的指纹与普通记录格式保持不变。

## 标签与退出

任务控制标签合并所有普通及资源输入标签；重建资源输入时附上固定控制标签。返回值再合并任务的保守输出标签。该规则可能扩大限制，但不会因把资源从数据记录拆开保存而丢失保密限制或 taint。

任务接受资源后仍可返回普通数据，并通过既有 Failed / Waiting / Cancelled 观察协议交出未完成责任。新增测试验证：适配器回复丢失后，Waiting 与任务清理保留 exposed 预留；晚到账单仍按原 attempt 结算。执行失败时，当前持有的预算被 Runtime 接管。

本阶段最初要求资源返回显式 join 或 handoff；后续 [资源任务结果](32-resource-task-outcomes-and-race.md) 已接通 Operation / Budget / Bundle 的 TaskOutcome 与 race。可信 Python TaskScope 仍拒绝隐式丢弃资源返回。

## 验证证据

```powershell
python -m examples.typed_task_resources
python -m unittest tests.test_typed_task_resources -q
```

[源码](../examples/typed_task_resources.al0) 与 [SDK 示例](../examples/typed_task_resources.py) 生成相同 IR：父方发送 Permit 和 Budget，子方在指定预算下接纳，关闭资金接口后返回 Operation；父方接收并关闭来源 scope，然后派发。独立 SQLite 端点记录一次调用和一次收费，根预算确认费用 1、余额 9、预留 0；恢复新增事件 0。[结果记录](../models/typed_task_resources_example.json) 保留实际输出。

30 项专项测试覆盖上述路径，以及新建资源返回、嵌套接收、再次转移、过期历史回执、信息流限制、类型/别名拒绝、Admission 返还后的再次转交、Failed / Waiting 责任处理和宿主显式接收。8 个独立进程退出位置覆盖 spawn、publish、receive 的事务内与提交后，以及接纳提交后和预算归还提交后；每次恢复均只派发一次并完成费用结算。

完整回归 631 项全部通过，用时 387.501 秒，启用了固定 mypy 门槛，日志见 [typed_task_resources_full_regression.log](../models/typed_task_resources_full_regression.log)。基础类型正例增至 15 个，另有 5 个生成绑定正例和 37 个指定负例。安装包在源码目录之外通过类型检查，并实际执行任务 Budget 转移、返回、归还和无新增事件的重放，见 [sdk_wheel_audit.json](../models/sdk_wheel_audit.json)。这些证据不等同于有限模型与运行时的完整对应证明；当前总体边界见 [实现状态](08-implementation-status.md)。

## 下一步

后续 [具名多资源结果包](31-checked-resource-bundles.md) 已接通数据、Intent、未消费 Permit、Operation 和 Budget 的一起返回、字段依赖与标签及解包义务。后续 [资源任务结果](32-resource-task-outcomes-and-race.md) 已明确 Completed/race winner 接收和 Failed/Waiting/Cancelled 责任移交。一般业务 Variant 已在 [受检业务 Variant](34-checked-business-variants.md) 接通；固定次数资源循环已在 [受检资源循环](38-checked-affine-loops.md) 接通。

ContextView、解密/完整性提升协议、独立 Root worker fencing、完整模型对应、真实工作负载及 SDK 对照实验仍在原目标范围内，尚未完成。
