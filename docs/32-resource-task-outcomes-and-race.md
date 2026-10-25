# 资源型 TaskOutcome 与 race 的原子接收

状态：已接入共享 IR、Python SDK、源码、TaskScope、解释器和持久 Store；数据库结构保持 schema 17。正常资源包见 [受检资源包](31-checked-resource-bundles.md)，整体协议设计见 [资源结果包设计](30-resource-result-bundles.md)。

## 接口与静态义务

`join_outcome`、`cancel_outcome` 现在接受返回 Operation、Budget 或 Bundle 的任务。Completed 分支得到任务声明的结果类型；Failed、Waiting、Cancelled 分支只得到状态说明，不重新构造资源包。

```text
let outcome = join_outcome(task);
let report = match_task(outcome, {
    Completed: completed,
    Failed: fallback,
    Waiting: fallback,
    Cancelled: fallback
}, {});
```

资源型 Completed 分支必须声明 `result` 参数。该参数按原有仿射规则移动、处理、归还或返回；不能用一个忽略结果的分支消除资源责任。一次匹配消费资源型 TaskOutcome，重复匹配拒绝。未匹配的 outcome 不能作为普通 JSON 使用或直接从工作流返回。

四个分支仍须返回相同的类型与证据。成功分支可以处理完资源后返回普通报告，其他分支返回同类型的失败报告。一般用户定义的带资源 sum type 尚未实现，因此本阶段不宣称任意分支可以返回不同结构的资源包。为保持已有数据程序行为，普通数据型 TaskOutcome 不额外加入资源型的一次匹配限制。

`race` 接收同一推测作用域中、返回相同封闭类型的任务。结果为胜者的 Operation、Budget 或 Bundle；恢复该结果的处理义务，不能只消费候选句柄而遗忘胜者资源。原有共享写入限制继续生效：传入已经存在的 Operation 可以由无行动效应的候选转交，但推测任务不能因此派发不允许的行动。

SDK 的 `TaskOutcome[Bundle]`、`Task[Bundle]`、`Ref[Bundle]` 保留类别信息。本阶段沿用现有 52 个公共构造方法。具体字段类型、完整匹配和资源单次使用仍由共享 IR 检查；Python 类型检查不被当作仿射证明。

## 完成观察的事务

新的接收路径按以下顺序在一个 Store 事务中执行：

1. 读取任务终态和既有 outcome 决定；若已有决定，返回固定记录。
2. 新决定验证接收方 actor、所属作用域及父 TaskLease。资源接收必须有显式 access context。
3. 对 Completed 规范化完整值记录，核对其中的资源路径和身份与暂存清单相同。Bundle/resource 元数据保留；不再把结果降为只有 data/receipts 的记录。
4. 原子接收整个资源清单，用接收回执中的固定代次重建 payload，释放子任务控制句柄。
5. 合并任务标签上界，保存 outcome 和事件，然后提交。

payload 与转移清单不一致、接收方权限不足、代次错误或任一资源不在 Escrow，均使整项决定回滚。该路径新增了持久接收前的值记录/清单对应检查；原始 Store 的发布接口仍允许可信宿主自定义业务 payload，受检发布的完整类型保证由 Broker 和 TaskRunner 提供。

结果记录使用独立结构校验，不把任务输入封装的 1 MiB 限额额外套到已持久化结果上；普通数据结果的原有接收边界保持不变。

读取旧 outcome 只是读取历史结果。接收过的成员再次转交后，旧 payload 保持原代次；后续使用仍经过当前 owner、generation 和 TaskLease 检查。重放不会重新接收资源，也不借当前 owner 查询刷新旧引用。

## 取消与完成的先后顺序

`cancel_outcome` 不再组合两个独立的 handoff/take 事务，而是通过同一原子决定固定结果。

| 决定时的状态 | 结果与责任 |
| --- | --- |
| Completed 且暂存包可接收 | 完成胜出；接收包并返回 Completed |
| 尚未完成，包括 Waiting | 取消胜出；接管当前剩余责任并返回 Cancelled |
| Failed 或 Cancelled | 返回已有终态说明；不恢复原输入资源 |
| 已有 outcome 决定 | 重放原决定及其固定接收代次 |
| Completed 包已被独立 handoff 退役 | 拒绝资源接收；历史完成状态不代表包仍可使用 |

