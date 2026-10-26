# AL-0 研究与设计

AL-0 是面向 Agent 的小型受检语言提案。它将模型候选、检查证据、权限和持久操作区分开，并让取消、重试与恢复遵循同一套资源规则。

研究与实现更新日期为 2026 年 10 月 26 日。已有设计规格、状态模型、顺序与任务 IR、Python SDK、核心源码前端、参考解释器、SQLite 持久执行核心、付费对账、受限推测执行及信息流出口检查。完整语言语法、ContextView、一般循环控制、信息流解密与完整性提升协议及研究实验尚未完成，详见 [实现状态与后续验收](docs/08-implementation-status.md)。

研究概览也保存在 [AL-0 深入研究与核心设计](https://chatgpt.com/space/page_5c800cfda8b481919bfb86e7db2bf079)。页面概览已回读核对；当前应用返回打开请求已排队，尚未完成页面视觉排版检查。本地文档为完整技术规格。

## 阅读顺序

1. [研究结论与设计决策](docs/01-research-design.md)：新增直接近邻、研究命题、取舍和路线。
2. [核心语言规范](docs/02-language-core.md)：类型、效应、动态计划、并发及诊断。
3. [持久运行时协议](docs/03-runtime-protocol.md)：动作身份、原子接纳、撤权、计费及崩溃恢复。
4. [形式语义与证明计划](docs/04-semantics.md)：状态、转移、目标定理及实现对应义务。
5. [实验与实施计划](docs/05-evaluation.md)：同运行时对照、故障矩阵、指标及阶段验收。
6. [模型结果与复现](docs/06-model-results.md)：原始模型复现和五组新增语义实验。
7. [来源与证据登记](docs/07-sources.md)：原始材料、公开论文、官方文档及适用范围。
8. [实现状态与后续验收](docs/08-implementation-status.md)：已实现行为、测试证据和完整目标中的剩余要求。
9. [SDK 与对账设计](docs/09-sdk-and-reconciliation.md)：共享 IR、查询计费、崩溃恢复及运行结束责任。
10. [持久预算分配树](docs/10-budget-allocation.md)：split/join、资金归属、迟到退款及结构化任务的后续要求。
11. [持久任务作用域](docs/11-durable-tasks.md)：并行执行、恢复代次、退出竞态与责任接管。
12. [任务 IR 与推测执行](docs/12-task-ir-and-speculation.md)：句柄消费、嵌套效应、持久 race 和写入边界。
13. [任务输入与 TaskOutcome](docs/13-task-inputs-and-outcomes.md)：显式数据/证据传递、固定输入和带效应的结果分支。
14. [信息流标签与持久任务](docs/14-information-flow.md)：保密限制、完整性标记、控制依赖、任务上界和出口检查。
15. [源码前端与共享 IR](docs/15-source-frontend.md)：可执行 `.al0` 语法、源码诊断、静态展开、CLI 及 SDK 等价验证。
16. [纯表达式与持久条件分支](docs/16-pure-and-conditional.md)：数据计算、证据失效、布尔分支、首次选择及输入快照的恢复规则。
17. [纯函数、有界循环与共享执行额度](docs/17-functions-loops-and-fuel.md)：纯度检查、运行时迭代、状态检查点、共享步数及协作期限。
18. [分支资源传递与操作观察借用](docs/18-resource-branches.md)：Intent 绑定、Permit/Operation 移动、可复制 OpRef、责任合并与恢复。
19. [持久 Admission 与许可返还](docs/19-admission-outcomes.md)：固定接纳决定、预算拒绝、返还许可、新 slot 重试和事务恢复。
20. [Budget 参数与持久工作流调用](docs/20-budget-workflow-calls.md)：预算移动、资金借用、调用帧、任务子分配和退出责任。
21. [Cap 权限衰减与受限调用](docs/21-capabilities-and-attenuation.md)：授权父链、传递撤销、显式资源边界及历史恢复。
22. [运行时预算向量与持久金额选择](docs/22-dynamic-budget-vectors.md)：计算额度、动态任务资金、首次选择、标签与恢复。
23. [跨任务 Cap 传递与持久权限表](docs/23-task-capability-transfer.md)：显式能力输入/返回、受限任务、固定权限及迁移边界。
24. [组合协议与运行时一致性检查](docs/24-combined-protocol-validation.md)：撤权、取消、两次尝试、恢复和迟到费用的联合模型、生成调度及独立端点记录。
25. [SDK 构造接口静态类型验收](docs/25-sdk-static-typing.md)：结果类型传播、mypy 正反例、共享检查职责和安装包类型发现。
26. [Catalog 驱动的 Python 类型绑定](docs/26-catalog-type-bindings.md)：schema/工具接口生成、工具身份、字段来源、目录漂移拒绝及差分验收。
27. [跨任务仿射资源协议](docs/27-affine-task-transfer.md)：控制权与出资分离、原子资源包、持久返回、取消反例，以及已实现的 schema 16 attempt 出资记录。
28. [持久资源控制权与任务返回](docs/28-durable-resource-custody.md)：schema 17 的可信 Store 接口、资源代次、任务 lease、暂存接收、取消、独立端点及进程退出验收。
29. [受检任务资源](docs/29-checked-task-resources.md)：源码/SDK/IR 的仿射任务参数、Operation/Budget 返回、持久重建与标签、嵌套任务及进程退出验收。
30. [具名资源结果包设计](docs/30-resource-result-bundles.md)：整体接口设计；命名包和任务结果接收已接通，一般业务资源分支仍待实现。
31. [受检资源包实现](docs/31-checked-resource-bundles.md)：混合/嵌套任务参数与结果、Permit 字段依赖、pack/unpack/take、完整接收、权限与恢复验收。

32. [资源型 TaskOutcome 与 race](docs/32-resource-task-outcomes-and-race.md)：成功包一次接收、完成与取消的原子决定、race 胜者及败者责任、固定回执与恢复验收。
33. [一般业务资源结果设计](docs/33-affine-business-results.md)：封闭 Variant、不同业务分支的资源包、空资源分支、持久枚举与验收路径。
34. [受检业务 Variant](docs/34-checked-business-variants.md)：数据/证据/资源分支、穷尽匹配、纯函数、跨任务接收及业务分支恢复验收。
35. [显式推理结果协议设计](docs/35-inference-result-protocol.md)：候选值与错误分类、未决调用责任、固定结果与恢复顺序；终态与未决接口已实现，接纳层已接通 try_infer，提供方拒绝结果已接入。
36. [受检推理结果](docs/36-checked-inference-results.md)：infer_result、持久终态结果、费用与标签、跨任务返回及进程退出验收。
37. [仿射循环状态设计](docs/37-affine-loop-state.md)：循环游标与资源代次的原子提交、固定回执、取消及嵌套恢复。
38. [受检资源循环](docs/38-checked-affine-loops.md)：Budget/Operation/Bundle/Variant 跨轮交接、固定代次、原子游标与恢复验收。
39. [未决推理与持久观察设计](docs/39-inference-progress-and-observation.md)：Waiting 操作控制权、按观察身份固定结果、接纳与取消边界；核心实现见第 40 项。
40. [受检推理进度](docs/40-checked-inference-progress.md)：模型 Operation、固定 Waiting/终态观察、任务/循环转移、取消与迟到账单。
41. [推理接纳前结果设计](docs/41-inference-preflight-results.md)：首次拒绝固定、原子授权与接纳、已接纳责任及提供方拒绝分层。
42. [受检推理接纳](docs/42-checked-inference-admission.md)：try_infer、首次拒绝固定、原子授权/接纳、回滚清理及组合调度验收。
43. [提供方拒绝与派发阻止设计](docs/43-inference-provider-outcomes.md)：已接纳责任、受检派发阻止、可信响应封套及错误分类。
44. [受检提供方结果](docs/44-checked-inference-provider-outcomes.md)：可信 Refused、队列阻止后的 Waiting、取消退款、迟到费用与组合调度。
45. [持久 ContextView 设计](docs/45-context-views.md)：来源与角色、必选约束、容量与筛选、固定快照、标签依赖及受检接入。
46. [固定提交审计修复](docs/46-audit-remediation.md)：七项问题、跨模块组合回归、独立语义版本、旧记录边界及 Linux 安装验收。
47. [真实 Git 发布适配器](docs/47-real-git-adapter.md)：宿主批准的提交、原子操作引用、未知结果对账、真实 Git 故障测试及 GitHub 在线验收。
48. [Git 任务配对评测器](docs/48-paired-git-evaluation.md)：强 SDK 检查、同任务故障调度、独立外部状态评分及参考答案校准。
49. [隔离计划构造](docs/49-isolated-plan-construction.md)：Docker 构造边界、独立类型检查、宿主 IR 复核及限制清理验收。
50. [开发任务契约](docs/50-development-task-contracts.md)：条件发布、顺序发布、接纳回退、任务级评分及参与者材料边界。
51. [生成与修复实验记录](docs/51-generation-repair-journal.md)：固定材料、逐轮回执与评分、未知费用和迟到结算。
52. [DeepSeek 开发预实验](docs/52-deepseek-development-pilot.md)：真实 API 调用、1 美元总限额、冻结材料与容器评分。

## 运行参考实现

在项目目录使用 Python 3.10 或更新版本，无第三方运行依赖：

```powershell
python -m unittest discover -v
python -m al0.demo
python -m examples.reconcile_sdk
python -m examples.allocated_recovery
python -m examples.parallel_tasks
python -m examples.speculative_repair
python -m examples.task_outcomes
python -m examples.information_flow
python -m al0.frontend examples/repair.al0 --demo-catalog
python -m examples.frontend_demo
python -m examples.conditional_repair
python -m examples.bounded_batches
python -m examples.resource_branches
python -m examples.admission_outcomes
python -m examples.budget_workflows
python -m examples.capability_calls
python -m examples.dynamic_budgets
python -m examples.task_capabilities
python -m examples.affine_task_resources
python -m examples.typed_task_resources
python -m examples.resource_bundles
python -m examples.resource_task_outcomes
python -m examples.business_variants
python -m examples.inference_results
python -m examples.affine_loops
python -m examples.inference_progress
python -m examples.inference_admission
python -m examples.inference_provider
```

示例使用确定性的本地模型、checker 和独立模拟端点数据库，不访问网络或真实仓库。它执行 [repair.ir.json](examples/repair.ir.json)，然后重放，验证不会新增操作和费用。

核心代码位于 [IR 检查器](al0/ir.py)、[Python SDK](al0/sdk.py)、[参考解释器](al0/execute.py)和[持久执行核心](al0/store.py)。[SDK 修复示例](examples/repair_sdk.py)生成与手写版本一致的 IR；[对账示例](examples/reconcile_sdk.py)演示回复丢失后的付费查询与恢复。宿主代码及适配器属于可信基，当前原型不提供恶意 Python 插件沙箱。

[推理进度示例](examples/inference_progress.al0)把未决模型请求作为 Waiting(Operation) 传入任务，在循环中显式观察至 Success。相同观察位置重放原结果；迟到结果由下一位置观察，迟到账单继续按原调用结算。

SDK 构造接口另有固定 mypy 版本的静态验收：安装 `.[typing]` 后运行 `python -m scripts.check_sdk_types`。59 个公共方法具备注解及正例调用，26 个基础计划和 9 个生成绑定计划通过共享 IR，40 + 14 个负例在预期行被拒绝；资源单次使用和精确 intent 身份等仍由共享检查器验证。可用 `python -m al0.codegen --demo-catalog --output examples/generated_repair_bindings.py --check` 检查生成接口是否过期。具体边界及安装包验收见 [SDK 静态类型文档](docs/25-sdk-static-typing.md) 与 [Catalog 类型绑定](docs/26-catalog-type-bindings.md)。

[预算分配示例](examples/allocated_recovery.py)将工作额度与对账储备划入同一根预算下的不同分配，验证工作流程结束后仍保留未决费用，恢复时不重复释放额度。

[并发任务示例](examples/parallel_tasks.py)在同一运行时中执行两个使用相同节点名的受检子计划，展示独立任务身份、持久结果及恢复时不重复调用。

[信息流示例](examples/information_flow.py)让模型和检查器处理带标签的合成私密输入。检查成功后仍拒绝公开发布，允许向 owner 交付，重放保留标签且没有新增事件。

[权限示例](examples/capability_calls.al0)取得 send 能力，限制目标频道和尝试次数后调用子工作流。受限调用同时检查环境权限表和显式 Cap、Permit、Operation 参数；父授权撤销后，派生权限不能发起新工作。

[源码修复程序](examples/repair.al0)生成与手写 IR、Python SDK 相同的计划。[前端示例](examples/frontend_demo.py)先执行源码编译结果，再从 SDK 恢复，验证业务修改一次且没有新增事件。任务与分支的源码见 [task_outcomes.al0](examples/task_outcomes.al0)。

## 运行设计模型

在项目目录中运行 Python 3.10 或更新版本，无需安装第三方依赖：

```powershell
python models/original_audit.py
python models/extended_audit.py
python models/admission_audit.py
python models/capability_audit.py
python -m models.combined_audit
python -m models.combined_conformance
python -m models.inference_admission_conformance
python -m models.inference_provider_conformance
python -m models.context_view_audit
```

模型脚本生成相邻的 JSON 结果文件，包含可达状态计数及最短反例。原始模型提取自用户提供的报告链接；新增模型由本项目设计编写。组合模型分别枚举业务成功和拒绝两种端点配置，生成调度检查真实 Store，并保存独立端点与运行时证据。各模型仍是有界语义切片，不构成完整协议或语言的证明。

## 当前建议

下一实现阶段按 [ContextView 设计](docs/45-context-views.md) 接通可信策略、材料来源、持久快照及模型输入契约。先支持 Fail 与完整材料 Filter，再接入显式摘要工作流和真实计数器；有限模型的反例作为实现对照用例。

继续完善共用受检 IR 的源码前端、单协调器运行时和 Python SDK，并开展组合故障验证及同运行时对照实验。保留 TypeScript API 设计，延后第二套 SDK 实现。独立语言是否扩大投入，由它相对强类型 SDK 的开发与维护收益决定。
