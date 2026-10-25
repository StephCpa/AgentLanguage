# 持久预算分配树

后续已将本账本接入源码和 SDK 的 Budget 参数、split/join 与持久工作流调用，并补齐任务对子分配的租约和责任管理，见 [Budget 参数与持久工作流调用](20-budget-workflow-calls.md)。

更新日期：2026 年 10 月 25 日。实现位于 `al0/allocation.py`，与原有接纳、取消、结算、退休及重试事务集成。此处的 `join_budget` 是预算分配归还；后续实现的任务控制权接口见 [持久任务作用域](11-durable-tasks.md)。

## 账本表示与不变量

一个 run 只有一个根预算。原有 `budgets` 表继续保存每个资源维度的 `B,F,R,C,Q`。新增的 `allocation_balances` 将 F 分给各个 allocation；不会为每个子分配再建立独立根额度。

```text
F_root_ledger = sum(free[a] for a in all_allocations)
B = F_root_ledger + R + C + Q
```

第二个等式要求提供方遵守费用上界。上界违约时保留实际金额并 halt，不能用守恒式掩盖超支。分配账目仍须与全局账本一致，即使守恒式已经失效。

`allocations` 保存不可变父子关系、稳定 split key、最初转移量和 Open/Joined 状态；`operation_allocations` 为操作保存不可变资金归属。一个操作的所有重试 attempt 都使用原分配。分配身份不能代替授权 grant。

`allocation_tree` 展示每个分配自身的 F/R/C/Q，责任按 operation 的归属统计，父节点不重复包含子节点。`initial_transfer` 只说明历史上划拨的金额，不是该节点当前可用总额，更不能跨树求和后当作根预算。

## 转移规则

| 操作 | 局部变更 | 全局变更 |
| --- | --- | --- |
| `split_budget(parent,q)` | 父 F 减 q，新增子 F 为 q | 无 |
| 接纳或重试，预留 u | 所属开放分配 F 减 u | F 减 u，R 加 u |
| 结算，实际 c 不超过 u | 退款 u-c 交给最近仍开放的祖先，包括自身 | R 减 u，C 加 c，F 加 u-c |
| 取消未派发 attempt | 完整退款 u 交给最近开放祖先 | R 减 u，F 加 u |
| 退休 attempt | 不增加任何局部 F | R 减 u，Q 加 u |
| `join_budget(child)` | 子 F 归还父，子进入 Joined | 无 |

每个操作都在原有 `BEGIN IMMEDIATE` 事务中执行；多资源维度全部满足才提交。根预算中的 F 足够，也不意味着某个子分配可以花费其他分配的余额。

split 的 key 在 run 内唯一。相同 key、父节点和转移量恢复同一分配；参数变化则拒绝。恢复已经 Joined 的 split 不会重新开放或补发资金。join 幂等，只归还当前 F；它不归还 R、C 或 Q，也不更改操作的业务知识状态。

父分配必须先合并所有仍开放的直接子分配，才能被合并。这个约束沿树保持成立。迟到退款沿不可变父链寻找最近开放节点，因此不会存入一个已经无法使用的关闭分配，也不会被结算与 join 的竞态发放两次。

Joined 分配不能提供新的预留，不能 split 新子节点；已接纳操作仍能恢复、取得一次派发权或接收回执。预算合并本身不代表任务取消。若需要禁止已接纳工作继续派发，宿主还须调用操作取消接口；未来任务作用域会统一这些转移。

## 接入共享执行路径

```python
worker = store.split_budget(run, principal, 'worker', {'units': 4})
recovery = store.split_budget(run, principal, 'recovery', {'units': 2})
result = executor.run(plan, run, principal, grants, allocation=worker)
store.join_budget(run, principal, worker)

# 已知待对账 operation；查询使用独立分配及独立 grant。
broker.reconcile(run, principal, 'recover', operation, 'repo.query',
                 grants['repo.query'], allocation=recovery)
store.join_budget(run, principal, recovery)
```

