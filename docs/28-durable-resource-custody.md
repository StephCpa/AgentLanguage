# 持久资源控制权与任务返回

更新日期：2026 年 10 月 26 日。

schema 17 将 Permit、Operation、Budget 的持久控制权接入真实 Store：已登记资源必须在事务中核对当前持有者、generation 和 TaskLease；任务创建原子移动资源包；任务完成发布暂存包；join 原子接收；取消按当前持有者接管责任。新增可选 `retry(..., allocation=...)` 支持为新尝试选择另一出资账户，已经预留的尝试继续使用 schema 16 的固定来源。

**本页记录可信宿主运行时接口。** 后续 [受检任务接入](29-checked-task-resources.md) 已支持 IR、Python PlanBuilder、源码及 TaskRunner 的命名仿射参数和单个 Operation/Budget 返回；后续 [受检资源包](31-checked-resource-bundles.md) 已支持具名多资源包及未消费 Permit 正常返回，后续 [资源任务结果](32-resource-task-outcomes-and-race.md) 已实现 TaskOutcome 与 race 接收；一般业务资源分支已在 [受检业务 Variant](34-checked-business-variants.md) 接通。

## 资源引用与新工作

新增 `al0.custody.ResourceRef(kind, resource, generation)`。它描述 Permit、Operation 或 Budget 的持久身份及代次，不代替 principal 认证、grant、预算、证据、标签或工具契约。

```python
permit_ref = store.manage_resource(run, principal, 'Permit', permit)
budget_ref = store.manage_resource(run, principal, 'Budget', allocation)

with store.resource_access(run, principal, [permit_ref, budget_ref]):
    task = store.spawn_task(
        scope, principal, 'child', child_program, {},
        caps={'work': grant},
        moves={'permit': permit_ref, 'funds': budget_ref},
    )
```

`resource_access` 保存当前调用呈交的引用。它使用 ContextVar 隔离调用上下文，退出或抛异常时恢复原上下文；数据库仍在每个实际操作的事务中重新验证当前控制权。仅进入该上下文不表示已经获准使用资源，也不会刷新过期引用。

一旦资源登记到控制表，原生字符串 ID 不能单独用于新增接纳、派发、重试、取消、预算分割、预算合并或作用域出资。现有 Store 方法仍使用原生 ID 参数，但还必须有匹配的 access context。普通查询、可信业务/费用回执以及历史请求重放不因此获得新执行权。

同一执行主体内部的多个宿主线程仍属于同一 actor；事务防止重复消费和超支，编译器的仿射规则仍需约束程序中的别名。本接口不隔离恶意 Python 宿主或直接数据库修改者。Root 是可信协调者角色，没有在本轮新增独立 root worker lease；Task actor 则必须具有当前 TaskLease。资源 generation 负责父→子→父之后的旧引用失效，TaskLease epoch 负责 worker 恢复之后的旧执行者失效。

## 新旧资源的接入

`manage_resource` 为现有、当前本地持有的资源登记 generation 0。Budget 和 Operation 的未登记初始持有者沿现有 allocation/task 关系验证；登记后使用独立 custody。根预算与任务 ambient allocation 由现有运行/任务生命周期管理，不直接作为可导出 Budget；先 split 得到显式 Budget 接口。

新授权记录 `permit_origins`，区分 root 与明确的任务来源。Broker 的任务授权入口传递 TaskLease，新的任务授权也会被旧 worker 检查阻止。旧数据库中的 Permit 若缺少来源记录，不能根据恢复时的调用者猜测来源来登记。

在 resource_access 中创建的 Permit、Operation 及显式 split Budget 自动登记到当前 actor。`resource_birth` 读取创建时的 generation 0；`manage_resource` 的重复读取也只返回原登记引用。二者都不会把历史别名升级为当前 generation。新建资源的引用需加入后续 access context。

未登记资源保留现有可信宿主接口行为。移动一个 Budget 不会暗中移动原来由它出资的未登记 Operation；该操作继续按原控制规则检查 worker。登记后的资源则无论通过新旧入口使用，都必须接受 custody 检查。

## 存储与原子边界

schema 17 新增：

| 记录 | 内容与作用 |
| --- | --- |
| permit_origins | 新 Permit 的明确来源任务；root 来源为空 |
| resource_custody | kind/id 唯一当前记录、run、owner、generation、Live/Spent/Retired 及初始持有者 |
| task_resource_inputs | spawn 的固定资源描述、转移后引用及发送者 |
| task_resource_returns | 返回描述、暂存引用、Published/Accepted/Retired 状态及接收回执 |

任务输入中的数据字段与资源字段不能重名，资源包中同一 kind/id 不能出现两次。spawn 在原有任务、资金、输入、权限表和标签事务中核对整包，再移动所有引用并保存固定记录。失败会回滚新任务、资金划拨及全部移动。

`finish_task(..., resources=...)` 将选定资源移到 Escrow，同时保存普通结果和完整返回包。此后子任务不能使用它们，父任务也尚未取得控制权。`join_task(..., parent_lease=...)` 在同一事务中接收包、递增 generation，并释放来源任务剩余责任。`task_resource_result` 仅在 Accepted 后返回接收回执。

重复 spawn、finish 和 join 核对相同描述并读取历史决定。资源在 join 后再次移到另一个任务，重放旧 join 仍返回旧回执，不覆盖当前 owner。新调用若拿该旧回执继续工作，会在 custody 检查中失败。

