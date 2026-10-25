# 任务 IR、持久 race 与推测执行

更新日期：2026 年 10 月 25 日。任务编排已从宿主接口接入受检 IR 和 Python 构造器。新增节点为 scope、spawn、join、handoff、close_scope 和 race；它们使用同一持久任务运行时，不建立第二套执行或预算账本。

## 静态规则

| 节点 | 检查规则 | 结果 |
| --- | --- | --- |
| scope | 声明 tasks 效应，预算为非负整数向量 | Scope 控制句柄 |
| spawn | scope 未关闭，嵌入的子计划独立通过检查，子效应包含于父 uses | Task<T> 控制句柄 |
| join | 消费一个尚未使用的 Task<T> | 保留子结果的数据类别、schema 和谓词集合 |
| handoff | 消费一个尚未使用的 Task<T> | 责任状态 JSON，不伪造子结果 |
| close_scope | 该 scope 下所有 task 已消费，scope 尚未关闭 | Bool |
| race | 至少两个互异、未消费、属于同一推测 scope 的 task，结果类型完全一致 | 同一结果类型 |

所有任务句柄必须在计划结束前消费，所有 scope 必须关闭。异常路径由运行时登记取消和接管。作用域和任务句柄不能作为普通 JSON 或计划返回值逸出。重复 join、handoff 后再 join、race 重复句柄、关闭后继续 spawn 和忽略子任务均被拒绝。

子计划的效应向父计划传递，包括嵌套 scope 中的工具调用。SDK 默认收集这些效应；显式指定 uses 时不会扩展调用者边界。子计划中的 shared-write 效应不能通过另设一层非推测 scope 逃逸外层推测策略。

首版子计划是闭合的 IR 文档；后续已增加显式数据和只读检查证据参数，见 [任务输入与 TaskOutcome](13-task-inputs-and-outcomes.md)。许可、预算引用或 task 句柄仍不能隐式捕获。任务可以返回 Data、Candidate、Observation 或 Checked。返回 Checked 时，运行时携带实际 checker receipt，父计划的 prepare/admit 仍检查回执与具体 artifact 的绑定。仅有 Candidate 的分支不能被 race 自动提升为 Checked；各分支必须具有相同的类型和谓词集合。

数据参数与 TaskOutcome 分支已经在后续版本实现；可返回的 Intent、Operation 转移及一般仿射资源分割仍需扩展。当前受限捕获并不替代原设计中的一般资源分割。

输入限制按整棵嵌套文档计算：最多 1 MiB、累计 10000 个节点、任务嵌套不超过 16 层。底层 JSON 的深度限制同时生效。这些限制不是完整的执行 fuel、堆空间或全局 worker 数量控制。

## 推测执行的写入边界

工具目录新增 shared_write 分类。action 默认会写共享状态；infer、observe、check 和 reconcile 默认不写共享状态，可信适配器登记者必须纠正不符合该默认值的工具。例如写共享日志的 observe 工具必须标记 shared_write=True。分类随工具版本固定，不能在同一版本中改变。

推测 scope 中禁止使用标记为共享写入的工具效应，包括用 observe 或 infer 名称包装的写入。明确隔离的沙箱 action 可以登记 shared_write=False 后运行；工具名字本身不证明隔离。prepare 只是构造不可变描述，不构成共享写入，但当前子计划返回边界尚不支持 Intent。

检查器递归检查推测策略，运行时将策略继承到任务和嵌套 scope，并在接纳、重试及派发时复核。推测任务使用未登记分类的工具也会被拒绝。模型生成的计划不能改变目录分类。提供方是否确实遵守“只读”或“隔离沙箱”契约仍属于可信工具适配器的责任，不能把一个布尔字段当作宿主沙箱的证明。

推测分支的模型调用、检查与隔离操作都需要实际 grant 和预算，并独立计费。落选不会撤销已经发生的调用费用。

## 持久选择与取消

race 选择第一个被协调器观察为成功完成的候选。失败或 Waiting 候选不能获胜；如果全部没有结果，登记责任接管后抛出明确错误。多个候选几乎同时完成时，没有固定的物理时间先后保证，最终以持久选择记录为准。

选择事务同时完成以下操作：

1. 固定 `(scope,race_key)` 对应的有序候选集合与 winner。
2. 收回获胜任务的控制权。
3. 取消并接管落选任务子树，保留已暴露请求和计费责任。
4. 归还各分配当前可用的 F；不重新发放未决的 R 或退休的 Q。

在提交前退出进程，整个选择与释放回滚；提交后退出，恢复读取原 winner，不重新竞赛。相同 key 改换候选集合被拒绝。两个选择调用者并发提交不同 winner 时，后提交者读取已经固定的 winner。

调用方得到结果后，落选的可信 Python 回调可能还在运行。它可以追加真实业务回执和账单，但其旧 task 状态不再允许新接纳或派发。`Broker.wait_for_workers(timeout=...)` 是宿主关闭本地存储前的等待接口；它等待真实 Future，包括运行中生成的嵌套 worker，不代表远端责任已经结清。race 自身不等待落选回调结束。

## 恢复与错误路径

Interpreter 直接执行这些节点，自动管理 scope 的异常退出。子任务身份和程序指纹、节点 activation、预算分配及 race 选择均持久化。普通恢复不会抢占 Running worker。可信监督者确认需要接管后，可通过 `Interpreter.run(..., recover_tasks=True)` 显式更新恢复树中工作线程的代次；已暴露的 attempt 不会因新代次重新发送。

旧 join 只有在取得实际子结果时才返回 T。若子任务因未知外部结果而 Suspended，它登记 handoff 并抛出 StateConflict。后续新增的 join_outcome 和 match_task 则让程序在受检控制流内显式处理 Completed、Failed、Waiting 和 Cancelled，详见 [任务输入与 TaskOutcome](13-task-inputs-and-outcomes.md)。

TaskScope 的宿主接口和 SDK 构造器同样提供 join、handoff 和 race。宿主接口本身仍属于可信代码；生成计划的任务控制必须通过 IR 检查器。

## 示例与验证

```python
scope = builder.scope('candidates', {'units': 6}, speculative=True)
left = builder.spawn('left', scope, checked_candidate_plan, {'units': 3})
right = builder.spawn('right', scope, checked_candidate_plan, {'units': 3})
winner = builder.race('winner', [left, right])
builder.close_scope('close_candidates', scope)
intent = builder.prepare('intent', builder.tool('repo.merge'), winner)
```

完整示例 `python -m examples.speculative_repair` 并行生成并检查两个候选，选择一个持久 winner，再由父计划授权一次仓库修改。该本地模拟示例使用屏障让两个 checker 都实际被调用：总确认费用为 7 个实验单位（两个候选各 3，最终修改 1），余额为 3，修改只发生一次。恢复保持相同 winner 和相同业务修改，选择记录只有一条。事件顺序也验证最终共享写入在选择提交之后接纳。

新增 32 项专项测试覆盖句柄消费、跨 scope 拒绝、子效应边界、嵌套策略继承、运行时直接调用的写入拒绝、隔离 action、Checked 结果传递、嵌套进程恢复、落选回调迟到收费、失败分支和 race 提交前后崩溃。它们是实现层证据，尚不是任意编译转换与故障组合的形式证明。

schema 升至 6，新增任务策略、工具效应分类和 race 选择表。支持从 2 至 5 的既有受支持版本迁移；旧任务保留程序、epoch、预算和状态，默认按非推测任务解释，不会对历史效果追认推测安全。v5 迁移测试验证这些字段与额度保持不变。
