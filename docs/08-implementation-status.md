# AL-0 实现状态与后续验收

更新日期为 2026 年 10 月 26 日。项目已从设计模型进入可执行参考实现。当前代码覆盖受检顺序与任务 IR、Python SDK、核心源码前端、纯函数与表达式、有界循环及共享步骤额度、持久条件分支、分支资源移动与只读操作借用、Admission 与许可返还、Budget 参数与持久工作流调用、Cap 权限衰减与受限调用、运行时预算向量、跨任务 Cap 与持久权限表、单存储持久执行核心、带预算的对账、未决责任查询、预算分配树、持久任务、受限推测执行及信息流出口检查；原设计的完整语言、运行时和研究实验仍在推进。

## 已实现的路径

`examples/repair.ir.json` 经 `al0.ir.check_plan` 检查，再由 `al0.execute.Interpreter` 执行。`examples/repair.al0` 经 `al0.frontend.compile_source` 生成与该 IR 及 Python SDK 相同的计划。模型、观察、checker 和业务动作都经 Broker 进入相同的 `Store`，需要宿主绑定的实际 grant。

模型生成的是 JSON 数据，不运行 eval、Python 源码或任意导入。检查器拒绝候选绕过检查、JSON 伪造许可、许可重复使用、许可与 intent 不匹配、未声明效应和未处理的业务状态。所有节点为 SSA，程序及工具目录的描述指纹绑定 run。

数据库使用 SQLite WAL、FULL synchronous 和 BEGIN IMMEDIATE。许可消费、预算预留、slot、operation、attempt 与 outbox 同事务提交。当前数据库 schema 版本为 21；支持从版本 2、3、4、5、6、7、8、9、10、11、12、13、14、15、16、17、18、19 或 20 事务式迁移。其他旧版本和含表但未登记版本的数据库显式拒绝，通用迁移仍待实现。信息流语义版本参与程序指纹；结构迁移保留旧账本，不授权新解释器以不同语义的旧程序身份恢复执行。v8 至 v9 增加条件决策表，v9 至 v10 增加循环进度表，v10 至 v11 增加持久接纳决定表，v11 至 v12 增加工作流调用帧，v12 至 v13 增加能力绑定和派生链，v13 至 v14 增加动态金额选择记录，v14 至 v15 增加任务权限表，v15 至 v16 增加不可变 attempt 出资记录，v16 至 v17 增加资源 custody、明确 Permit 来源及输入/返回记录，v17 至 v18 增加独立推理终态结果表，v18 至 v19 增加循环资源交接回执，v19 至 v20 增加推理观察与控制交接记录，v20 至 v21 增加推理接纳决定。这些结构迁移保留既有账本。当前审计修复另将信息流与执行位置语义设为版本 2，改变 Catalog/计划指纹并拒绝旧受检执行恢复，详见 [审计修复](46-audit-remediation.md)。

接纳结果按 slot 恢复；授权调用按稳定键恢复同一 permit；checker 以原检查 operation 恢复同一证据。已取得派发权的 attempt 不会在重放中重新发送。新的重试 attempt 独立预留费用，并检查当前授权、次数和端点去重有效期。

业务终态和费用结算独立。取消已经派发的请求保留预算；最终无业务效果不自动退费；晚到费用按原 attempt 结算；重复证据不重复消费。超过费用上界会保存实际金额并停止新接纳，金额不会被截断或因 SQLite 整数溢出转成近似浮点数。

## 当前测试证据

固定提交审计的 F1–F7 已修复并通过组合验收：Linux Python 3.10.22 和 3.12.15 分别运行 950 项测试，均无跳过并包含固定 mypy 1.18.2；symlinks/copies venv 的独立 wheel 消费者也均通过。CI 记录、语义升级和旧记录兼容规则见 [审计修复](46-audit-remediation.md) 与 `models/audit_ci_acceptance.json`。下述 932 项为修复前基线证据。

