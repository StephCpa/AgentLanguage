# Python SDK、付费对账与未决责任

更新日期：2026 年 10 月 26 日。本文件记录 Python 构造接口和显式对账协议；完整实现进度见实现状态文档。

## 同一受检 IR 的两种入口

`al0.sdk.PlanBuilder` 构造数据形式的 IR，`finish` 调用同一个 `check_plan`。引用是符号引用，不能直接当作运行时权限。`Candidate[T]`、`Checked[T]`、`Permit[A]` 等泛型表达接口关系；schema、谓词、许可使用次数和精确 intent 绑定仍由共享检查器验证。后续已为 48 个公共构造方法完成固定 mypy 版本的静态验收，并通过 `CheckedPlan[T]` 保留调用、分支和任务结果类型。正反例、动态边界和安装包验证详见 [SDK 静态类型验收](25-sdk-static-typing.md)；这仍不等于整个运行时已通过严格类型检查或完整强基线实验。

`examples/repair_sdk.py` 与 `examples/repair.ir.json` 生成完全相同的规范化 IR 和程序指纹。测试还分别在新数据库上执行两者，比较事件顺序、节点位置、结果、预算和模拟端点状态。随机生成的 operation/attempt ID 不要求字面相同。这是一个工作流的差分证据，尚未证明全部 SDK 程序的语义等价或开发效率收益。

构造器默认推导实际使用的效应；传入 `uses` 时保留调用者指定的上界，不自动扩大。跨构造器引用、跨构造器工具、重复节点及错误工具种类被拒绝；资源使用规则由共享检查器执行。

```python
builder = PlanBuilder('repair', catalog)
request = builder.literal('request', 'Text', 'Fix the error.')
draft = builder.infer('draft', builder.tool('code-model'), request)
tested = builder.check('tested', builder.tool('patch-tests'), draft)
intent = builder.prepare('intent', builder.tool('repo.merge'), tested)
permit = builder.authorize('permit', intent)
operation = builder.admit('operation', intent, permit)
status = builder.await_result('status', operation)
result = builder.match_business(
    'result', status, 'Text', applied='Done', proven_no_effect='NeedsRevision',
    pending='Waiting', unknown='NeedsReconciliation', not_dispatched='Waiting')
plan = builder.finish(result)
```

目前 `match_business` 选择数据值，不执行分支中的子程序。对账示例因此固定执行一次查询，不能据此声称已有条件控制流或自动恢复策略。

## 对账是新的计费操作

目录为 `repo.query` 登记 `reconcile` 种类，并绑定目标 `repo.merge` 的版本和 scope。IR 声明 `reconcile:repo.query`；宿主另行绑定查询 grant。业务动作 grant 不隐含查询权限。

`Broker.reconcile` 为查询创建自己的 intent、permit、operation 和 attempt，通过原有接纳与派发路径执行。查询从空闲余额预留资源，不能借用目标动作仍持有的预留。查询撤权、过期、费用上界和回复丢失均遵循普通调用规则。

稳定查询输入仅包含目标 operation 和 intent 摘要。同一节点恢复复用持久化查询结果，不重新收费；新节点表示一次新查询，需要重新预留。一次查询没有结果时不会自动循环轮询。业务内容相同也不会把不同查询节点合并成同一操作。

受信适配器返回的协议结构为：

```json
{
  "operation": "target-operation-id",
  "intent_digest": "target-intent-digest",
  "business": {
    "evidence_id": "provider-business-receipt",
    "outcome": "Applied",
    "details": {"output": {"version": 8}}
  },
  "bills": [{
    "attempt": "target-attempt-id",
    "evidence_id": "provider-billing-receipt",
    "actual": {"units": 1}
  }]
}
```

业务证据可以为 `null`；账单可以为空。缺少记录不会转成 `ProvenNoEffect`。业务终态证据与费用证据分别进入账本。查询响应自身的费用首先按真实调用结算；即使其目标证据验证失败，也不会免除查询费用。

应用前检查整个回执的形状、目标 operation/intent、attempt 归属和派发状态、资源维度及重复证据身份。这不是对外部证明真实性的密码学验证；身份认证、回执真实性和终态有效性仍是注册适配器的职责。

合法回执随后逐项幂等应用。在查询结果已持久化但尚未应用、业务证据已应用但账单未应用、账单已应用三个位置退出进程，恢复后均复用查询结果。回执应用不是单个跨多记录事务：进程退出时可暂时只录入业务结果，费用预留继续保留，恢复补齐账单。冲突证据会被账本拒绝，不能覆盖旧事实。

查询自身也可能回复丢失，形成额外未决责任。当前未提供递归查询对账工具的机制，后续需由可信回执接入或新的适配器协议解决。不能据此保证所有外部责任最终可自动结清。

## 流程终止与责任结清分开

`Store.finish_run` 显式登记 `Completed` 或 `Failed`。相同终止登记可重放，终止决定不能任意翻转。解释器返回值不会自动调用该接口，宿主决定何时结束控制流程。

`Store.obligations` 只读取本地持久状态，列出未终结业务、未结算预留及已退役但尚无实际账单的 attempt。它不访问提供方，不产生查询费用。

`Store.status` 组合控制状态与责任，区分 `CompletedClean`、`CompletedWithObligations`、`Failed`、`FailedWithObligations` 等。`CompletedWithObligations` 的预算不能当作已释放。流程结束后拒绝新的普通工作，允许显式授权且有余额的对账；已接纳工作仍按其状态推进。结束流程不等于取消已接纳操作。

预算违约的 halt 独立生效，仍阻止新的付费对账接纳；已有回执可以经可信入口登记。不能通过查询工具绕过硬预算规则。

当前恢复重建程序局部值并读取最新持久状态。一个原来返回 `NeedsReconciliation` 的程序，在外部对账后恢复可能返回 `Done`。这是恢复执行，不是逐事件重现历史观察的 audit replay。

## 复现和迁移

```powershell
python -m examples.repair_sdk
python -m examples.reconcile_sdk
python -m unittest discover -v
```

对账示例模拟一次远端修改成功但回复丢失：最终返回 `Done`，总确认费用为 5 个实验单位，其中查询 1 单位；模拟仓库修改一次、查询一次。再次恢复没有新增事件或费用。费用不是实际 token 或货币价格。

对账实现首次将 schema 从 2 升至 3，增加对账路由和控制状态表。后续预算分配实现已升至 4，支持从 2 或 3 迁移，详见 [预算分配树](10-budget-allocation.md)。迁移测试验证保留既有未知操作及许可消费。其他旧版本和含表但未登记版本的库仍拒绝打开；这不是通用迁移或备份回退解决方案。旧模拟端点新增账单归属字段，但不会给历史缺失归属的账单伪造 operation。

预算分配树、任务 IR、信息流标签、AL-0 核心前端及 SDK 构造接口静态验收已在后续接通；固定 Catalog 的 [schema/工具类型绑定生成](26-catalog-type-bindings.md) 已完成接口、漂移拒绝和具体程序差分验证。仍需运行时变化的 schema 类型精化、完整诊断、完整资源协议，以及覆盖更多程序和故障调度的同运行时差分实验。当前数据库版本和各项剩余要求以 [实现状态](08-implementation-status.md) 为准。
