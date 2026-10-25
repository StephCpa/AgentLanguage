# 受检推理接纳与固定拒绝

更新日期：2026 年 10 月 26 日。本轮实现 `try_infer`，在模型 Operation 产生之前固定本地拒绝，并复用已有推理进度协议处理已接纳请求。提供方明确拒绝及受检派发阻止结果已在后续 [第 44 项](44-checked-inference-provider-outcomes.md) 扩展。

## 分层结果

```text
try_infer(model, request)
  -> Variant<{
       Rejected: InferenceRejection,
       Started: Variant<{
         Error: InferenceError,
         Success: Candidate<T>,
         Waiting: Operation<Model>
       }>
     }>
```

Started 只表示本地接纳成功；它不是远端已执行、已收费或用户目标已达成的证明。Rejected 表示本位置未产生调用操作。外层 Variant 因 Started 中的 Waiting 而具有仿射义务，即使当前结果为 Rejected，也必须处理或返回整个容器。

`InferenceRejection` 是新增的保留数据 schema，字段为 code、tool、version，均为 Text。当前代码为 BudgetExceeded、AuthorizationUnavailable、FlowDenied。无绑定、未知授权、过期及撤权统一使用 AuthorizationUnavailable，不复制授权标识或内部诊断。结构是普通数据，不能作为权限证明；伪造同形记录不会产生任何控制能力。

新增保留名意味着此前自行注册同名 `InferenceRejection` 的 Catalog 需要重命名其自定义 schema；注册时会明确拒绝重名。其他既有工具/schema 描述的指纹计算保持原规则，旧 infer、infer_result、infer_progress 接口不增加分支。

## 原子边界

schema 21 新增 `inference_admissions`。主键为 run、activation、node，固定请求及控制标签、Catalog/工具版本/输出契约、出资 allocation、grant 选择和结果。没有 grant 也记录为明确的空选择。

读取历史结果时精确核对这些绑定。同位置改变请求、授权选择或资金来源会产生 ReplayMismatch。预算释放后，同一位置仍返回首次 BudgetExceeded；显式进入新位置才能重新尝试。原位置没有 grant 时，恢复调用不能悄悄补绑一个新 grant。

成功接纳在一个事务内完成：授权检查、内部 Permit、Permit 来源与 custody、预算预留、Operation、attempt、outbox、不可变出资记录以及 Started 决定。`Store.authorize` 的事务体提取为 `_authorize_in_tx`，旧公开 authorize 仍保留自己的事务包装和授权键重放行为。

只有授权不可用、预算不足和信息流拒绝进入普通 Rejected。其失败 savepoint 会回滚内部 Permit、预算及控制记录，并恢复解释器的引用登记映射；不会残留一个 Live Permit。已认证主体、运行生命周期、TaskLease、资金/资源控制权、任务效应及契约/证据完整性检查位于拒绝分类之外。内部错误继续抛出，不能伪装成正常拒绝。

接纳决定与进度观察分属两个事务。成功决定先固定唯一 Operation；若在观察前退出进程，恢复只使用该操作及原 attempt。观察仍依照 schema 20 的固定结果与代次协议处理。已接纳后、派发前撤权不会改写为 Rejected：本阶段初始实现抛出 AuthorizationDenied，Started 决定及预留仍在，适配器未调用。后续 [受检派发阻止](44-checked-inference-provider-outcomes.md) 已将进度接口接通为固定 Waiting，旧直接派发接口保留异常。

## 标签与取消

请求、当前控制和派生能力的标签共同限制接纳结果。派生能力标签也进入实际请求 Intent，因此选择一个私密能力不会丢失其控制依赖。FlowDenied 返回的原因和外层结果保留这些限制。

任务取消先于决定事务时，旧 worker 不能固定新决定。Started 提交后取消时，任务处理已存在的操作；尚未派发的队列可被取消并释放预留，但成功决定本身不改写为 Rejected。派发后的费用责任继续由既有独立账本承担。

## 可复现证据

`tests/test_inference_admission.py` 新增 33 项专项测试。与前端及生成绑定检查合计 84 项通过，用时 53.483 秒，日志为 [专项回归](../models/inference_admission_extended.log)。进程测试覆盖 16 个成功路径退出位置、拒绝决定提交前后，以及 schema 20→21 迁移提交前后。另覆盖并发争用最后一单位额度、预算释放后固定拒绝、授权选择漂移、私密标签、嵌套 Variant 匹配、跨任务结果、取消前后及回滚引用登记。

源码和 SDK [示例](../examples/inference_admission.al0) 产生相同 IR。Success、InvalidOutput、Pending 均先进入 Started，分别返回内部 Success、Error、Waiting；预算不足、无授权和撤权进入 Rejected，端点调用及残留 Permit 均为零。六条路径重放均不增加事件；端点记录独立保存在 SQLite，结果见 [示例报告](../models/inference_admission_example.json)。

`python -m models.inference_admission_conformance` 将一个独立状态模型与真实 Store 对照：对一次已接纳的 Pending 调用，排列业务事实、账单、撤权、取消和再次观察五个事件，共 120 条调度、600 步、5,760 组状态与重放检查。组合属性按组计数，部分检查组包含多个断言。每步比较业务状态、取消标记、预算、控制代次、固定决定、历史结果及端点调用次数。另有三条接纳拒绝恢复轨迹，确认旧位置不重开、新位置可显式尝试。报告见 [组合对照](../models/inference_admission_conformance.json)。

这组对照固定为一个操作、一个后续观察、每类事件一次，不涵盖任意程序、任务转交/race、多次尝试、提供方拒绝或全部 SQLite 交错。它补充既有模型对应工作，不构成完整语言证明。

该阶段 SDK 类型门禁覆盖 59 个公共方法、26 个基础计划、9 个生成绑定计划、38 个基础负例与 14 个生成绑定负例；7 个静态接受但共享检查器拒绝的边界仍保留。完整回归 902 项全部通过，用时 608.201 秒，见 [回归日志](../models/inference_admission_full_regression.log)。从源码目录之外安装 wheel 的消费环境已通过成功接纳、预算拒绝、无授权拒绝、固定重放及类型检查，见 [安装包报告](../models/sdk_wheel_audit.json)。整体范围见 [实现状态](08-implementation-status.md)。