2026 年 10 月 26 日执行 `python -m unittest discover -q`，并通过 `AL0_TYPING_PYTHON` 启用固定版本类型工具，932 项测试全部通过，用时 825.434 秒，日志保存在 `models/inference_provider_fixed_full_regression.log`。测试覆盖跨任务仿射协议有限模型、可信 Store 资源控制权与暂存返回、受检任务资源输入/单资源返回/嵌套接收/标签/Admission 返还组合、混合与嵌套 Bundle/Permit 字段依赖/完整拆包/整包再次转移及恢复、资源 TaskOutcome 一次匹配/完成取消原子决定/race 胜者接收及败者迟到费用、纯数据/证据/资源 Variant 的穷尽匹配/任务传递/空资源义务/嵌套标签/真实进程恢复、infer_result 终态分类/独立结果记录/迟到计费/未决恢复/跨任务返回及 schema 18 迁移、资源 repeat 的固定回执/游标与代次原子提交/嵌套任务及循环/取消与旧引用拒绝/schema 19 迁移、infer_progress/resume_infer 的固定 Waiting/终态观察与代次转移/任务循环/取消及并发事实/迟到账单/schema 20 迁移、try_infer 固定拒绝/原子授权与接纳/回滚清理/并发额度争用/标签与取消/schema 21 迁移、可信提供方拒绝/伪造标记拒绝/已接纳派发阻止/旧接口异常优先级/取消与迟到费用、attempt 出资迁移与进程退出、静态拒绝、宿主授权、证据绑定、并发接纳、撤权与派发竞态、取消与派发竞态、账本结算、解释器重放、源码/SDK/IR 等价、SDK 公共构造接口静态正反例与结果类型传播、源码诊断与展开限制、纯度与实参检查、纯运算与数据上限、共享步数及协作期限、有界循环与进度恢复、条件选择及输入快照、资源捕获与移动、只读操作借用、持久 Admission 与许可返还、Budget 参数与工作流调用、Cap 权限衰减/传递撤销/受限调用、动态预算向量与持久金额选择、跨任务 Cap/权限表/结果传递、撤权/取消/重试/崩溃恢复的组合生成调度与独立费用检查、付费对账、回执应用崩溃恢复、预算分配和合并、任务子分配租约与责任、任务 IR、持久输入、TaskOutcome 分支、旧工作线程拒绝、持久 race、推测写入限制、责任状态及信息流传播/出口检查。测试文件可直接定位每项断言。

`tests/crash_worker.py` 在独立 Python 进程中以 `os._exit(77)` 强制退出，覆盖预留后、许可消费后、操作插入后、接纳提交前后、派发标记提交后、结算提交前后。它检验进程级崩溃恢复，不模拟断电、磁盘控制器丢写或整库备份回退。

端到端测试采用独立 SQLite 数据库作为外部端点，检查真实模拟仓库状态和独立收费记录。远端修改成功但响应丢失后，重放保留 Unknown，不重复修改。客户端观察的旧版本在端点提交时被原子拒绝，拒绝仍可收费。

运行 `python -m al0.demo` 得到顺序修复示例：首次完成后再次重放，事件增加为 0、仓库只修改一次、总资源消费为 4 个实验单位。该单位不是美元或真实 token，代码模型和测试 checker 均为确定性的本地模拟器。

运行 `python -m examples.reconcile_sdk`，远端成功且回复丢失后经一次付费查询恢复：结果为 `Done`、状态为 `CompletedClean`，总确认费用为 5 单位，预留为 0，业务修改和查询各一次。再次恢复增加事件为 0。三个新增进程崩溃测试分别覆盖查询结果持久化后、业务证据应用后及费用结算后。

运行 `python -m examples.parallel_tasks`，两个同名节点子计划实际并行进入本地适配器，形成两个独立操作。任务均为 Completed/Joined，根预算确认费用为 2、余额为 8、预留为 0；恢复作用域后新增事件为 0。任务专项测试还覆盖七处进程退出、嵌套退出、取消与新派发竞态、等待回执后的恢复及迟到收费。

