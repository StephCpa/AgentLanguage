# AL-0 核心语言规范

本规范为 0.1 设计草案，定义可实现的首版核心；示例为拟议语法。运行协议是其外部操作的含义，见 [持久运行时协议](03-runtime-protocol.md)。

当前已实现的 `.al0` 表面语法见 [源码前端与共享 IR](15-source-frontend.md)。它覆盖顺序调用、任务、结果分支、标签、纯函数、有界循环、只读操作借用、Admission 分支、Budget 及工具 Cap；本草案中的一般资源多态、高层语法糖和完整隔离尚未全部实现。

## 值与计算

普通值包括 `Unit`、`Bool`、有界整数、定点数量、字符串、不可变记录、枚举、列表和纯函数。预算使用最小计量单位的非负整数，禁止浮点金额。泛型首版只支持显式类型参数，不实现高阶类型或任意依赖类型。

纯表达式确定、无网络、无宿主文件系统、无系统时间或随机源。时间、随机种子、模型结果、外部观察和并发选择均通过受介导事件进入程序并记入历史。有界循环在入口检查上限；纯计算仍受本地 fuel、内存与墙钟约束，耗尽返回显式执行错误。

`fn` 定义纯函数；`workflow` 定义可持久化计算；`agent` 是以后展开为 workflow 和配置的语法糖。所有跨持久边界的局部值必须实现受检序列化，受保护对象仅序列化不具授权能力的引用。

## 类型分类

| 类型 | 可复制 | 构造者 | 含义 |
| --- | --- | --- | --- |
| `Candidate<T>` | 是 | 解码器或纯候选包装器 | 结构有效，未承诺业务正确 |
| `Observation<T>` | 是 | 登记的观察适配器 | 值与来源、时间、版本及标签 |
| `Checked<T,P>` | 是 | 已登记 checker | 对不可变值和依赖执行过 P |
| `Intent<A>` | 是 | `prepare` | 未授权、不可变的动作描述 |
| `Cap<K,S>` | 依 K | 宿主能力表 | 允许的权限种类 K 和范围 S |
| `Permit<A>` | 否 | 授权服务 | 对确定动作及重试策略的接纳权 |
| `Budget<R>` | 否 | 资源账本 | 对确定分配身份的消费接口 |
| `OpRef<A>` | 是 | 成功接纳或运行时恢复 | 已接纳操作的观察引用，本身不赋予重发权 |
| `Task<T>` | 否 | `spawn` | 子任务控制权，须 join 或登记接管 |
| `Plan<I,O,E>` | 是 | 计划检查器 | 输入输出和效应边界已检查的代码 |

当前 IR 进一步区分 `Operation<A>` 责任接口和可复制 `OpRef<A>`：前者可移动到分支并承担显式处理义务，后者通过 `borrow_operation` 建立，只能 inspect。二者都引用同一持久操作，借用不转移预算或产生重发权。实现及尚未完成的资源接口见 [分支资源传递与操作观察借用](18-resource-branches.md)。

当前 Cap 子集采用 `Cap<"tool">`，可复制，scope 来自可信工具目录。期限、每操作尝试上限和顶层字段等值约束可以单调衰减；父链撤销在授权、接纳、派发及重试时复核。`invoke_caps` 同时限制环境工具表和显式保护引用，详见 [Cap 权限衰减与受限调用](21-capabilities-and-attenuation.md)。

任务也可以显式接收和返回 Cap；spawn_caps 限定子任务权限，普通 spawn 固定继承的权限表。join、TaskOutcome 和 race 保留 Cap 类型与标签，详见 [跨任务 Cap 传递](23-task-capability-transfer.md)。

当前资源金额可以通过纯 resources 构造及计算形成普通向量，再用于 split、scope 或 spawn；首次使用位置持久化金额与控制标签。向量可复制，Budget 所有权不可复制，详见 [运行时预算向量](22-dynamic-budget-vectors.md)。

