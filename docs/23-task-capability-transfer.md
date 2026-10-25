# 跨任务 Cap 传递与持久权限表

更新日期为 2026 年 10 月 25 日。Cap 现在可以作为显式任务输入及结果；新的 spawn_caps 同时指定子任务的环境权限表。任务创建持久化程序、输入、预算与权限表，使恢复使用原授权边界。

## 语言接口

```text
workflow narrow(permission: Cap<"send">) -> Cap<"send">
uses {"cap:send@outbox"} {
    let reports = attenuate(permission, {
        max_attempts: 1, constraints: {channel: "reports"}
    });
    return reports;
}
```

父工作流可显式创建这个子任务：

```text
let base = capability("send");
let group = scope({}, false);
let task = spawn_caps(group, narrow, {}, {send: base}, {permission: base});
let reports = join(task);
let closed = close_scope(group);
```

SDK 对应 `spawn_caps(name, scope, plan, budget, caps=..., inputs=...)`。budget 继续接受字面量或运行时向量。Cap 参数用 `cap_parameter` 声明；join 返回的 Cap 可以继续 attenuate、authorize_with 或传给另一个受限调用。

普通 spawn/spawn_with 也可以传入 Cap，并将创建处的完整环境权限表固定为子任务的权限表。spawn_caps 则使用显式表完整替换它，空表表示没有环境工具权限。两种方式都没有子任务自由变量捕获。

## 三层检查

编译时检查 Cap 的工具类型、实参集合及传递效应。spawn_caps 的每个键必须与对应 Cap 的工具相同；本阶段任务实参只开放普通数据、检查证据和 Cap，任务结果只增加 Cap；后续 [受检任务资源](29-checked-task-resources.md) 独立接入了 Budget、Permit、Operation 的移动和单资源返回。

创建时检查显式 Cap 参数：它必须是子任务表中同工具的授权本身或其后代。较宽的父 Cap 和不属于该派生链的兄弟 Cap 都被拒绝。检查在创建子任务及其预算分配之前完成，失败不会遗留一个已创建的子任务；外围作用域依原生命周期退出。

执行入口再次核对持久输入和权限表，Cap 参数还要匹配同一 run、工具及 scope。任务不能利用一个伪造的普通 Json 值获得保护类型。根入口仍拒绝宿主 `Value("grant-id")` 作为 Cap 参数；可信任务宿主接口则必须给出真实授权引用，并通过任务输入和边界检查。

Cap 返回值在完成前及恢复读取时核对声明工具、scope 和持久权限表中的后代关系。TaskRunner 不接受子任务返回一项更宽的工具授权。这里判断的是结构包含关系；真正的新授权、接纳、派发和重试另行复核整条授权链的当前活性。

## 持久权限与恢复

schema 15 增加 task_capabilities：

```text
task_capabilities(task_id, canonical_tool_to_grant_map)
```

通过 TaskScope 创建任务时，权限表与 task、allocation、task_inputs、task_flows 在同一个 spawn 事务写入。提交前崩溃看不到这些新记录；提交后恢复原任务，不再次划分资金或绑定一个新权限表。TaskCapabilitiesBound 事件属于该事务。

相同 spawn key 的程序、预算、输入、控制标签及权限表必须一致。更改权限表会报 ReplayMismatch。任务中的普通 invoke、普通 spawn、分支及循环继续使用这份权限表，嵌套子任务再保存自己的表。

TaskRunner 恢复正在执行的任务时，先读取或核对权限表，再领取新 worker epoch。更宽、较窄或仅换成另一个 grant ID 的表都会被拒绝，不会先把旧 worker 作废。`TaskRunner.run(..., grants=None)` 可直接使用已有持久表；Interpreter 的任务入口也核对表，不能绕过 TaskRunner 改用宿主权限。

工具权限的持久身份与当前有效性不同。父授权撤销后，子 Cap 不能开始新的授权或派发；已经完成的 Cap 结果仍可恢复为历史引用。旧引用随后用于新动作时仍会失败。期限、父 epoch 和参数约束延续 [Cap 权限衰减](21-capabilities-and-attenuation.md) 的规则。

