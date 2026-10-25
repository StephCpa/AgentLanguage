# 显式任务输入与 TaskOutcome 分支

更新日期：2026 年 10 月 25 日。本轮新增 parameter、spawn_with、join_outcome、cancel_outcome 和 match_task。任务输入可以来自父计划计算结果，分支可以执行受检工具调用；它们继续使用同一授权、账本及持久任务核心。

## 数据和检查证据的显式传递

parameter 声明名字、schema、数据类别及所需谓词。数据输入支持 Data、Candidate、Observation 和 Checked；Checked 必须列出已注册 checker 的谓词。后续另加入 [Cap 输入与结果](23-task-capability-transfer.md)。子计划的自由变量仍被拒绝，父计划通过 spawn_with 的 inputs 显式绑定参数。

绑定要求名字集合完全一致、schema 一致、数据类别一致，且来源包含目标需要的全部谓词。Candidate 不能通过参数变成 Data 或 Checked。Scope、Task、Permit 和其他独占控制引用不能冒充 JSON 捕获；预算仍由 spawn 的额度字段在账本内转移。

运行时输入由 JSON data 和 receipt 引用组成。Checked 参数进入解释器前，会核对 receipt 所属 run、artifact 摘要和谓词。仅在输入中写出一个 receipt 字符串不能得到检查证据；跨运行或绑定其他内容的回执被拒绝。检查器正确性、版本撤销和依赖新鲜度仍遵循现有可信基与未完成功能边界。

```python
child = PlanBuilder('check-child', catalog)
patch = child.parameter('patch', 'Patch', kind='Candidate')
tested = child.check('tested', child.tool('patch-tests'), patch)
child_plan = child.finish(tested)

task = parent.spawn('checker', scope, child_plan, {'units': 1},
                    inputs={'patch': parent_draft})
```

数据输入和只读检查证据可以复制到多个子任务。这里还没有实现 Permit、Budget、Intent 或 Operation 的一般 move 捕获、借用和跨任务返回规则；禁止这些捕获不是对原设计要求的删除。

## 恢复固定输入

task_inputs 与任务、预算分配在 spawn 的同一事务中写入。输入先经过规范化和复制，之后修改宿主原字典不会改变任务内容。重复 spawn 相同 key 时，程序、额度和完整输入记录都必须一致；同一个程序配上新输入不能冒充旧任务恢复。

根程序同样使用 run_inputs 固定首次输入，即使程序没有外部调用，也不能在恢复时静默换值。旧版无参数任务按空输入解释，不给它们添加新数据。每组输入有 1 MiB 限制；它不是完整的运行时内存上限。

Value 和输入记录都是可信宿主与解释器之间的数据接口，不是宿主沙箱。输入类型不能代替身份认证、工具 grant 或数据出站授权。

## 显式结果和责任转移

join_outcome 消费任务控制句柄，并返回需要穷尽匹配的 TaskOutcome。它等待真实 Future 返回或挂起，再依据持久任务状态生成结果，不把工作线程领取失败误认为业务失败。

| 结果 | 分支参数 result | 责任处理 |
| --- | --- | --- |
| Completed | 子任务实际返回的 Data/Candidate/Observation/Checked 值或 Cap | 收回控制句柄；未决操作仍交运行时保管 |
| Failed | Data<Json>，包含任务身份及错误类型 | 保留已暴露操作的费用责任 |
| Waiting | Data<Json>，包含任务与等待操作身份 | 记录 Waiting 观察后，取消新工作并登记接管 |
| Cancelled | Data<Json>，包含任务身份及可用的等待操作身份 | 保留在途效果及费用；不解释为 ProvenNoEffect |

任务状态、首次结果观察与责任释放在 take_task_outcome 的同一事务中提交。Waiting 接管后的控制状态会变成 Cancelled，但持久 TaskOutcome 仍为 Waiting。晚到成功回执可以结清责任，却不会把恢复中的父程序悄悄改走 Completed 分支。

