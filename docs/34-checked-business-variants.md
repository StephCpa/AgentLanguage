# 封闭业务 Variant：数据、证据与资源分支

状态：已实现共享 IR、Python SDK、源码前端、持久值、工作流调用、任务输入/返回、TaskOutcome 与 race。数据库结构保持 schema 17。设计依据为 [一般业务资源结果](33-affine-business-results.md)，执行状态接口见 [资源型 TaskOutcome](32-resource-task-outcomes-and-race.md)。

## 类型与构造

Variant 显式声明全部标签及各自载荷类型。接口由规范化标签和完整载荷类型决定，不包含局部 SSA 名字。支持普通数据、Candidate、Observation、Checked、Cap、Operation、Budget、Bundle 及嵌套 Variant。裸 Permit、Intent、OpRef、Scope、Task、Unpacked 不作为独立载荷；返还 Permit 时需用已有 Bundle 同时封闭其 Intent 依赖。

```text
Variant<{
    Accepted: Bundle<{funds: Budget, operation: Operation<"send">}>,
    Deferred: Bundle<{funds: Budget, permission: Permit<request>, request: Intent<"send">}>,
    Denied: Bundle<{funds: Budget, reason: Text}>
}>
```

源码 `variant("Accepted", packet)` 要求完整的 Variant 类型注解；注解提供其他未选中分支的类型，并不能转换载荷。`match_variant(value, {Accepted: accepted, Deferred: deferred, Denied: denied}, captures)` 穷尽列出各分支，每个分支必须显式接收自己的 `result` 参数。

SDK 使用 `variant_type({...})` 与显式 Type 描述接口，无需执行示例计划。`PlanBuilder.variant_parameter`、`variant`、`match_variant` 提供参数、构造与匹配；Bundle 参数也可直接使用封闭 Type 接口。`Ref[Variant]` 保留类别，匹配的公共结果类型通过泛型传播；标签对应的具体载荷、证据与资源规则由共享检查器验证。

## 仿射、纯度与受控入口

若任一标签的类型含仿射资源，整个 Variant 就必须移动、匹配或返回一次。即使本次选中 Empty/Text 等不含资源的分支，也不能重复使用或忽略整个资源结果。构造只消费实际仿射载荷；它不会消费可复用的普通数据。

纯数据 Variant 可复用，可进入纯函数和现有数据循环状态。纯函数同时检查全部分支类型；不能把未选中 Cap 分支藏进一个看似只返回 Text 的纯构造。固定次数资源循环后续已在 [受检资源循环](38-checked-affine-loops.md) 接通。

Cap 是可复制的权限值，但含 Cap 的 Variant 仍受入口限制。原始根输入不能把它当作普通 JSON 导入；经受检构造、调用或任务参数传递时，再递归核对实际 Cap/Permit/Operation 与接收方权限表。资源型 Variant 的 TaskOutcome Completed 分支必须接收结果，包括实际资源清单为空的情形。

资源型匹配消费容器，再引入选中载荷的处理义务。禁止同时匹配容器并将同一容器捕获给分支。所有捕获的仿射值在各分支显式接收；分支须返回相同类型。若需要不同形状的返回值，应显式构造同一个 Variant 接口，而不是隐式合并任意结构。

## 记录、标签与恢复

Value 使用独立 `variant` 元数据保存封闭接口，data 保存 `{tag, payload}`，其中 payload 是完整值记录。普通 JSON 中恰好存在 tag/payload 不会获得 Variant 类型。检查器、Broker 和记录解码分别核对接口、实际标签、载荷种类、schema、证据和资源身份。

资源枚举只遍历实际选中载荷。路径中的 Variant 段使用带类别标记的对象，与 Bundle 的字符串字段段区分；相同 kind/id 在整个嵌套结构中重复出现会被拒绝。现有 Bundle 路径及已合法计划的 IR 身份保持原样。

Variant 自身没有 custody 行。任务输入保存完整模板与实际资源移动清单；发布和接收复用既有事务。恢复依据固定清单重建实际资源引用，不查询当前 owner 来刷新代次。无资源的活动分支仍保存 Variant 元数据。隐式 Scope 退出依据声明接口拒绝丢弃资源型 Variant，也包括包内含有空资源 Variant 的 Bundle。

构造合并控制上下文与载荷标签；参数规范化递归把嵌套载荷标签提升到外层记录。匹配先合并整个值与载荷标签，再执行分支，因此忽略私密载荷、返回常量也不会绕过已有控制标签。Checked 回执在载荷中保留，并在工作流或任务边界重新校验。

有副作用的匹配复用 `branch_choices`，固定首次标签、完整条件记录和捕获输入；重放读取原快照。纯函数匹配直接求值，不新增分支持久事件。发布、接收与匹配快照仍是不同阶段，均有各自的事务恢复边界。

## 业务分支与执行状态

示例使用 `TaskOutcome<Variant<Accepted, Deferred, Denied>>`：外层描述执行是否完成，内层描述正常返回的业务选择。

- Accepted 返回 Operation 和 Budget，父方完成已有行动并归还预算。
- Deferred 返回未消费 Permit、其原 Intent 与 Budget，父方通过另一受检调用使用返还资金重新接纳。
- Denied 返回说明和 Budget，不伪造许可或操作引用。

异常、Waiting 和取消仍走 Runtime 的责任接管路径，不自动构造任一业务分支。业务标签本身不证明外部系统没有生效，也不消除已暴露请求的费用。

## 执行与证据

```powershell
python -m examples.business_variants
python -m unittest tests.test_variants -q
```

[源码](../examples/business_variants.al0) 与 [SDK](../examples/business_variants.py) 的两个入口产生相同 IR。通过任务出资和撤权时点分别执行 Accepted、Deferred、Denied。独立 SQLite 端点只记录 Accepted 和 Deferred 各一次调用与收费；两者确认费用 1、可用预算 9，Denied 确认费用 0、可用预算 10。三条路径恢复均不增加事件，见 [执行报告](../models/business_variants_example.json)。

43 项专项测试通过，用时 22.746 秒。覆盖纯数据复用、纯调用和数据循环、空资源义务、重复匹配与别名、封闭类型和完整匹配、裸许可拒绝、Cap 入口与权限边界、真实检查回执、私密嵌套载荷、模板代次、路径区分、嵌套任务、空资源 race 和 TaskOutcome。

其中 8 项真实进程退出测试覆盖发布、接收、匹配快照事务中和提交后，以及 Deferred 许可返还后再次接纳。恢复后每条行动路径只产生一次调用和一次确认收费，重放不增加事件。

本阶段 SDK 有 55 个公共构造方法，20 个基础计划与 5 个生成绑定计划，31 + 12 个静态负例。完整回归 746 项全部通过，用时 460.529 秒，见 [完整日志](../models/variants_full_regression.log)。从源码目录之外安装 wheel 后，纯数据和资源 Variant 消费程序均通过实际执行及类型检查，见 [安装包报告](../models/sdk_wheel_audit.json)。

## 尚未完成

Variant 提供了 `Result<Candidate<T>, InferenceError>` 所需的封闭类型机制，后续 [受检推理结果](36-checked-inference-results.md) 已接入终态 infer_result；未决与接纳层结果仍按 [推理结果协议设计](35-inference-result-protocol.md) 继续推进。一般循环控制、ContextView、授权解密/完整性提升、受检动态计划、完整模型对应，以及真实工作负载与同运行时 SDK 对照仍在总体目标内。