## 返回、结果分支与 race

| 方式 | Cap 的处理 |
| --- | --- |
| join | 成功时返回同一 Cap 引用及标签 |
| join_outcome / match_task | Completed 分支的 result 类型可为 Cap；其他分支仍是普通失败/等待/取消数据 |
| race | 各候选须返回同一工具的 Cap，持久 winner 决定返回引用 |
| handoff / cancel_outcome | 继续按任务状态处理控制权，不将普通状态 Json 转成 Cap |

返回的 Cap 可在创建它的子任务及作用域结束后使用，因为授权链归属于 run；它不依赖已结束的 worker lease。复制 Cap 不增加任何额度，也不复制 Permit。每操作尝试上限与根预算仍分别由许可和账本执行。

推测任务可以派生和返回 Cap，但其子计划仍不能请求被禁止的共享写入效应。父程序可以在持久选择之后使用返回的能力发起动作，届时需重新通过当前授权、证据、标签和预算检查。

Cap 的输入标签、创建控制依赖和任务结果标签一同保留。join、结果分支与 race 不解密数据，也不清除完整性 taint。任务取消会阻止旧 worker 开始新工作；它不是对整个授权树的隐式撤销协议。

## 宿主低层接口与旧数据

`Store.spawn_task(..., caps=...)` 可由可信宿主在创建时保存权限表。为保留低层生命周期操作，未传 caps 时仍可创建一个尚无权限表的任务；它不能直接进入新解释器执行。

对于状态为 Ready、epoch 为 0、尚未启动且仍 Attached 的任务，`bind_task_capabilities` 可以提交首次表。TaskRunner 在该情形下可使用宿主明确传入的 grants 完成绑定，然后才领取 worker。绑定事务提交前后的进程退出均可恢复。

v14 到 v15 的结构迁移保留旧任务及账本，但不能从缺失的数据推导旧权限表。已经 Running 或 Waiting 的旧任务若没有表，恢复执行会报 StateConflict；不能拿恢复时的宿主 grants 猜测原边界。已完成的普通数据结果仍可通过 TaskRunner 读取，未决业务和费用证据仍按原协议处理。通过父 TaskScope 重放旧任务也必须具备相符的权限记录。

因此，此次结构迁移不等于所有旧运行都可透明续跑。带证据的旧权限导入与一般运行迁移协议仍属于后续工作，不能通过关闭检查解决。新受检任务在创建时已保存必要数据。

## 可运行证据

```powershell
python -m examples.task_capabilities
python -m unittest tests.test_task_capabilities -q
```

[源码示例](../examples/task_capabilities.al0) 与 [SDK 示例](../examples/task_capabilities.py) 生成相同 IR。宿主提供 send 和 admin，子任务的权限表只有 send。子任务把 send 限定为 reports 频道和每操作最多一次尝试，再把 Cap 返回给父程序。父程序发送一次，确认费用 1、预留 0；恢复结果仍为 Done，新增事件为 0。

31 项专项测试覆盖显式输入与返回、较宽实参拒绝、权限表替换、直接解释器入口、同运行与工具绑定、嵌套任务、动态预算、结果分支、race、标签、推测策略、撤销后历史恢复和 v14 迁移。八组独立进程退出覆盖 spawn、子 Cap 派生、任务完成、宿主首次权限绑定的事务前后。

全量测试证据见 [实现状态](08-implementation-status.md)。后续 [schema 17 可信 Store 接口](28-durable-resource-custody.md) 已接通 Permit/Operation/Budget 的控制权移动及暂存返回。后续 [受检任务资源](29-checked-task-resources.md) 已接通命名参数、单资源返回及 TaskRunner；[受检资源包](31-checked-resource-bundles.md) 又接通了多资源正常返回；循环资源状态、资源结果分支、完整组合证明及真实工作负载对照仍未完成。