`Candidate` 的目的在于阻止隐式流入要求证据的接口，不阻止读取字段。写成 `Candidate<T>` 并不会神奇地使所有使用都安全；敏感工具必须在签名和网关策略中要求对应证据。

`Checked` 在运行时携带 `(value_id, predicate_id, checker_version, dependency_vector, receipt_id)`。首版不把每个摘要编码成编译期类型参数；编译器检查结构关系，运行时核对具体身份。把动态摘要强行放进依赖类型，会增加模型生成和宿主 API 复杂度。

受保护类型无公开构造函数、无通用反序列化和无反射访问。文本中出现 permit ID 不赋予使用权：网关还检查宿主主体、租户、run 绑定和授权记录。JSON 中的 `approved: true` 始终是普通数据。

## 权限和效应分离

`uses` 表示程序可能请求哪些效应。实际执行还需要能力、当前策略和必要的具体许可。效应本身不携带授权。

首版采用封闭的参数化效应集合：

```text
Infer(model), Observe(tool, scope), Check(checker),
RequestApproval(scope), Admit(tool, scope), Reconcile(tool, scope),
Spawn, Cancel, ReadMemory(scope), WriteMemory(scope)
```

`prepare` 是纯描述构造。网关、模型调用和 checker 子进程仍有独立的费用及数据发送检查。`check` 不是任意 Python 回调；它调用受审计 checker，且资源消耗进入账本。

