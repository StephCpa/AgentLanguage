# Budget 参数与持久工作流调用

更新日期为 2026 年 10 月 25 日。本轮把已有分配树接入受检语言，并增加带效应、带资源输入的工作流调用。Budget 是独占的分配接口；它引用已存在于账本中的资金归属，不是可复制的余额数字。

## 源码和 SDK 接口

```text
workflow emit-one(message: Text) -> Observation<Text> uses {"observe:emit"} {
    let value = observe("emit", message);
    return value;
}

workflow funded(funds: Budget, message: Text) -> Observation<Text>
uses {"observe:emit", "budget"} {
    let value = invoke_with(emit-one, funds, {message: message});
    let released = join_budget(funds);
    return value;
}
```

| 构造 | 含义 |
| --- | --- |
| `split_budget({u:3})` | 从当前调用的资金分配划出子分配，产生一个 Budget |
| `split_budget_from(parent,{u:2})` | 借用 parent 的剩余额度，再划出子分配 |
| `join_budget(funds)` | 消费 Budget 接口，归还其当前可用余额 |
| `invoke(workflow,{parameter:value})` | 在当前资金分配内同步调用工作流 |
| `invoke_with(workflow,funds,{parameter:value})` | 在 funds 指定的分配内同步调用工作流 |

SDK 对应 `PlanBuilder.budget_parameter`、`split_budget(..., parent=...)`、`join_budget` 和 `invoke(..., budget=...)`。预算拆分与归还要求声明 `budget` 效应；调用的完整传递效应必须包含在父工作流 uses 中。

`invoke` 使用同一 run、任务 activation 和 worker lease，与 spawn 的独立任务不同。callee 的节点拥有带调用路径的稳定 site，调用同一工作流两次不会合并为一次外部动作。函数 call 继续只运行纯函数；invoke 明确要求 workflow。声明图保持无递归，展开规模和执行步骤限制继续生效。

## 资源输入和返回

工作流实参必须精确绑定全部参数。Data、Candidate、Observation、Checked、Intent、OpRef 和 Cap 可复制；Permit、Operation 和 Budget 移入 callee，父引用此后不可再用。Permit 的参数关系仍须绑定同一 Intent；Operation 的处理责任随引用移入，并可由返回值转交父调用。

Budget 参数必须被 join、移入下一层受检调用或分支，或作为唯一返回值交还。callee 也可以返回自己新建的子 Budget。分支可合并相同类型的 Budget 返回值。所有这些路径都保留账本 allocation 身份，不复制其 free 份额。

`invoke_with` 的资金参数是调用期间的借用，调用结束后调用者仍持有该 Budget。禁止把同一 Budget 同时作为资金借用和独占实参移动给同一个 callee；也禁止以两个参数名复制同一独占资源。

根入口不能通过 `Value("allocation-id")` 构造 Budget，普通 Json、记录和纯运算也不能生成它。当前 Budget 参数只由受检分支或 invoke 建立。一般 Budget 句柄仍不能经 spawn 输入或任务结果传递；任务资金继续通过作用域分配协议划拨。

## 账本分割与责任

根账本的 free 是所有分配可用份额之和：

```text
root.free = Σ allocation.free
root.reserved = Σ unsettled_attempt.upper_bound
```

split 只在 allocation 间转移 free，不改变总预算。一次尝试必须同时满足全局可用额和指定 allocation 的本地可用额，不能在子预算不足时自动借用根或兄弟分配。

join 只归还 free。Operation 及其每次尝试保留最初的 allocation 归属；已暴露请求的 reserved 不因 join、异常、取消或工作流返回而释放。费用结算产生退款时，沿原父链寻找最近仍开放的分配。已 join 的节点不会因迟到回执重新开放。

父预算尚有开放子分配时，join 会抛出 StateConflict，并保持账本不变。当前静态检查保证 Budget 接口不重复使用和不静默丢弃；完整父子图的关闭顺序仍由账本动态验证。这是显式的 `NoOpenChildren` 义务，而非已经证明的静态性质。

## 持久调用帧

schema 12 增加 invocation_frames，以 `(run,activation,node)` 标识调用，保存：

- 首次输入，包括保护引用和 Intent 描述；
- 资金 allocation 身份；
- 首次控制标签。

帧提交后才进入 callee。恢复固定原输入与资金来源，资金身份改变会报 ReplayMismatch。当前控制标签与历史标签继续合并，旧帧不能放宽信息流策略。