运行 `python -m examples.speculative_repair`，两个受检候选在推测作用域中运行，持久选择后由父计划接纳一次共享写入。示例总确认费用为 7、余额为 3，业务修改一次，race 选择记录一条；恢复保持相同 winner。新增进程测试覆盖选择事务提交前后，以及嵌套任务结果提交后的恢复。

运行 `python -m examples.task_outcomes`，父计划将模型候选作为显式输入传给检查任务。成功路径修改仓库一次并确认费用 4；检查拒绝路径不修改仓库、确认费用 3；回复丢失路径不修改仓库、保留 1 单位预留。三个场景恢复时都保持原分支且没有新增事件。

运行 `python -m examples.information_flow`，私密输入经过模型与检查器后仍保留接收方限制和模型 taint。向 owner 交付允许，公开导出与公开动作授权均被 AL7001 拒绝，公开适配器调用次数为 0。被拒绝运行确认费用为 2，预留为 0；安全运行重放标签一致且新增事件为 0。新增 33 项专项测试包括四处任务结果与结果观察事务前后的独立进程崩溃恢复。

运行 `python -m examples.frontend_demo`，先执行 `.al0` 编译结果，再改用 SDK 恢复，结果均为 Done，新增事件为 0，确认费用为 4，业务修改一次。前端专项 37 项测试覆盖全部现有 IR 操作、源码映射、静态拒绝、展开限制、受限目录和 CLI、三种任务结果路径及运行时标签拒绝；其中固定种子变异测试包含 150 个样本。这些证据确认当前核心的语义共用，不证明独立语法提升开发效率。

运行 `python -m examples.conditional_repair`，程序投影检查后补丁的版本字段、计算条件，再按持久选择决定是否提交。源码与 SDK 的 IR 相同，分支选择为 1 条，确认费用 4，业务修改一次，恢复新增事件为 0。新增 23 项专项测试还覆盖状态从 Unknown 变为 Applied 后选择与输入不变，以及分支提交前后独立进程退出的恢复差异。

运行 `python -m examples.bounded_batches`，四轮分别输出 0、1、3、6，确认费用为 4，未决预留为 0。首次执行 35 步，SDK 恢复只执行 3 步并跳过已完成循环体，新增事件为 0。新增 28 项专项测试覆盖函数纯度、循环状态类型、运行时次数上限、跨任务共享额度、未决回执与费用、标签、迁移和四处循环进程崩溃恢复。

运行 `python -m examples.resource_branches`，Permit 移入提交分支，返回的 Operation 再移入处理分支；结果 Done，外部发送一次，确认费用为 1，预留为 0，恢复新增事件为 0。源码与 SDK 的 IR 相同。新增 32 项专项测试覆盖 Intent 绑定、分支移动和责任合并、OpRef 只读借用、嵌套分支与 TaskOutcome、任务内执行、检查回执和标签，以及四处资源分支进程崩溃恢复。

运行 `python -m examples.admission_outcomes`，成功、预算拒绝、撤权拒绝分别返回 Done、Deferred、Denied；只有成功路径发送一次并确认费用 1，所有路径恢复新增事件均为 0。新增 33 项专项测试覆盖持久拒绝、新 slot 使用返还许可、期限复核、并发竞争、责任与标签、v10 迁移和六组独立进程崩溃恢复。`models/admission_audit.py` 的正确变体枚举 38 状态、51 转移，未违反所检查不变量；省略持久拒绝的变体产生三步最短反例，详见模型结果。

运行 `python -m examples.budget_workflows`，划分工作与储备预算，通过 Budget 参数和资金借用执行一次观察调用。确认费用 1、预留 0、根可用余额 9；两个调用帧、两次拆分和两次归还，恢复新增事件 0。新增 31 项专项测试覆盖预算隔离、移动与返回、工作流资源绑定、任务子分配租约、推测策略、挂起和迟到退款、v11 迁移，以及六处独立进程退出后的预算与调用恢复。

