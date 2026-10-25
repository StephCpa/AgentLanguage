# 显式推理结果：分类、责任与恢复设计

状态：本文保留整体协议设计。终态 infer_result 已在 [受检推理结果](36-checked-inference-results.md) 接通 IR、SDK、源码与独立结果表；可继续执行的 Waiting(Operation) 已由 [受检推理进度](40-checked-inference-progress.md) 接通，接纳层已由 [try_infer](42-checked-inference-admission.md) 实现，提供方拒绝已由 [可信响应封套](44-checked-inference-provider-outcomes.md) 实现。原 infer 仍返回 Candidate，未决调用抛出 Suspended，输出 schema 不合法抛出 InvalidData，可信提供方拒绝抛出 InferenceRefused。

## 当前执行路径

`Broker.admit_call` 完成授权与接纳，`Broker.drive` 派发调用并先保存外部业务事实、再记录可得的实际费用。`Broker.output` 最后检查输出 schema。这个顺序保证输出无法解码时，已经发生的调用及费用仍留在账本中。

当前 `tests/test_ir.py` 已覆盖两项必要性质：模型输出结构错误仍确认实际费用；响应丢失后进入 Unknown，再次执行不会自动重发。新结果接口必须保留这两项性质。

## 分开表示结果与责任

推理返回值回答“是否获得可用的候选值”；Operation 与费用账本回答“外部请求及费用是否已经确定”。两者不能合并为一个成功或失败标志。

第一阶段建议新增 `infer_result`，保留原 `infer` 的行为与既有程序指纹。返回类型使用已实现的封闭 Variant：

```text
Variant<{
    Success: Candidate<T>,
    Error: InferenceError
}>
```

`InferenceError` 是受大小限制的普通数据 schema。第一阶段只包含稳定错误类别、工具契约版本及公开诊断代码，不复制原始提示词、模型输出、宿主异常栈或提供方响应。错误数据沿用输入、操作和当前控制流的标签；它不会自动解除保密限制。

候选值仍需通过对应 checker 才能获得 Checked。Error 数据不授予重试、接纳、退款、权限或资源控制权。

| 当前事实 | 拟议结果 | 保留的责任 |
| --- | --- | --- |
| Applied，输出符合 schema | Success(Candidate) | 费用未结算时继续由账本跟踪 |
| Applied，输出不符合 schema | Error(InvalidOutput) | 外部调用事实和已确认/待确认费用均保留 |
| ProvenNoEffect，且当前契约确认不能产生候选值 | Error(NoResult) | 不由该标签推导费用为零 |
| NotDispatched、Pending 或 Unknown | 暂停当前求值 | 保留 Operation、预留和恢复位置，不生成终态 Error |
| 提供方表示内容拒绝 | 第一阶段不单独分类 | 需要契约明确的结构化拒绝事实后再扩展 |
| 接纳前权限或预算不满足 | 沿用现有接纳异常 | 后续统一 Admission 接口，不混为模型返回错误 |
| 适配器违约、账本冲突、重放不一致 | 保留运行时错误 | 不以捕获所有异常的方式生成正常业务分支 |

这个阶段只把确定的推理返回错误变成可匹配值，未完成原始设计的全部错误代数。原规范中的完整 Result 接口仍须补上接纳层结果、可恢复未决状态和提供方契约。

## 未决状态的后续接口

如需允许业务代码在 Unknown 时继续执行，应另行提供包含 Operation 的仿射结果，而不是把操作 ID 填进普通 Error JSON。可考虑 `Waiting: Operation<ModelTool>` 与终态分支并列，再通过 inspect、显式对账或责任移交处理。整个 Variant 因存在 Operation 分支而具有仿射义务，即使某次选中 Success 也必须遵循同一接口的消费规则。

该扩展需要明确现有自动 `infer` 所持操作的控制权如何交给调用方，何时注册消费义务，以及任务取消时由谁接管。第一阶段不隐式引入这项转移，也不自动重试。需要新调用时，由显式的新接纳位置创建新的逻辑操作并单独出资。

## 固定结果与恢复顺序

实现时需把“记录外部事实”“结算费用”“固定语言层结果”作为可分别恢复的步骤：

1. 复用同一逻辑调用位置，恢复原 Operation；不因 Error 或重放创建新 slot。
2. 读取持久业务事实，使用已绑定的输出契约分类；只将输出验证阶段的 InvalidData 转为 InvalidOutput，输入验证及持久数据损坏不属于此分类。
3. 固定标签、候选值或稳定错误码，以及输出契约身份。首次结果选择后的恢复不得因瞬时状态或新适配器行为换分支。
4. 后到费用可以继续结算，但不修改已经固定的语言层结果。任务完成与费用全部结束继续分开判断。

实现已采用独立 inference_results 表，并固定完整语言层结果及契约身份。选择依据是原子性与恢复身份：不能仅依靠已有的 match 分支快照，因为调用结果可能先被返回、传给子任务或打包，并没有在本地立即匹配。

## 下一轮验收

- Success 保留 Candidate 类型，不能直接作为 Checked 使用；源码、SDK 与 IR 生成同一封闭结果类型。
- InvalidOutput 仍保留真实调用和确认费用；无实际账单时保留未决费用；NoResult 不触发自动退款。
- 超时、断连、Pending 在重放中不新增派发；不能被通用异常捕获转换成确定失败。
- 在业务记录后、费用记录后、结果固定前后、任务发布和接收前后退出进程，恢复结果与独立端点记录一致。
- 私密输入产生的成功值、错误标签和错误码均传播控制标签；错误分支不能向公开出口泄漏模型响应。
- 原 infer 示例、旧 IR 指纹及恢复行为保持兼容；新 API 纳入 SDK 类型正反例与安装后消费验证。

本设计依赖 [受检业务 Variant](34-checked-business-variants.md)，并沿用 [持久 Admission](19-admission-outcomes.md)、[资源 TaskOutcome](32-resource-task-outcomes-and-race.md) 的责任分层。终态接口的实际验收见受检推理结果文档；未决与接纳层协议仍不计入已实现范围。