取消先提交后，旧 worker 无法再发布成功包。完成先提交后，`cancel_outcome` 接收成功包，不会先退役它。独立 handoff 仍表示放弃接收、将责任交给 Runtime。

Failed/Waiting/Cancelled 不证明外部请求没有生效，也不清除已暴露请求的费用。当前持有的资源进入既有责任接管路径；迟到费用按原 attempt 结算。对 Waiting 使用 `join_outcome` 固定 Waiting 观察并交接责任；显式 `cancel_outcome` 固定 Cancelled。这两种观察都不授予原输入资源的新引用。

## race 的胜者、接收与败者处理

`select_race` 在同一事务中验证候选集及胜者、核对并接收胜者的完整资源包、释放胜者句柄、取消败者当前持有的责任，并保存 winner。任何一步失败均回滚，不能留下已确定胜者但未接收包、或者只处理部分败者的状态。

重放使用持久 winner 和 `task_resource_returns.received` 中的接收回执。成员后来再次转移，不改变旧 race 结果。结果标签合并所有候选的标签上界，未获胜的私密候选仍限制公开结果。败者已经暴露的请求继续保留费用责任，胜者资源接收不会改写其出资记录。

## 执行与验证

```powershell
python -m examples.resource_task_outcomes
python -m unittest tests.test_resource_task_outcomes -q
```

[SDK 示例](../examples/resource_task_outcomes.py) 与 [源码示例](../examples/resource_task_outcomes.al0) 的两个入口生成相同 IR：

- `resource-outcomes` 接收混合包，在 Completed 分支使用已接纳 Operation、尚未消费 Permit、对应 Intent 和 Budget，完成两次行动。独立 SQLite 端点记录两次调用和两次收费，确认费用 2、可用预算 8、预留 0。
- `resource-race` 让两个候选返回嵌套预算包，原子接收任一成功候选、释放败者责任，再拆包归还胜者预算。最终可用预算恢复为 10。胜者取决于实际完成顺序，不承诺固定为第一个候选。

两条路径重放结果相同且不新增事件，输出保存在 [执行报告](../models/resource_task_outcomes_example.json)。

41 项专项测试通过，用时 24.948 秒。覆盖一次匹配、显式结果参数、Budget/Operation 责任、嵌套父任务、过期父 lease、结果与清单不一致回滚、完成/取消并发、历史回执、包和败者标签、执行失败与 Waiting、败者迟到费用，以及仍然禁止的推测共享写入。

其中 12 项执行真实进程退出：8 项在 Store 层覆盖结果接收、取消已完成任务及 race 的事务中和提交后边界；4 项从完整解释器退出并恢复，验证混合包后续行动只调用两次、race 恢复不新增事件。有限故障位置覆盖不等于任意执行调度的完整证明。

2026 年 10 月 26 日最终完整回归通过 703 项测试，用时 437.262 秒，见 [回归日志](../models/resource_task_outcomes_full_regression.log)。[SDK 类型报告](../models/sdk_type_audit.json) 覆盖 52 个公共方法、18 个基础计划与 5 个生成绑定计划、28 + 12 个负例；新增正例包含资源 outcome 和 race。

[安装包报告](../models/sdk_wheel_audit.json) 确认最终 wheel 在仓库目录之外通过运行及严格类型检查：消费程序执行普通 Bundle 接收、资源 race、胜者包转交、TaskOutcome 匹配和预算归还，并验证重放无新增事件。总体证据及未完成要求见 [实现状态](08-implementation-status.md)。

## 剩余研究工作

一般业务资源 sum type 后续已在 [受检业务 Variant](34-checked-business-variants.md) 实现。终态 infer_result 已在 [受检推理结果](36-checked-inference-results.md) 接通；推理未决/接纳层结果、一般循环控制、ContextView、授权解密/完整性提升、完整模型与运行时对应，以及真实工作负载和同运行时 SDK 对照实验仍未完成。现有 TaskOutcome 只描述执行状态；Completed 不能替代业务成功的检查证据。
