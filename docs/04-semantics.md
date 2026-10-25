# AL-0 形式语义与证明计划

形式化目标是将语言检查、资源账本与真实派发连接起来。以下给出核心判断、状态与证明义务；它们是待完成的形式化设计。本轮模型检查只覆盖 [模型报告](06-model-results.md) 中列出的有限切片。

## 抽象语法

```text
v ::= unit | bool | int | record | variant | function | protected_ref
e ::= v | x | let x = e in e | if e then e else e
    | match e with arms | call f(args) | bounded_for(n, body)
    | infer(req) | observe(query) | check(v,p,deps) | prepare(args)
    | authorize(intent) | admit(slot,intent,permit,budget)
    | inspect(op) | await(op) | reconcile(op)
    | split(budget,q) | scope(e) | spawn(e,resources) | join(task) | cancel(task)
```

时钟、随机数和模型调用是运行时事件，LLM 的返回关系允许任意 schema 合法值和显式失败。checker 的正确性是对所声明谓词的前提，不能由“模型说通过了”实现。

## 类型判断

```text
Γ ; Δ ; pc ⊢ e : τ ! E ▷ Ω ⊣ Δ'
```

Γ 包含可复制的普通值和共享只读能力；Δ 包含独占预算接口、permit 和 task 控制权；pc 是当前控制依赖标签；E 是可能请求的效应；Ω 是未能静态解除的动态检查义务。

当前 IR 的分支资源实现进一步区分可复制 OpRef 与 Operation 处理责任。后者需要独立的责任环境 O，不能由复制一个观察引用而隐式消除；分支输入移走责任，返回 Operation 再在父环境建立责任。现有检查规则、恢复对应和仍未完成的证明义务见 [分支资源传递](18-resource-branches.md)。

当前跨任务 Cap 实现属于 Γ 中的可复制授权引用。spawn 的持久权限表限制环境工具调用，显式 Cap 参数和结果还必须满足该表的派生包含关系；恢复不能重新扩大表。此路径不消耗或转移 Δ 中的 Permit、Budget 或操作责任，详见 [跨任务 Cap 协议](23-task-capability-transfer.md)。

跨任务仿射资源的后续协议把当前 custody、worker epoch 与 attempt 的不可变出资记录分开。原子资源包先移动到子任务，返回时经过持久暂存区，再由 join 接收；重放历史结果不能重建当前控制权。[协议与有限模型](27-affine-task-transfer.md) 覆盖 1,629 个状态、5,174 条转移及九个错误变体，尚未证明任意任务树或与运行时完整对应。attempt 出资记录已接入 schema 16，后续 [schema 17 的可信 Store 接口](28-durable-resource-custody.md) 已实现 custody 和暂存返回；后续 [受检任务接口](29-checked-task-resources.md) 已接通命名资源输入、单资源返回及 TaskRunner，[受检资源包](31-checked-resource-bundles.md) 已进一步接通嵌套包和未消费 Permit 的正常返回；后续 [业务 Variant](34-checked-business-variants.md) 已接通资源结果分支，完整模型对应仍待完成。

Ω 的元素包括 `SameArtifact`、`CurrentAuthority`、`ScopeMembership`、`CompatibleSink`、`BudgetAvailable`、`EndpointContract` 和 `SameSlotDescriptor`。每个义务有检查位置和所绑定对象，不能只在 IR 中存一句无执行含义的描述。

运行时预算另产生 `ResourceVectorWellFormed`、`KnownDimensions` 和 `LocalFreeAvailable` 义务。resources 构造只产生可复制数据，真正的资金分割仍发生在账本。动态分配位置首先持久化金额及控制标签，随后执行原有资金事务；求值记录本身不改变 F/R/C/Q，恢复不能用新的求值覆盖旧转移金额。具体实现规则与崩溃证据见 [运行时预算向量](22-dynamic-budget-vectors.md)。

### 普通组合

顺序：第一段消耗后的 Δ₁ 作为第二段的输入，效应取并集。分支：两条路径的剩余独占资源必须可合并；否则返回表示状态差异的 sum type。循环：每次迭代须满足同一资源状态接口，或显式携带资源状态；禁止把一次性 permit 当循环不变量。

