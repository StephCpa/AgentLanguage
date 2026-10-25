# 持久 Admission 与许可返还

更新日期为 2026 年 10 月 25 日。本轮实现 `try_admit` 和穷尽的 `match_admission`，让受检程序区分接纳成功、确认未接纳且仍可返还许可，以及确认未接纳但不返还许可。接纳结果与业务结果是两层状态。

## 语言接口

```text
let admission: Admission<"send"> = try_admit(request, permission);
let result = match_admission(admission, {
    Admitted: accepted,
    Returned: returned,
    NoPermit: denied
}, {request: request});
```

Admission 的动态值仍使用草案中的两层结构：

```text
Admitted { operation }
Rejected { reason, permit: ReturnedPermit | NoPermit }
```

当前表面语法把第二层展开成 Returned 与 NoPermit 分支。每个结果还保存产生决定时的控制标签，与 Intent 标签合并。reason 是只含稳定错误码的 Json，不包含动作正文或内部异常消息。

| 分支 | 保留参数 result | 保留参数 reason | 静态要求 |
| --- | --- | --- | --- |
| Admitted | `Operation<"send">` | 无 | 必须声明 result，并处理或返回操作责任 |
| Returned | `Permit<request>` | 可选 Json | 必须声明 result，且 request 捕获必须是原 Intent |
| NoPermit | 可选 Json，内容为拒绝原因 | 可选 Json | 不提供 Permit 或 Operation |

调用者不能用 captures 覆盖 result 或 reason。Returned 分支先声明 Intent 参数，再声明依赖它的 Permit 参数；许可仍绑定原动作，不能借失败分支改绑参数或工具。所有分支必须返回相同类型和检查证据。

`try_admit` 消费原 Permit 引用，产生必须恰好匹配一次的 Admission。只有 Returned 分支能获得新的可用 Permit 绑定。未匹配 Admission、重复匹配、继续使用原 Permit、把 NoPermit 当作许可，以及静默丢弃 Admitted 的 Operation，都会被共享检查器拒绝。Admission 不能由 Json 构造，也不能作为普通函数或任务参数传递。

完整源码和 SDK 对照见 `examples/admission_outcomes.al0` 与 `.py`。

## 一个 slot 只能形成一个决定

数据库 schema 11 增加 admission_decisions，主键为 `(run,node,activation)`，保存规范化 Intent、输入 Permit、资金 allocation 和结果。请求中任一绑定改变，都会触发 ReplayMismatch。

新请求在 `BEGIN IMMEDIATE` 内执行：

1. 验证主体、run、任务租约、资金归属和生命周期。
2. 若已有决定，读取原结果；若该 slot 已有旧版成功操作，返回相同 Operation。
3. 在保存点内执行既有接纳检查、预算预留、许可消费、operation/attempt/outbox 写入。
4. 成功时把 Admitted 决定与操作共同提交。可分类拒绝时，先回滚保存点，再写入 Rejected 决定。
5. 提交后才向解释器返回结果。

因此，拒绝也占据 slot。后来增加可用余额、收到退款或恢复授权，都不会把该 slot 改成成功。若获得 ReturnedPermit，程序必须在新的静态接纳位置尝试使用。返回同一许可身份，不创建第二张授权票据。

关键实现不变式为：

- 每个接纳 slot 至多有一个决定；已登记 Rejected 的 slot 不存在对应 Operation。
- 新 Admitted 决定和许可消费、预算预留、Operation、首次 Attempt、Outbox 同事务存在。
- 返回许可之前，失败尝试的预留和消费均已回滚。
- 相同 Permit 在不同 slot 的并发接纳至多成功一次；其余尝试不返回已消费许可。
- 重放读取历史结果。历史 ReturnedPermit 即使已经在后续位置消费，原决定也不被改写；后续位置根据自己的 slot 恢复原 Operation。

这些规则由事务、静态消费检查和回放位置共同实现。仅复制一个结果字典不是重新授权。

## 哪些拒绝返还许可

| 拒绝 | 当前结果 |
| --- | --- |
| 完整费用上界超过可用余额，且许可复核仍有效 | Rejected + ReturnedPermit，AL1002 |
| 过期、撤权、授权代次变化、许可与 Intent 不匹配 | Rejected + NoPermit，AL1000 |
| 许可已被其他 slot 消费 | Rejected + NoPermit，AL1001 |
| 检查证据不符合动作 | Rejected + NoPermit，AL2002 |
| 动作出口或控制标签不兼容 | Rejected + NoPermit，AL7001 |