调用帧不缓存整个 callee 的最终结果。恢复重新执行受检节点，工具操作、子预算划分、条件选择和循环进度各自恢复持久身份。已完成的子 Budget 即使处于 Joined，仍可恢复旧 split 和旧调用帧、读取已有操作，并重复执行无额外账本变化的 join；它不能为新操作付款。

拆分键包含任务 activation 和完整调用路径。相同源码节点在不同任务或调用位置产生不同分配；同一位置重放取得原分配。

v11 至 v12 在事务中创建调用帧表，不重建旧账本或授权记录。既有合法 IR 的指纹不变，新增构造及嵌入工作流进入新程序的指纹。

## 任务子分配的所有权

任务现在拥有自己 allocation 下、未跨越新任务边界的所有子分配。令 `owner(a)` 为从 allocation a 向上遇到的第一个任务，则新 split、join、接纳和派发必须通过该 owner 的当前 lease 校验。推测写入策略也取自这个 owner。

这一规则补齐了从任务根分配继续 split 后的检查：子分配不能因为没有直接对应 tasks 表中的一行，就退回到无任务管辖的资金。旧 worker 或不带 lease 的宿主调用都不能对这些分配发起新工作。任务根 allocation 本身仍只由任务生命周期释放。

嵌套任务作用域可以从当前任务的子预算获得资金。新任务形成新的 owner 边界，父 lease 不能代替子任务 lease。等待操作可以属于任务的任意自有子分配，不再局限于任务根 allocation。

取消或交接时，运行时检查整棵 allocation 子树中的操作责任，关闭尚未暴露的排队工作，随后自底向上 join 分配。已暴露请求及迟到费用继续由运行时负责。此处不依赖 Python 对象析构或某个局部 Budget 变量是否还存在。后续已登记 custody 的资源按当前持有者清理，资金父链保留用于原 attempt 的账单归属，详见 [持久资源控制权](28-durable-resource-custody.md)。

## 正常返回、错误和挂起

正常路径必须显式处理 Budget 接口。根工作流因 Suspended 或执行额度耗尽中断时，分配保持持久状态以供恢复；不会为了回收局部变量而提前关闭恢复还需要的资金。宿主若放弃该运行，需要执行明确的清理策略。

任务被取消、失败后释放或交给运行时管理时，生命周期负责回收未完成局部子分配的 free。已经提交的责任和费用不随局部控制结束而消失。fuel 与协作期限继续跨 invoke、纯函数、循环和任务共享，账本与回执处理不依赖剩余解释器步骤。

## 验证

```powershell
python -m examples.budget_workflows
python -m unittest tests.test_budget_workflows -q
```

示例划出 3 单位工作预算和 2 单位储备预算。工作预算作为独占参数移入 funded，再借给 emit-one 支付一次观察调用；两笔预算在正常路径 join。结果为 hello，确认费用 1、预留 0、根可用余额 9，两个调用帧、两次拆分、两次归还。源码与 SDK 的 IR 相同，恢复后新增事件 0，外部调用仍为一次。

专项测试覆盖子预算不足时拒绝根余额回退、预算参数移动与返回、借用/移动冲突、拆分和关闭顺序、Intent 与 Permit 调用绑定、Operation 返回责任、参数伪造、共享额度中断、调用帧输入冻结、任务子分配租约、推测策略、嵌套作用域、挂起后的费用责任及迟到退款。

六处独立进程退出位于 split 提交前后、调用帧提交前后和 join 提交前后。恢复后均只有一次外部调用、两条拆分和两条归还事件，确认费用 1。schema 迁移测试另验证旧 allocation 身份保留。最终全量测试结果见 [实现状态](08-implementation-status.md)。

## 剩余范围

后续已加入 [Cap 参数、权限衰减与受限调用](21-capabilities-and-attenuation.md)，可通过 invoke_caps_with 同时约束权限与资金；[运行时预算向量](22-dynamic-budget-vectors.md) 已支持计算金额、动态 split/scope/spawn 及首次选择恢复。[受检任务资源](29-checked-task-resources.md) 已支持跨任务句柄移动和单资源返回；后续 [受检资源包](31-checked-resource-bundles.md) 已允许 Intent 和绑定的未消费 Permit 与其他资源一起返回；循环资源状态和一般带资源 sum type 仍需实现，独立标量 Permit 返回仍不开放。

当前借用检查针对同步 invoke 的资金接口，不是通用借用检查器。完整资源环境证明、并发与故障组合模型、ContextView、完整隔离以及语言收益实验仍属于总体目标。