运行 `python -m examples.capability_calls`，把 send 限为 reports 频道及每操作最多一次尝试，再通过受限调用发送。确认费用 1、预留 0、发送一次，能力绑定和派生各一条，恢复新增事件 0。41 项专项测试覆盖父链撤销、epoch、期限与次数、字段约束、Cap/Permit/Operation 参数边界、观察工具、嵌套任务、标签、v12 迁移及六组独立进程退出。权限模型的正确变体枚举 60 状态、120 转移，两个削弱变体分别产生祖先撤销与显式参数越界反例。

运行 `python -m examples.dynamic_budgets`，纯函数计算 `{u:3}`，依次用于作用域、子任务和任务内 split。金额记录三条，确认费用 1、预留 0、根余额 9，外部调用一次，恢复新增事件 0。28 项专项测试覆盖运行时数量和多维校验、静态种类、数据标签、首次金额、并发、循环身份、业务状态变化、任务等待与迟到退款、v13 迁移，以及九组求值和资金事务之间的独立进程崩溃。

运行 `python -m examples.task_capabilities`，子任务只取得 send 权限，将其衰减后返回给父工作流发送。确认费用 1、预留 0，外部发送一次，恢复新增事件 0。31 项专项测试覆盖显式 Cap 输入与结果、持久任务权限表、较宽实参拒绝、嵌套任务、动态预算、TaskOutcome 与 race、标签、撤销、旧任务迁移及八组独立进程崩溃。

运行 `python -m models.combined_audit`，把祖先撤权、取消、两次尝试、一次协调器崩溃/恢复及独立业务/费用回执纳入同一有限模型；apply/reject 两种模式各有 548 状态、1,344 转移，所检查不变量无违规，三个错误变体均产生反例。`python -m models.combined_conformance` 的 194 条生成调度、1,271 步和 5,890 次重放/拒绝/重复检查通过真实 Store 与独立端点记录的比对。另有五个进程退出位置、两种端点模式的十个崩溃情形。它是指定覆盖分组下的有限对应检查，完整覆盖定义和未展开机制见 [组合验证规格](24-combined-protocol-validation.md)。

SDK 已通过固定 mypy 1.18.2 的公共构造接口静态验收：59 个方法具备注解及正例调用，扩展后的十一个源码文件严格检查通过，26 个基础计划与 9 个生成绑定计划被共享 IR 接受，40 + 14 个负例在预期行被拒绝。另有 7 个静态接受、共享检查器必须拒绝的边界用例。`CheckedPlan[T]` 保留分支/调用/循环/任务结果类型，`py.typed` 提供安装后的类型发现；从源码目录之外安装本地 wheel 的消费环境也通过运行、来源核验及正反例类型检查。动态 schema、权限身份和 affine 规则的边界见 [SDK 静态类型验收](25-sdk-static-typing.md)。

Catalog 生成器已接通固定 schema、工具名义标记、参数与字段来源重载，并在生成接口、混用原始 SDK 和 finish 时拒绝目录漂移。14 项专项测试覆盖目录版本/费用/scope/标签/schema/工具变更、特殊名字、CLI、来源保持及运行时值检查。生成版修复计划与手写 SDK 的 IR、执行轨迹、预算及端点状态一致，重放不新增事件。安装包消费验收也包含源码目录之外的生成、执行及类型正反例。具体保证和 TypedDict 结构类型边界见 [Catalog 类型绑定](26-catalog-type-bindings.md)。

## 里程碑审计

schema 17 的 [持久资源控制权接口](28-durable-resource-custody.md) 已在可信 Store 接通 Permit/Operation/Budget 原子移动、暂存发布、join 接收、generation 与 TaskLease 检查、当前持有者清理及显式重试出资。33 项专项测试含八个真实进程退出位置、join/取消竞态和嵌套任务。独立端点示例产生两次收费、一次业务效果，两个 attempt 使用不同账户，重放新增事件 0。该阶段提供可信宿主 API；后续受检接入见下段。