资源包当前由可信宿主提供，尚未与受检子程序的参数和返回类型建立静态对应。Store 会核对实际资源身份、持有者和 action authority，但不能据此宣称任意宿主传入的字段名称已经满足 AL-0 类型接口。

## 权限、资金与退出

Permit/Operation 移入子任务时，其实际 grant 必须处于固定子任务权限表的对应派生链内。向父任务返回时，同样核对父任务的持久权限表；Root 接收者属于已认证的可信宿主。任务在 access context 中新建受管动作资源，也不能超出任务表。接纳、派发和重试继续验证当前 grant 链活性、有效期、证据和原工具契约。

推测写入检查按当前任务 actor 执行。一个在父方已经接纳、尚未派发的写操作移入推测任务后，仍不能在那里派发；资金来源在普通父任务中不会绕过推测边界。

每次重试都验证 Operation 当前控制权及所选 Budget 的使用权。省略 allocation 时沿用原默认出资账户；指定新账户只影响新增 attempt。旧尝试的账单和退款始终读取其不可变出资记录。

取消或退出只接管当前仍由该任务或其 Escrow 持有的资源：未消费许可退休，操作交给 Runtime 并停止新增派发，queued 预留释放，exposed 预留保留，当前持有的显式预算关闭。已经接收给父方的资源不再属于来源任务的清理集合。任务整体和后代的取消仍按任务树展开。

父方是另一个任务时，返回资源随新任务 actor 的退出处理；父方为 Root 时，由可信协调者承担后续显式使用、关闭或责任处理。来源任务与来源 scope 可以结束，不能因此回收已合法返回给 Root 的资源。

金融父链不因控制权变化重接。导出的 Budget 可以在其历史金融祖先关闭后继续存在；迟到退款从原 attempt 账户沿固定父链寻找开放账户。内部生命周期清理允许关闭一个金融账户而保留独立持有的后代接口，普通手工 `join_budget` 仍要求先处理开放子账户。

## 实际执行与测试

```powershell
python -m examples.affine_task_resources
python -m unittest tests.test_resource_custody -q
```

[示例](../examples/affine_task_resources.py) 使用真实 Store 与另一份 SQLite 端点数据库。父方把 Permit 和 Budget 移给子任务；子方接纳并派发，再发布 Operation 和 Budget；父方接收后关闭来源任务/scope，用第二个账户重试；端点先后完成两次请求，再按独立记录提交账单与业务证据。

结果为一次业务效果、两次独立收费，总预算 10 中确认费用 2、可用 8、预留 0。两次 attempt 的出资账户不同。再次读取 spawn/join 历史不新增事件。结果记录见 [resource_custody_example.json](../models/resource_custody_example.json)。这是一个有独立端点记录的具体执行，不是通用 exactly-once 证明。

33 项专项测试覆盖字符串入口拒绝、重复别名、包描述变化、不同 actor 和 generation、worker 恢复、暂存区隔离、ABA、重放不重新赋权、跨任务权限收缩、撤权、推测写入、独立出资、嵌套任务、已关闭金融祖先、上下文释放及迁移。

并发测试验证两个 join 只产生一次接收，以及 join/取消竞争后资源只归接收者或 Runtime 一方。8 个真实进程退出位置覆盖 spawn、publish、receive、cancel 各自的事务内与提交后。事务内退出恢复原持有者；提交后退出保留完整新状态；暴露尝试的费用责任在取消后继续存在。

完整回归 601 项测试全部通过，用时 370.758 秒，记录见 [custody_full_regression.log](../models/custody_full_regression.log)。安装后的 wheel 在项目目录之外通过基础与生成 SDK 的静态正反例检查，并实际执行资源登记、受管预算分割及无引用访问拒绝；记录见 [sdk_wheel_audit.json](../models/sdk_wheel_audit.json)。该安装验收没有为可信 Store 内核新增静态类型保证。

## 迁移与未完成项

v16→v17 创建新结构，不回填猜测的 Permit 来源，不自动把旧引用提升为新控制权。已有 attempt 出资、根账本、任务权限表和程序指纹保留。旧 Permit 可按原可信宿主授权路径使用，但缺少来源证据时不能直接登记为仿射移动资源。

ResourceRef 已通过后续受检任务接口接入 IR、SDK、源码、TaskRunner 的持久输入/单资源结果重建与调用帧。后续 [资源任务结果](32-resource-task-outcomes-and-race.md) 已实现 Completed 包接收和 race winner 接收。Failed/Waiting/Cancelled 仅报告执行状态及 Runtime 接管的责任，不伪造可接收的原输入包；一般业务资源分支已在 [受检业务 Variant](34-checked-business-variants.md) 接通。

后续受检接口已保守传播资源输入和单资源返回的标签；后续 [受检资源包](31-checked-resource-bundles.md) 已提供封闭字段描述并保留字段与包的标签。直接调用 low-level Store 仍由可信宿主负责相应标签参数。未提供可信宿主之外的沙箱、独立 Root worker fencing、任意资源包静态证明或任意任务树的模型对应证明。[上一轮有限模型](27-affine-task-transfer.md) 的 `runtime_integrated: false` 仍表示尚未建立该模型与完整运行时的对应验收，不能仅因核心接口接通就改成 true。