cancel_outcome 先请求取消并接管任务，再取得结果。若任务已经成功结束，它可以返回 Completed；若取消先阻止本地完成，则返回 Cancelled。该请求不保证远端没有生效，也不自动释放在途预留。

旧 join 接口仍保留“只取得成功值，否则传播异常”的行为。需要程序内恢复策略的代码应使用 join_outcome 和 match_task。普通的无结果、授权错误和预算错误也没有被统一改成任务成功。

## 带效应的穷尽分支

match_task 必须提供 Completed、Failed、Waiting、Cancelled 四个子计划。只执行所选分支；每个分支仍完整接受静态检查，所需效应必须包含在父 uses 中，推测策略继续向内传递。

分支可以声明名为 result 的参数：Completed 中它具有子结果的原始数据类别和谓词；其他分支中它是 Data<Json>。失败分支不能声明一个 Checked result 并据此执行受保护动作。

其他参数通过 match_task 的 inputs 显式捕获父计划数据或检查证据；后续还加入了 [Intent、Permit、Operation 与 OpRef 分支捕获](18-resource-branches.md) 和 [跨任务 Cap 传递](23-task-capability-transfer.md)。仿射资源仍不经 spawn 传给子任务。result 是保留绑定，调用者不能覆盖。所有分支返回相同的类型和谓词集合，不能通过把失败分支的普通数据与成功证据合并来伪造 Checked 或 Cap。

```python
outcome = parent.join_outcome('outcome', task)
parent.close_scope('closed', scope)
answer = parent.match_task(
    'answer', outcome,
    completed=apply_checked_result,
    failed=needs_revision,
    waiting=needs_reconciliation,
    cancelled=cancelled_message)
```

分支直接使用父执行上下文及其预算，不偷偷建立新的根运行。工具调用、授权恢复键和嵌套 scope 的位置包含分支路径，避免分支里的同名节点与父节点或其他分支冲突。首次 TaskOutcome 冻结了选择依据；恢复复用同一分支位置和已接纳操作。

这是面向任务结果的条件控制流。后续实现已加入 [信息流控制标签](14-information-flow.md)、[布尔条件分支](16-pure-and-conditional.md)、[纯函数与循环](17-functions-loops-and-fuel.md)、[分支资源传递](18-resource-branches.md) 和 [Budget 工作流接口](20-budget-workflow-calls.md)。后续 [受检资源包](31-checked-resource-bundles.md) 已支持跨任务句柄移动和多资源正常返回；[资源型 TaskOutcome/race](32-resource-task-outcomes-and-race.md) 已补齐成功包接收、完成/取消原子决定和胜者资源接收。

## 可运行证据

`python -m examples.task_outcomes` 执行三个本地场景：父计划生成候选，子任务检查候选，父计划根据结果决定是否修改模拟仓库。

| 场景 | 程序结果 | 仓库修改 | 确认费用 C | 未决预留 R |
| --- | --- | --- | --- | --- |
| 检查成功 | Done | 1 次 | 4 | 0 |
| 检查拒绝 | NeedsRevision | 0 次 | 3 | 0 |
| 检查回复丢失 | NeedsReconciliation | 0 次 | 2 | 1 |

三个场景恢复后都保持原结果且没有新增事件。失败与等待路径不会调用仓库修改，也不会隐式重试检查器。检查拒绝场景的 CompletedClean 只表示控制流程已经返回且外部责任已清空，不表示业务修复成功。

新增 28 项专项测试覆盖参数绑定、类型与谓词边界、输入不可变性、跨运行和修改 artifact 的回执拒绝、穷尽分支、同名节点位置、取消后的迟到收费、结果观察事务提交前后崩溃、输入与 spawn 同事务提交，以及 v6 迁移。完整测试结果以 [实现状态](08-implementation-status.md) 为准。

schema 升至 7，新增 task_inputs、run_inputs 和 task_outcomes。支持从版本 2 至 6 中既有受支持版本迁移；迁移不生成旧任务的新结果，也不释放旧预留。