[受检任务资源](29-checked-task-resources.md) 已将多个命名 Permit/Operation/Budget 输入和单个 Operation/Budget 返回接入 IR、SDK、源码、TaskScope 与 TaskRunner。30 项专项测试覆盖嵌套接收、再次转移、标签、Admission 返还、Failed/Waiting 责任及八个进程退出位置。源码与 SDK 生成相同 IR，独立端点调用和收费各一次、确认费用 1，重放新增事件 0。后续资源包已支持具名多资源返回和未消费 Permit；资源 TaskOutcome/race 后续已接通；一般业务 Variant 后续已接通；固定次数的资源循环已在 [受检资源循环](38-checked-affine-loops.md) 接通；一般循环控制仍待完成。

[受检资源包](31-checked-resource-bundles.md) 已实现 pack/unpack/take、封闭字段依赖、嵌套包、包参数与正常任务返回。包内 Permit 必须绑定同包 Intent，资源叶子在一次事务中移动/发布/接收；字段标签及 Cap/动作权限检查保留。31 项专项测试含八个进程退出位置、SSA 身份碰撞、模板代次、重复别名和权限边界。源码/SDK 示例整包再次转交后使用两个动作，独立端点调用及收费各两次，重放新增事件 0。

跨任务仿射资源协议已形成 [设计、模型和接入清单](27-affine-task-transfer.md)。有限模型覆盖 1,629 个状态、5,174 条转移，九个错误变体均有反例，10 项模型测试通过。真实 Store 已新增 attempt 级固定出资记录，退款、queued 释放和分账户统计读取该记录；schema 16 在事务中导入旧出资事实。12 项存储测试包含五个真实进程退出情形。后续 schema 17 已接通可信 Store 的 custody 与资源返回暂存区；受检参数、单资源及资源包正常返回也已接通，但尚未建立该有限模型与完整运行时的对应证明。

| 阶段 | 当前权威证据 | 尚未完成 |
| --- | --- | --- |
| M0 语义冻结 | docs/01 至 07 与实现暴露的边界 | 将实现中的结果及异常接口进一步对齐完整规范 |
| M1 受检 IR | al0/ir.py、al0/execute.py、al0/pure.py、al0/limits.py、任务句柄与传递效应检查、纯函数、有界循环、TaskOutcome 与 Bool 分支、Permit/Operation 分支移动、OpRef 只读借用、Admission 与 ReturnedPermit 分支、Budget 参数/借用/返回、运行时金额与 resources/split/scope/spawn、Cap 参数/衰减/跨任务返回、任务仿射参数、单资源/嵌套包返回、资源 TaskOutcome 一次匹配与 race 结果义务、纯数据及资源 Variant/穷尽匹配、infer_result 终态值、infer_progress/resume_infer 固定等待与终态观察、try_infer 固定接纳与拒绝、可信提供方拒绝、派发阻止后的 Waiting、资源 repeat 与初值消费、pack/unpack/take、spawn_caps、带资源 invoke 与 invoke_caps、局部数据限额、共享步骤/协作期限、标签传播与出口检查 | 一般能力种类与动态约束、一般循环控制及单独能力状态、ContextView、解密与完整性提升协议、完整本地 CPU/内存隔离、动态计划生成 |
| M2 持久运行时 | al0/store.py、al0/admission.py、al0/allocation.py、al0/tasks.py、al0/flow_store.py、al0/control.py、al0/capabilities.py、预算 split/join、任务子分配继承及取消接管、传递撤销、持久任务权限表/金额选择/权限链/调用帧/接纳决定/输入/标签/结果观察/race/条件选择/循环游标、付费对账、attempt 级出资与退款归属、可信 Store 的 custody/原子移动/暂存返回/当前持有者清理、TaskRunner 固定资源输入重建与单资源及整包暂存返回、完成/取消原子结果与 race 整包接收/败者责任、原子推理授权/接纳及固定拒绝、派发事务内局部阻止分类、可信模型响应封套、独立推理终态记录、推理观察与控制交接原子提交、循环游标/代次原子提交与固定回执、旧版本到 v21 结构迁移、进程崩溃及并发测试 | 远程 AdmissionUnresolved 协议、独立监督与对账调度服务、通用迁移、完整运行时语义对应 |
| M3 Python SDK | al0/sdk.py、al0/codegen.py、al0/concurrency.py、共享检查器、单工作流 IR/轨迹差分、并发宿主和 SDK 对账示例、59 个公共构造方法的 mypy 严格验收、结果泛型传播、类型分发标记、固定 Catalog 的 schema/工具绑定生成与漂移拒绝 | 运行时变化的 schema 与通用动态接口类型精化、完整诊断、更多工作负载与故障下的强基线差分验收 |
| M4 AL-0 前端 | al0/source.py、al0/frontend.py、核心 parser/AST/源码映射/静态展开、共用资源与效应检查、fn/call/repeat/if、全部当前 IR 构造表面语法、源码与 SDK 等价及恢复测试 | 嵌套表达式、完整资源语法、运行时源码位置映射、完整草案正反例覆盖 |
| M5 组合验证 | Admission 与 Cap 有限模型；撤权/取消/两次尝试/崩溃恢复/迟到收费联合模型；指定分组和相邻事件对覆盖的生成调度；独立业务与费用记录；跨任务仿射控制权/返回/费用归属有限模型及九个错误变体；进程退出测试 | 多操作、任务租约、预算树、信息流与动态计划的进一步组合；完整实现语义对应及形式证明 |
| M6 因果实验 | docs/05 中的对照设计 | 真实工作负载、强基线、消融及语言收益测量 |