并发：Δ 分割为互不重叠的子上下文，不能通过记录、闭包捕获或别名共享同一个仿射值。分割预算句柄之前还需要账本 split；把一个句柄拆成两个源码变量不是账本转移。

### 特定规则

```text
INFER
  schema(T) valid; Infer(model) allowed by effect boundary
  -----------------------------------------------------
  infer<T>(...) : Result<Candidate<T>, InferError>
  obligations += capability, sink, resource bound

CHECK
  checker(P) registered; dependency schema valid
  -----------------------------------------------------
  check(v,P,D) : Result<Checked<T,P>, CheckError>
  receipt binds immutable identity(v), P, checker version, D

ADMIT
  intent : Intent<A>; permit : Permit<A>; budget : Budget<R>
  -----------------------------------------------------
  admit(...) : Admission<A>
  permit is moved; result explicitly returns any retained right
  obligations += exact binding, current authority, budget, contract

SPAWN
  Δ = Δ_parent disjoint_union Δ_child
  Γ ; Δ_child ⊢ e : T ! E_child ▷ Ω_child ⊣ Δ_return
  -----------------------------------------------------
  spawn(e, move Δ_child) : Task<T, Δ_return>
```

`Task<T,Δ_return>` 是元层表示；表面语法可隐藏资源返回细节，但检查器必须维护它们。异常和取消也要有输出资源状态。未决 R 不以 Task 返回值形式复制，归持久运行时所有。

## 运行配置

```text
Σ = ⟨Tasks, Heap, Slots, Operations, Attempts, Ledger,
      Grants, Policy, History, EndpointWorld, BillingWorld⟩
```

EndpointWorld 和 BillingWorld 独立，允许业务无效果而收费、业务成功而账单尚未到达。History 记录来源、授权、请求、网关接纳和最终证据，不只记录函数返回值。

调度语义允许任意任务交错。崩溃丢弃易失栈及未提交事务，保留已提交的 Slots、Operations、Attempts、Ledger、Grants 和 History。外部世界在本地崩溃时继续变化。

## 关键状态转移

| 转移 | 前提 | 持久变化 |
| --- | --- | --- |
| AdmitNew | slot 不存在，许可匹配，额度足够 | 消费许可、预留、插入操作及首 attempt，一步完成 |
| AdmitReplay | slot 已有相同描述 | 返回既有决定，账本不变 |
| GatewayAccept | 当前 epoch 有效、未取消、未过期、attempt 有预留 | 持久标记 DispatchStarted |
| Send | 已取得该 attempt 的派发执行权 | 外部世界可能开始处理 |
| CancelQueued | 尚未 DispatchStarted，且与派发竞争原子解决 | 排除发送并释放预留 |
| CancelExposed | 已 DispatchStarted | 仅记录取消请求 |
| RecordBusiness | 绑定且最终的业务证据 | 记录业务结果，不隐式释放费用 |
| SettleBilling | 最终费用证据、尚未结算、费用不超上界 | R 移入 C 和 F |
| Retire | 未决责任被永久占用 | R 移入 Q，不可再消费 |
| Retry | 契约支持、当前授权有效、attempt 数及预算允许 | 同 operation 下新增 attempt 和预留 |
| Revoke | 授权方合法 | epoch 单调增加或 grant 撤销 |
| CrashRecover | 已提交存储完整 | 重建控制状态，不回滚资源事实 |

`Send` 如何保证执行权不会产生多次未计费发送，是适配器实现义务。首版每个 attempt 的发送执行权只能由一个活进程使用一次；进程丢失后不重发这个 attempt。需要重试时另建 attempt，外部业务去重仍使用原 operation 的 key。这样避免把队列 redelivery 隐藏成不计费的同一尝试。

## 目标性质

