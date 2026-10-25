# 显式终态推理结果：实现与恢复

状态：`infer_result` 已接入共享 IR、Python SDK、固定 Catalog 绑定生成器和 AL-0 源码前端。它将确定的推理输出错误表示为可匹配值，原 `infer` 保持既有行为。运行时新增独立结果表，数据库版本为 schema 18。整体设计及后续范围见 [推理结果协议](35-inference-result-protocol.md)。

## 语言接口

```text
infer_result("model", prompt)
    -> Variant<{Error: InferenceError, Success: Candidate<T>}>
```

操作沿用 `infer:model` 效应、工具输入类型和宿主权限。它不能出现在纯函数中；模型返回类型由工具目录决定。Success 载荷仍是 Candidate，不能声明成 Checked 或绕过行动所需的检查证据。

InferenceError 是新增的内建数据类型，字段为 `code: Text`、`tool: Text`、`version: Text`。该名字不再允许注册为自定义 schema。运行时当前产生的错误码是 InvalidOutput 和 NoResult；错误对象不包含原始提示词、原始输出、异常栈或操作控制权。它可以像其他普通数据一样被复制和显式构造，因此单凭一个 Error 数据值不能证明某次真实调用失败。

SDK 的 `PlanBuilder.infer_result` 返回 `Ref[Variant]`。固定 Catalog 生成器提供对应的 `infer_result_<tool>` 方法及类型化输入。Success 的具体 Candidate 类型、Error 载荷与分支完备性由共享 IR 检查；Python 的 Ref[Variant] 本身没有编码每个标签的载荷类型。

## 状态与费用

| 持久业务状态 | 语言结果 | 费用处理 |
| --- | --- | --- |
| Applied 且输出满足 schema | Success(Candidate) | 已确认费用保留；缺失账单仍待结算 |
| Applied 且输出不满足 schema | Error(InvalidOutput) | 不回退已发生的调用，也不退回预留 |
| ProvenNoEffect | Error(NoResult) | 无候选值不代表无调用费用 |
| NotDispatched、Pending、Unknown | Suspended | 不生成终态 Error，不自动重试 |

只有输出 schema 验证产生的 InvalidData 被转换为 InvalidOutput。根输入错误、授权或预算不足、适配器未返回 Response、内部异常、损坏的持久记录和契约冲突仍进入各自的错误路径。提供方内容拒绝尚未定义独立的结构化证据与错误分类。

业务调用事实先持久化，再记录可得的账单，最后固定语言结果。如果进程在收到响应后、保存业务事实前退出，恢复仍观察到 Unknown。此时须由可信对账入口提供外部证据；不会因再次执行相同节点而重新派发。

## 独立持久结果

`inference_results` 以 Operation 为主键，保存协议版本、Catalog 摘要、工具版本、输出类型和完整 Value 记录。首次提交固定标签、载荷及信息流标签。结果不依赖后续是否执行 match_variant，因此直接返回、跨调用传递或交给任务的结果也有独立恢复身份。

读取已提交结果时核对契约身份，不重新运行适配器或重新选择错误分支。首次写入要求业务状态已经终结，并验证当前任务 lease、run 和 activation。旧工作线程不能据此发布新结果。账单后到可以更新费用状态，但不改变已固定的 Success/Error。

错误标签及公开错误码也受信息流规则约束。结果与载荷均合并输入、操作输出策略和控制标签；匹配 Error 后产生的常量仍受分支控制标签限制。错误文本精简不等于该分支可以公开。

schema 17 到 18 的迁移在事务中创建结果表，同时保持已有操作、费用和任务记录。迁移不凭空补造旧调用的语言结果；若新的受检调用恢复到已有终态事实，再按绑定契约生成结果。旧 `infer` 不写入新结果表。

## 可复现示例与验证

```powershell
python -m examples.inference_results
python -m unittest tests.test_inference_results -q
```

[源码示例](../examples/inference_results.al0) 和 [SDK 示例](../examples/inference_results.py) 生成相同 IR。子任务返回 Variant，父任务匹配后，成功路径先通过 checker 再执行行动；两个错误路径返回错误码。

- Success：模型、checker、行动各调用一次，总确认费用 3。
- InvalidOutput：仅模型调用一次，总确认费用 1。
- NoResult：仅模型调用一次，总确认费用 1。
- 三条路径重放均新增 0 个事件；调用与费用另由独立 SQLite 端点记录核对。

实际输出见 [独立端点报告](../models/inference_results_example.json)。该阶段 SDK 验收覆盖 56 个公共方法、21 个基础计划、6 个生成绑定计划与 33 + 12 个静态负例。从源码目录之外安装 wheel 后，新的推理结果消费程序通过运行与类型检查，见 [类型报告](../models/sdk_type_audit.json) 和 [安装包报告](../models/sdk_wheel_audit.json)。

40 项专项测试通过，用时 28.133 秒。覆盖 Candidate 保留、错误载荷、迟到计费、未知状态恢复、权限和预算错误边界、源码/SDK 等价、标签传播、契约身份、跨任务直接返回及原 infer 行为。13 项使用真实进程退出，覆盖业务记录前后、计费后、结果提交前后、任务发布与接收前后，以及 schema 17 迁移提交前后。完整回归 786 项全部通过，用时 507.492 秒，见 [回归日志](../models/inference_results_full_regression.log)；当前整体范围见 [实现状态](08-implementation-status.md)。

另行验证了取消时序：在 `after_call_business_record` 故障钩子中先取消任务，再让原 worker 处理账单及发布结果。观察到 Cancelled、结果表新增 0 条、原 worker 发布被拒绝、外部调用 1 次、确认费用 1。该次针对性实验与上述单元测试数分开记录，见 [取消检查报告](../models/inference_cancel_audit.json)。

## 剩余范围

本阶段初始实现不含接纳层结果与提供方拒绝；后续 [try_infer](42-checked-inference-admission.md) 与 [可信响应封套](44-checked-inference-provider-outcomes.md) 已接通这两层。可交给业务代码的仿射 Waiting(Operation) 已由后续 [受检推理进度](40-checked-inference-progress.md) 实现；本页 infer_result 的 Unknown 仍保持暂停和恢复协议。固定次数资源循环已在 [受检资源循环](38-checked-affine-loops.md) 接通；一般循环控制、ContextView、授权解密/完整性提升、受检动态计划、完整实现对应证明和真实工作负载的同运行时 SDK 对照实验仍属总体目标。