这些阶段没有因顺序示例通过而被标记为全部完成。完整目标仍包括独立前端、同运行时 SDK 对照和组合验证。

## 当前实现边界

宿主 Python、适配器和数据库均属于可信基。Principal 由宿主完成身份认证后传入；它不是一个认证协议。拥有 Python 解释器或数据库文件访问权的恶意插件可以绕过库，当前不声称对其形成沙箱。

v15 结构迁移不会猜测旧任务的权限表。已启动且没有持久权限表的旧任务不能自动恢复执行；未启动任务可由可信宿主绑定首次表，已完成普通数据结果及未决费用仍可按对应协议读取或处理。一般权限导入与运行迁移仍待完成，新受检任务则在创建时保存该表。

目前 checker 接口只登记谓词与版本，尚未实现 checker 版本撤销和跨环境依赖新鲜度。checker 不解除数据保密限制或完整性 taint。信息流已覆盖受检 IR、工具出口和持久任务，但尚未覆盖可信宿主之外的插件沙箱、通用日志视图、侧信道或完整非干扰证明。示例端点提供原子版本条件；其他端点必须独立满足契约。没有接入真实云模型、Git 服务或业务系统。

`try_admit` 已提供持久 Admission 及返回许可分支；原 admit 的 Operation/异常接口保留。未分类内部错误和提交不确定不转换成正常拒绝；远程 AdmissionUnresolved 协议仍待实现。旧 infer/infer_result 在未知模型调用时抛出携带 operation 的 `Suspended`；infer_progress/resume_infer 及 try_infer 的已接纳路径返回 Waiting(Operation)，动作 Unknown 则进入显式匹配。费用仍未结算但业务成功的情况保留在运行时，不应仅根据示例的字符串 Done 判断整个 run 已无责任。

重放先使用固定程序重建局部值，但通用 `audit replay`、resample、fork 和节点迁移尚未实现。当前实现语言使用 Python 以便快速建立可执行语义与故障测试；Rust 检查器和长期解释器的选型仍保留在设计路线中。