`Store.admit`、`Broker.admit_call`、`Broker.reconcile` 和 `Interpreter.run` 接受宿主绑定的 allocation。未指定时使用根分配。相同 slot 恢复时必须保持原有资金归属，不能悄悄改由根或其他子分配承担。旧程序在原分配 Joined 后仍可读取和恢复已经记录的操作；若它尝试新增付费节点，则失败。

allocation ID 是账本引用，不是具有密码学认证的能力。持有 Store 或数据库访问权的宿主仍属于可信基。后续已实现 [独立任务身份](11-durable-tasks.md)、[语言内 Budget 与 split/join](20-budget-workflow-calls.md) 和 [运行时金额计算](22-dynamic-budget-vectors.md)；普通 JSON 仍不能伪造 Budget 接口。

## 验证证据

本轮新增 23 项账本测试和 1 项 SDK 端到端测试，完整套件为 107 项，全部通过。覆盖的区别性情况包括：

- 父、子、兄弟资金不可交叉使用；多资源 split 失败没有局部扣减。
- 并发重复 split 恢复同一分配；并发不同 split 不能重复划拨。
- join 与接纳、结算的竞态；未知业务仍保留预留。
- 多层 join 后迟到退款；取消未派发操作的退款；退休与迟到零账单不释放额度。
- 重试固定原分配；操作恢复不能改换分配；外部超支保存实际金额。
- split/join 提交前后四个独立进程退出位置，以及 v3 数据库迁移后的未知操作恢复。

这些是具体实现测试，不是任意任务树、任意故障调度或全部语言程序的形式证明。

运行 `python -m examples.allocated_recovery`：初始根预算 10，工作分配 4，对账分配 2，其余 4 留在根。远端修改成功但回复丢失后，工作流程返回 `NeedsReconciliation`。合并工作分配并结束控制流程时，仍有 C=3、R=1、F=6，状态为 `CompletedWithObligations`。一次查询确认业务和费用后，C=5、R=0、F=5；合并查询分配后全部 F 回到根。状态变为 `CompletedClean`，仓库修改和查询各一次，恢复不新增事件或费用。

schema 升至 4。从版本 2 或 3 迁移时，已有全局 F 归入一个根分配，既有 operation 绑定该根；预留、已确认费用、退休额度和许可消费均保留。建表、初始化归属与升级版本在同一事务中。未登记版本或其他旧版本仍需显式迁移方案。

## 下一层任务作用域的要求

分配树提供资源底座。下列任务生命周期要求中，1 至 4 已有后续宿主实现，详见 [持久任务作用域](11-durable-tasks.md)；语言层集成及后两项仍待推进：

1. 为 scope/task 持久化身份、父子关系和程序激活命名空间，避免子计划相同 node 名造成 slot 冲突。
2. 原子绑定任务与预算分配；重复 spawn 恢复原任务，不能再次划拨或派发。
3. 区分本地执行结束、join 和未决责任接管；未决远端操作不能随着 Python Future 或栈帧消失。
4. 将作用域退出、任务取消与新接纳/派发串行化，自动取消未派发工作并登记在途责任接管。
5. 持久化 race 的获胜选择，检查推测子任务的传递效应；胜出之前不允许共享外部写入。
6. 以同一运行时比较 SDK 与 AL-0 前端，并验证不同调度及崩溃下的轨迹和责任对应。

本节预算测试验证的是账本事务在多调用者下的安全性。后续任务实现额外验证了持久生命周期、实际并行执行和退出接管，仍不能代替完整语言与组合证明。

schema 16 进一步为每次 attempt 固定出资 allocation，结算、queued 释放及分账户统计改读该记录。该阶段先建立资金基础，迁移及进程退出验证见 [跨任务仿射资源协议](27-affine-task-transfer.md)。后续 [schema 17](28-durable-resource-custody.md) 已在可信 Store 接通 Budget/Operation 控制权移动和显式重试出资；后续 [受检任务资源](29-checked-task-resources.md) 已开放仿射参数与单资源返回，显式重试出资仍属于可信 Store 接口。