预算失败后的许可复核发生在回滚之后、同一写事务之内。它再次核对消费状态、当前授权、期限、证据及出口策略，覆盖期限恰好在首次检查与返还决定之间跨过的情况。ReturnedPermit 只表示决定时可返还；在新 slot 接纳时仍要重新检查当前授权和预算。

主体认证、错误 allocation、旧工作线程租约、已结束或因契约违规停止接纳的运行、未知工具契约，以及未分类内部错误，继续抛出运行错误。它们不生成可供程序处理的正常拒绝。数据库 I/O 异常和提交结果不确定也不被转换成 ReturnedPermit：恢复必须查询同一 slot 的权威状态。当前实现是本地 SQLite 协调器；远程接纳查询及显式 AdmissionUnresolved 传输协议仍待实现。

## 接纳失败与业务失败

Admitted 后的 Timeout、Unknown、Pending、ProvenNoEffect 和费用结算都仍属于该 Operation。

例如，远端业务明确拒绝但收费 1，语言进入 Admitted 分支，再从业务 Snapshot 处理 ProvenNoEffect；它不会进入 Returned，也不会返还已消费的许可或退还该费用。请求已暴露而结果未知时，预留保持，重放不重新发送。

## 信息流与分支恢复

Admission 决定保存 Intent 与当时控制上下文的合并标签。敏感上下文中的拒绝本身也保持敏感；恢复到较弱的当前上下文不会抹去历史标签。

`match_admission` 使用现有 branch_choices 表，首次进入时固定所选分支、结果、捕获和控制标签。恢复不重新挑选分支，也不以更新后的普通数据替换已捕获值。ReturnedPermit 在分支内使用时仍受当前控制上下文约束。

任务内 Admission 使用任务稳定 activation、任务 allocation 和当前 worker lease。拒绝不会绕过任务 fencing，也不会把子任务资金归还到根运行。

## 兼容性与迁移

schema 10 至 11 在事务中创建决定表，保留已有 Operation、Permit 消费状态与账本。支持的更早版本仍通过现有迁移路径升级。新增 IR 操作参与程序指纹；已有合法程序的 IR 与指纹不改变。

原 `admit` API 保留返回 Operation、失败时抛出异常的接口。它也不能穿过一个已经由 try_admit 登记的拒绝 slot。旧 admit 自己的异常不建立新的类型化拒绝记录；需要持久拒绝分支的程序使用 try_admit。由旧 admit 成功建立的 slot 可经 try_admit 读取，身份检查后返回同一个 Operation。

## 验证

```powershell
python -m examples.admission_outcomes
python -m unittest tests.test_admission -q
```

示例的三种场景中，成功路径返回 Done、发送一次、确认费用 1；预算不足返回 Deferred、不发送；授权后撤权返回 Denied、不发送。三种路径恢复后新增事件均为 0，源码与 SDK 的 IR 相同。

专项测试覆盖余额恢复后的固定拒绝、新 slot 复用 ReturnedPermit、返还后撤权、跨期限复核、不同主体和旧工作线程租约、并发同 slot 与不同 slot、保存点部分更新回滚、未分类内部异常、业务拒绝费用、Unknown 预留、静态伪造和重复消费、信息流标签以及 v10 迁移。

六组独立进程退出覆盖成功路径的决定提交前后、许可消费后和接纳提交后，以及拒绝路径的决定提交前后。恢复后成功场景发送一次并确认费用 1；拒绝场景始终不发送。测试总数与最终全量结果见 [实现状态](08-implementation-status.md)。

`models/admission_audit.py` 另外枚举一个许可、两个 slot、退款和撤权的所有可达状态。持久拒绝版本为 38 状态、51 转移，没有违反所检查的不变量；忘记拒绝的版本为 50 状态、85 转移，出现 12 个违反“拒绝不重开”的状态。最短反例依次为预算拒绝并返还许可、先前预留退款、原 slot 接纳成功。模型把接纳视为原子步，数据库事务对应、标签和远程提交不确定仍需其他证据。

## 剩余工作

本轮完成受检 Admission 分支和单存储许可返还。后续已加入 [Budget 参数与带资源工作流调用](20-budget-workflow-calls.md) 和 [Cap 权限衰减与受限调用](21-capabilities-and-attenuation.md)。[受检任务资源](29-checked-task-resources.md) 已支持跨任务句柄移动，并覆盖 Admission 返还许可的再次转交；[受检资源包](31-checked-resource-bundles.md) 进一步支持未消费 Permit 和其他资源一起正常返回；一般带资源 sum type、循环资源传递及远程 AdmissionUnresolved 仍未完成。ContextView、完整隔离、组合形式验证和同运行时 SDK 收益实验继续保留在总体目标中。