[资源型 TaskOutcome 与 race](32-resource-task-outcomes-and-race.md) 已接通成功包的一次匹配、完成/取消原子决定、胜者整包接收与败者责任接管，保留固定接收代次、标签与迟到费用。41 项专项测试通过，包含 12 个真实进程退出情形。混合包与嵌套 race 的源码/SDK 示例 IR 一致，重放不新增事件。

[受检业务 Variant](34-checked-business-variants.md) 已接通纯数据、证据和资源分支、穷尽匹配、调用/任务持久传递及标签和权限检查。43 项专项测试含 8 个进程退出位置；Accepted/Deferred/Denied 三条源码/SDK 路径与独立端点账单一致，重放无新增事件。

[受检推理结果](36-checked-inference-results.md) 已实现 infer_result 的 Success(Candidate)/Error 分类、独立终态结果记录、跨任务返回及标签传播。40 项专项测试含 13 项真实进程退出，确认输出错误不抹去费用，未知调用不自动重发。可继续执行的 Waiting(Operation) 已由下述新接口接通；接纳层已由 try_infer 接通，提供方拒绝已由 [可信响应封套](44-checked-inference-provider-outcomes.md) 接通。

[受检资源循环](38-checked-affine-loops.md) 已接通固定次数 repeat 的 Budget/Operation/Bundle/仿射 Variant 状态、初值消费、固定交接回执、游标与代次原子提交、嵌套任务和循环恢复。43 项专项测试含 13 项真实进程退出；取消前后两个时序、旧回执拒绝新操作和独立端点账单均符合预期。

[受检推理进度](40-checked-inference-progress.md) 已接通 infer_progress/resume_infer、模型 Operation/OpRef 参数、固定 Waiting/终态选择与代次原子转移。40 项专项测试覆盖 12 个进程退出位置、两处迁移中断、任务取消前后及并发事实观察。源码/SDK 示例只调用模型一次，经任务和循环推进到 Success，迟到账单结算 1，重放无新增事件。

[受检推理接纳](42-checked-inference-admission.md) 已实现 try_infer 的 Rejected/Started 分层结果、内部授权与接纳原子提交、拒绝回滚引用清理、首次拒绝固定和 schema 21 迁移。33 项专项测试及前端/生成绑定合计 84 项通过；另有 120 条真实 Store 调度、600 步与独立端点对照，覆盖已接纳调用的迟到结果、账单、撤权、取消和观察排列。该有界切片不等于完整语义对应。

[受检提供方结果](44-checked-inference-provider-outcomes.md) 已接通可信 InferenceResponse、Refused 分类及派发阻止后的 Waiting。新增 30 项测试，包含 14 个真实进程退出位置；117 项相关回归通过，兼容修复后的组合协议与提供方专项 41 项通过，最终全量 932 项通过。新 wheel 在源码目录外完成安装、运行与类型验证。另有 126 条真实 Store 调度、618 步与独立端点对照，覆盖迟到拒绝/费用及未派发队列的观察/取消/重放。源码与 SDK 的 Output、Refused、Blocked 三条路径 IR 一致，重放新增事件为 0。

[持久 ContextView 设计](45-context-views.md) 已配套一个独立有限模型：3,576 个状态、11,324 条转移，七个目标错误变体均有反例，且成功/失败/筛选/派发/历史读取路径均可达。其 token 计数为合成单位，尚未接入生产 Store，不能作为 ContextView 实现完成或真实提供方窗口保证。

## 下一组具体交付

1. 在 [受检提供方结果](44-checked-inference-provider-outcomes.md) 基础上推进 [持久 ContextView](45-context-views.md) 的策略、材料与快照内核，以及保护类型和模型输入契约；在 [受检资源循环](38-checked-affine-loops.md) 基础上扩展一般循环控制与能力范围。
2. 在已有标签和受检出口检查之上，补齐 ContextView、日志/发布协议和授权解密/完整性提升，再开放真实模型适配器。
3. 在已有 AL-0 核心前端上扩展嵌套表达式和资源控制语法，并补运行时源码位置映射；保持同运行时 SDK 对照。
4. 在 SDK 静态验收及固定 Catalog 生成绑定基础上继续精化通用动态接口，扩展强基线差分工作负载与组合故障验证。