| 编号 | 要证明的性质 | 主要前提 |
| --- | --- | --- |
| T1 | 类型保持和受保护引用不可伪造 | checker、解释器和序列化边界正确 |
| T2 | 每个网关接纳事件均满足当时授权 | 完整介导、当前 epoch 与接纳原子排序 |
| T3 | 被执行描述与被授权描述一致 | 规范化正确、内容不可变、适配器编码保持 |
| T4 | 根预算每个维度保持 F+R+C+Q=B | 原子资源转移、独占分配、结算去重 |
| T5 | 实际费用受 C+R+Q 覆盖 | 可靠费用上界、全部发送受介导、最终费用证据 |
| T6 | 恢复不重复创建同 slot 的操作或复活许可 | slot 唯一、消费记录持久、activation 稳定 |
| T7 | 检查证据仅用于被检查 artifact 和依赖 | checker 可靠、receipt 不可伪造、身份匹配 |
| T8 | 声明的版本条件在修改点成立 | 端点原子检查与修改覆盖相应条件 |
| T9 | 受介导的显式数据发送符合标签政策 | 所有出口标注、转换与控制依赖标签保守 |

T2 的事件是网关接纳，不是远端完成。T5 不能只由 T4 推出。T7 不推出测试充分；T8 不覆盖端点无权检查的其他系统状态。首版不主张任意隐式侧信道非干扰、任意外部服务 exactly-once 或任务一定完成。

### 预算归纳证明骨架

初态 F=B，R=C=Q=0。reserve 在 F 与 R 之间搬移同量；结算把 u 拆成 c 与 u−c；retire 把同量从 R 移到 Q；委托只移动 F 的归属。每个转移保持等式和非负性。重复证据为 stutter，不重复移动资源。取消 exposed 不改变任何余额。

外部费用界还需要覆盖关系：每个可能收费的发送必须有一个尚在 R/Q 中的独占上界，或已进入 C 的最终结算。只有结算确认其余费用不再可能产生，才把余量移回 F。计费与账本分别建模，才能检验覆盖而非只检验自洽的数字。

### 恢复身份证明骨架

唯一 slot 表使每次 AdmitNew 最多创建一条 operation；重复 admission 由相同描述判断变为 stutter。CrashRecover 不修改唯一表和许可消费。重放从历史获得同一 activation；resample 或 fork 则明确分配新 activation，并重新申请资源。因此新计算不会冒充历史重放，历史恢复不会生成新接纳权。

## 编译正确性

定义源配置 S 与 IR/数据库配置 I 之间的表示关系 `Rep(S,I)`，至少保存节点身份、动态 activation、已接纳操作、受保护值映射、资源归属、标签和未解除义务。

要求每个具体步骤要么对应源语义的一步或多步，要么是对可观察轨迹无影响的 stutter。可观察事件包含网关接纳、业务效果、计费和授权变化；仅比较最终返回值不足以证明正确。

崩溃恢复转换的专门义务是：已提交 effect site 对应同一个 slot；模型结果用历史值恢复；未完成 effect site 返回既有 op 或维持 Unknown；不能重新运行一个已记录模型调用来“恢复”旧答案。

接入已有守恒协议也不能跳过此证明。相关预算工作已明确给出抽象到具体的对应义务，AL-0 需进一步把语言及 lowering 纳入关系，而非重复声称有一个抽象不变量即可。[预算守恒论文第 7.7 节](https://arxiv.org/html/2610.00349v1)

## 活性和不能兼得的要求

不可查询且不可去重的端点，在发送后响应丢失时，本地无法区分已生效和未生效。自动重发可能重复，不重发可能失去进展。AL-0 通过显式 Unknown 暴露这一条件，不把它转成异常后静默重试。

安全不需要网络最终恢复；最终完成需要公平调度、存储可用、端点或人工提供最终证据，以及足够的对账预算。保持未知是合法的安全状态，但实验中不能计为任务成功。

## 证明实施顺序

先在纯核心上证明 substitution、preservation 与仿射资源分割；随后加入动作与预算的状态机；再加入结构化并发、取消和崩溃；最后证明 checked IR 到单协调器实现的对应。

建议选择一种证明助手完成窄核心，不并行维护多套形式系统。有限状态检查用于发现转移缺项，故障注入用于检验实现对应，二者都不能替代无界归纳证明。选定工具及维护者后再冻结证明工程。

已完成的窄组合检查见 [组合协议模型与运行时一致性检查](24-combined-protocol-validation.md)：一操作、两次尝试、祖先撤权、取消、一次崩溃/恢复与独立费用记录。模型状态与 Store/端点观察之间已有可执行比对，覆盖选定生成调度和五类进程退出位置；目前没有对所有图边、所有具体执行或完整语言证明 `Rep` 的模拟关系。