没有自定义效应处理器绕过网关的扩展点。模拟后端可以解释请求，但事件仍区分 requested、simulated、denied、accepted；模拟回执不能满足生产写入要求的证据类别。请求轨迹不能因 mock 消除外部效果而被抹去，这一点沿用 ETAS 的区分。[ETAS](https://arxiv.org/html/2607.17780v1)

## 核心操作签名

```text
infer<T>(request, cap, &mut budget)
    -> Result<Candidate<T>, InferError>
observe<T>(query, cap, &mut budget)
    -> Result<Observation<T>, ObserveError>
check<T,P>(value, checker, dependencies, &mut budget)
    -> Result<Checked<T,P>, CheckError>
prepare<A>(args, evidence, contract) -> Intent<A>
authorize<A>(intent, authority)
    -> Result<Permit<A>, AuthorizationError>
admit<A>(slot, intent, move permit, &mut budget)
    -> Admission<A>
inspect<A>(op: OpRef<A>) -> OperationSnapshot<A>
await<A>(op: OpRef<A>, wait_policy) -> OperationSnapshot<A>
reconcile<A>(op: OpRef<A>, reconcile_cap, &mut budget)
    -> Result<OperationSnapshot<A>, ReconcileError>
```

`Admission<A>` 有两个语言级分支：`Admitted(OpRef<A>)` 和 `Rejected(reason, ReturnedPermit<A>)`。确认未接纳时才可返还仍有效的许可；校验失败导致许可失效则返回 `NoPermit`。把返还包装成枚举，防止失败路径自动复制许可。

当前可执行形式为 `try_admit` 与 `match_admission`，将两层结果展开为 Admitted、Returned、NoPermit 三个穷尽分支；Admitted 交付 Operation 责任接口。成功和拒绝决定均持久化，返还许可只可在新 slot 尝试。原 admit 的 Operation/异常接口保留用于已有程序。具体规则见 [持久 Admission 与许可返还](19-admission-outcomes.md)。

接纳请求自身断网时，运行时保留 slot 并执行接纳查询；尚不能判断是否接纳，向控制器暴露 `AdmissionUnresolved(slot)`，不把原 permit 重新交给程序。这是宿主 API 的第三种结果。受检 workflow 在该边界挂起，直到能恢复确定决定，或转入带未决责任的运行状态。

`commit` 作为表面组合可展开为 `admit` 后 `await`，但不能把两者描述为一个跨网络原子步骤。`inspect` 读取本地已持久信息；远端查询使用 `reconcile`，可能产生新费用和数据发送。

## 动作结果采用三个维度

```text
OperationSnapshot<A> {
    operation: OpRef<A>,
    dispatch: Queued | DispatchStarted | Closed,
    business: NotDispatched | Pending | Unknown
              | Applied(Receipt<A>) | ProvenNoEffect(Evidence),
    billing: BillingSummary {
        confirmed: ResourceVector,
        reserved: ResourceVector,
        retired: ResourceVector,
        attempts_closed: Bool
    },
    cancel_requested: Bool
}
```

这些状态是运行时事实的快照。复制快照不能驱动操作发生倒退；变更由账本单调决定。每个 attempt 的计费状态为 `Reserved(u)`、`Settled(c)` 或 `Retired(u)`，operation 汇总全部 attempts，因此可以同时存在已结算费用和未决预留。`attempts_closed` 表示不再创建 attempt 且既有 attempt 均已结算或退休，不表示所有真实费用均已查明。最终业务结果仍可能带有非零 reserved；retired 表示保守占用，不表示实际已收费。

对 `Unknown` 的模式匹配必须显式处理，禁止隐式转换为 `None` 或普通失败后自动重试。语言不要求程序永远等待；可以返回 `Waiting(op)` 或 `NeedsReconciliation(op)`，责任移交运行时服务。若业务流程遗忘 op，运行时也不释放其预留。

## 分支和资源规则

普通变量在 Γ 中，独占控制权在 Δ 中。顺序计算将 Δ 输出传给下一段。两个并行分支只能接收不相交的独占资源；可共享只读能力可以借用。分支合并要求每条路径的资源状态一致，或显式返回区分这些状态的枚举。

`Budget.split(q)` 是账本中的原子转移，不是复制一段余额。父预算减少可支配份额，两个子分配获得不同身份。`join` 只回收已确认可用余额；子任务已预留、已花费和退休份额仍属于同一根预算，不回收为新额度。

首版函数作用域内支持受限借用，不允许借用跨 spawn；子任务通过 move 获得分配。每个 `Task` 必须 `join` 或 `handoff_to_runtime`。异常退出时作用域自动登记取消请求和后续接管，不能要求远端立即结束。

仿射规则保证不重复使用，但不保证资源一定被归还。`drop` 一个 permit 使它不可再由该程序使用；`drop` 一个 Budget 触发运行时归还可用份额；`drop` OpRef 无账本变化。这些释放协议必须显式定义，不能只依赖宿主析构器。

## 结构化并发和推测执行

`scope` 管理子任务生命周期；`spawn` 创建子任务；`join` 等待本地终态或已接管的未决状态；`cancel` 只记录请求。`race` 的获胜选择持久化，恢复时不重新竞赛。

`speculate` 允许带预算的推理、读取和沙箱操作，禁止任何共享外部写入效应，包括共享记忆写入。并非仅禁止名为 `commit` 的语法：编译器按传递效应集合检查嵌套调用和生成计划。胜出分支把候选、证据和 intent 返回父作用域，父作用域再授权接纳。

读取也可能外发信息，沙箱计算也可能消耗成本；其效应仍存在。所有分支共享秘密时，选择了哪个分支也形成控制依赖，数据标签不能因只保留获胜结果而丢失。

## 上下文和信息流

`ContextView` 显式记录材料引用、来源、接收主体、token 上限和截断策略。上下文中的控制指令由宿主固定，外部材料保留来源和完整性标签。凭据只能作为网关句柄，不能进入模型上下文。

每个值的保密标签由可接收主体集合或有序等级表示；合并取更严格限制。完整性标签单独维护，不能用同一个高低序同时表示保密和可信程度。纯计算、摘要、记忆写入和模型输出继承输入与控制依赖的约束。

发送到模型、检索、日志、缓存和其他 Agent 都执行 sink 检查。检查某个事实正确不等于允许公开它。释放限制必须由独立的可信 declassification 操作授权，记录释放内容和目标。

首版目标是完整介导的显式发送规则与保守控制标签，不主张涵盖时序、计费或资源竞争侧信道的非干扰。与 CaMeL 的控制数据分离进行比较时，应保留这一保证范围差异。[CaMeL](https://arxiv.org/abs/2503.18813v2)

## 模型生成计划

模型只生成普通 `Candidate<PlanAst>`。`check_plan` 做名称解析、类型与效应检查、循环界限检查及能力需求提取，得到 `Plan<I,O,E>`。`execute` 时宿主只绑定允许的实际能力，计划文本中的名字不是权限本身。

计划不能声明新的 trusted checker，不能导入任意插件，不能重定义工具契约，不能把外部字符串当可执行代码。每次修复生成新的计划版本；已接纳操作仍归旧版本和旧 slot。重新规划复用同一业务目标时，必须先查询尚未解决的旧动作，不能通过换计划绕过已有占用。

动态检查失败产生结构化诊断，模型可以修复候选，但诊断里不包含秘密值或真实凭据。模型再尝试也消耗调用预算。

## 代码修复示例

```text
workflow repair(repo, issue, authority, move budget)
uses { Infer(CodeModel), Observe(Repo, repo), Check(PatchScope),
       Check(Tests), RequestApproval(repo), Admit(Repo, repo) }
{
    let snapshot = observe Repo.snapshot(repo, authority.read, &mut budget)?;
    let draft = infer<Patch>(context(issue, snapshot), authority.model, &mut budget)?;
    let scoped = check(draft, PatchScope(issue), snapshot, &mut budget)?;
    let tested = check(scoped, Tests(TestSuiteV3), snapshot, &mut budget)?;
    let intent = prepare Repo.merge(
        repo, tested.value, tested.receipt, expected = snapshot.version);
    let permit = authorize(intent, authority.merge)?;
    match admit(slot("merge"), intent, move permit, &mut budget) {
        Rejected(reason, returned) => return NeedsRevision(reason, returned),
        Admitted(op) => {
            let state = await(op, Wait.up_to(2m));
            return match state.business {
                Applied(receipt) => Done(receipt, state.billing),
                ProvenNoEffect(reason) => NeedsRevisionWithOperation(reason, op),
                NotDispatched | Pending | Unknown => Waiting(op)
            };
        }
    }
}
```

`slot("merge")` 是编译器登记的静态位置与当前动态激活身份的组合，不是全局字符串键。工具签名要求端点原子检查版本；所有依赖是否可在提交端检查，应由适配器声明。测试环境依赖仅支持测试证据，不自动获得跨系统原子新鲜度。

## 诊断契约

| 代码 | 拒绝条件 | 建议修复 |
| --- | --- | --- |
| AL1001 | 复用已移动 permit | 查询已有操作，或为新的动作申请独立授权 |
| AL1002 | 分支共享独占预算 | 显式拆分或使用同一账本接纳接口 |
| AL2001 | 候选流入要求检查证据的参数 | 调用签名要求的 checker |
| AL2002 | 证据与 artifact 身份不符 | 使用被检查对象或重新检查 |
| AL3001 | 推测分支存在外部写入 | 返回 intent，选定分支后接纳 |
| AL3002 | 未处理 Unknown | 返回操作引用或执行有权限的对账 |
| AL4001 | 工具缺少要求的原子版本契约 | 选择支持的适配器或降低明确的业务要求 |
| AL4002 | 恢复 slot 与历史描述不符 | 使用旧版本恢复或执行显式迁移 |
| AL5001 | 发送标签与接收方不兼容 | 使用可接收端点或取得对应释放授权 |

每个诊断包含源码范围、相关值定义位置、当前所缺证据和一条局部修复建议。语言体验实验应分别消融错误文本与静态规则，避免把更好的提示误归因为新语言语义。