本轮 SDK、对账协议、结束责任状态及局限详见 [SDK 与对账设计](09-sdk-and-reconciliation.md)。

分配树的账本表示、退款归属、迁移和测试见 [持久预算分配树](10-budget-allocation.md)。任务身份、工作线程代次、自动作用域退出及责任接管见 [持久任务作用域](11-durable-tasks.md)。闭合子计划的句柄检查、传递效应、race 与共享写入限制见 [任务 IR 与推测执行](12-task-ir-and-speculation.md)。

显式数据输入、证据转移、恢复时固定绑定及带效应的结果分支见 [任务输入与 TaskOutcome](13-task-inputs-and-outcomes.md)。当前不能把任务的 Completed 或运行的 CompletedClean 当作用户业务目标成功的通用证明。

标签代数、任务静态上界、分支与 race 控制依赖、出口规则、语义版本及保证边界见 [信息流标签与持久任务](14-information-flow.md)。

可执行源码语法、编译 API、CLI、独立 source map、稳定节点身份及 SDK 等价证据见 [源码前端与共享 IR](15-source-frontend.md)。

纯数据变换、检查证据失效、带效应 Bool 分支及首次选择/输入持久化的恢复规则见 [纯表达式与持久条件分支](16-pure-and-conditional.md)。

纯函数边界、运行时有界状态循环、每轮进度提交、跨任务共享步骤和协作期限见 [纯函数、有界循环与共享执行额度](17-functions-loops-and-fuel.md)。当前额度不等于完整进程内存或跨恢复的 CPU 总量限制。

Intent 绑定、Permit/Operation 分支移动、可复制 OpRef、责任合并与恢复见 [分支资源传递与操作观察借用](18-resource-branches.md)。普通 JSON 和宿主根输入不能据此构造保护类型。

接纳决定的原子提交、拒绝不重开、许可返还、新 slot 使用及迁移见 [持久 Admission 与许可返还](19-admission-outcomes.md)。

Budget 接口、invoke 调用帧、任务子分配的租约继承及取消责任见 [Budget 参数与持久工作流调用](20-budget-workflow-calls.md)。预算父子关闭顺序仍由账本动态检查。

Cap 参数、衰减规则、授权父链、受限调用中的保护实参检查及历史恢复见 [Cap 权限衰减与受限调用](21-capabilities-and-attenuation.md)。尝试上限作用于每个逻辑操作，跨操作的预算仍由账本管理。

纯资源向量构造、运行时拆分及任务金额、首次选择和资金转移的区分见 [运行时预算向量与持久金额选择](22-dynamic-budget-vectors.md)。普通向量可以复制，但它不携带预算所有权。

Cap 的子任务输入与结果、spawn_caps、创建事务中的权限绑定以及旧任务恢复边界见 [跨任务 Cap 传递与持久权限表](23-task-capability-transfer.md)。后续 custody、受检资源包及 TaskOutcome/race 已接通 Permit、Operation 和 Budget 的跨任务控制权协议；完整模型对应仍待验证。

组合模型、反例、生成调度的明确覆盖分组、独立端点记录和进程退出测试见 [组合协议模型与运行时一致性检查](24-combined-protocol-validation.md)。真实 Store 的选定调度检查不等于穷举全部实现执行。

SDK 的结果类型传播、59 个公共构造方法静态验收、40 个基础负例及共享检查器职责分界见 [SDK 构造接口静态类型验收](25-sdk-static-typing.md)。构造 API 的验收不等于整个运行时内核已通过严格 Python 类型检查。

后续工作以这份状态表和实际测试为依据推进，不把待实现要求从设计目标中删除。
