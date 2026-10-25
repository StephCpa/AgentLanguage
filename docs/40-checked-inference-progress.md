# 受检推理进度与固定观察

更新日期：2026 年 10 月 26 日。本轮实现 `infer_progress` 与 `resume_infer`，保留既有 `infer` 和 `infer_result` 的行为。接纳前拒绝及提供方明确拒绝的分类接口仍待实现。

## 语言接口

```text
infer_progress(model, request)
    -> Variant<{Error: InferenceError,
                Success: Candidate<T>,
                Waiting: Operation<Model>}>
resume_infer(move operation: Operation<Model>) -> 同一结果接口
```

整个 Variant 具有仿射义务，包括实际选中 Success/Error 的空资源情形。Success 保留 Candidate 来源，不能直接作为 Checked。Waiting 携带受检模型工具身份和持久 Operation 引用，支持 Bundle、跨任务输入/返回和固定次数资源循环。

模型 Operation 和 OpRef 参数现已公开；Intent/Permit 的公开工具种类约束保持原协议。`inspect` 可读取模型操作，`await` 对模型操作显式拒绝，必须通过声明 `infer:<tool>` 效应的 `resume_infer` 推进观察。OpRef、字符串、另一个工具的 Operation 均不能替代该控制输入。生成绑定用独立工具标记表示 Operation<Model>，不把模型输出类型当作工具身份。

首次 infer_progress 复用现有授权、接纳、预留和派发协议。resume_infer 不创建 slot、permit 或 attempt，只可能驱动同一操作中尚未派发的 Queued 尝试。Unknown/Pending 不自动重试；付费对账仍需独立工具调用。

## 事务与重放

schema 20 新增 `inference_observations`，主键为 `(run, activation, node)`。node 使用解释器已有的分支、调用和循环索引路径。每行保存源引用及代次、Catalog/工具/输出契约、观察快照与完整固定结果。它与只保存终态的 `inference_results` 分开。

Broker 先查历史观察；命中时核对源引用及契约，返回原始结果。未命中时核对真实操作的 run、工具、当前能力范围及控制代次，然后驱动已有队列。随后 Store 在同一事务中读取业务状态、调用本地 schema 分类器、转移控制权并保存结果：

| 读取状态 | 返回值 | 控制变化 | 费用 |
| --- | --- | --- | --- |
| NotDispatched / Unknown / Pending | Waiting(Operation) | generation 增加，持有者不变 | 保留既有责任 |
| Applied 且输出合法 | Success(Candidate) | 输入引用转为 Runtime/Spent | 未结费用仍保留 |
| Applied 且输出不合法 | Error(InvalidOutput) | 同上 | 已发生费用仍计入 |
| ProvenNoEffect | Error(NoResult) | 同上 | 不推断为免费 |

分类器只执行本地 schema 检查，不在数据库事务内调用适配器。无效输出不复制到 Error 或观察快照中，可信业务证据仍保留原始事实。观察快照中的业务状态和返回载荷取自同一事务，不能拼接两个时间点的状态。

历史 Waiting 不会因为迟到结果到达而变成 Success；必须进入新的观察位置。历史回执保留当时的代次，不能查询当前最大代次刷新旧别名。新位置消费过的引用会被拒绝。同一位置重放不会再次消费。

## 对应语义的证明义务

令观察身份为 k，源引用为 (o,g)，记录为 R[k]，当前控制代次为 G[o]。参考实现对应以下局部规则，后续形式模型需要独立检查：

1. **固定观察**：R[k] 一旦存在，只能以相同源引用和契约读取；读取结果始终相同。后续业务事实不修改 R[k]。
2. **一次转移**：没有 R[k] 时，只有当前持有者携带 g=G[o] 才能提交；记录 R[k] 与把 G[o] 增加到 g+1 属于同一事务。Waiting 返回新引用，终态不返回操作控制。
3. **责任独立**：观察转移不修改 attempt 的费用上界、原出资记录、预留或已确认费用。终态关闭控制不等于费用终态。
4. **取消排序**：取消与新观察通过数据库事务排序。取消先发生时不允许新观察；观察先发生时取消处理其新代次。历史读取不授予可用于新操作的控制权。

观察事务提交是本地结果选择与资源交接的线性化点。派发提交、远端执行和账单到达是不同事件；这个线性化点不能用于声称远端恰好执行一次。提交前退出恢复原代次，提交后退出恢复固定回执；两者都不能把已派发的 Unknown 当成可安全重发。

这些规则依赖既有单协调器执行边界、SQLite 事务与可信适配器。语言检查器保证合法程序中的局部单次消费，Store 则拒绝过时代次与任务 lease；二者不能互相替代。

## 取消、权限与信息流

观察提交前核对可变运行状态、TaskLease、当前持有者与代次；已经取消的模型操作不能发布新观察。任务取消先发生时，旧 worker 无法固定新结果；观察先提交时，取消处理新代次。迟到业务事实与账单仍由既有可信入口接纳。

撤权与取消不同：已有操作的历史事实可以继续观察，撤权不抹去已经发生的结果。任何尚未派发的请求仍在派发事务内检查当前授权；后续进度接口使用 try_start_attempt 处理受阻队列。受限调用不能借一个捕获的 Operation 超出当前能力表。

结果及内部 payload 保留输入、控制依赖、操作与工具输出标签。首次派发使用原请求的控制标签，不把尚未观察到的模型输出 taint 倒灌成此次请求的前置条件。

## 验收证据

`tests/test_inference_progress.py` 的 40 项专项测试通过，日志为 `models/inference_progress_focused.log`。其中一个参数化测试覆盖 12 个真实进程退出位置，另一个覆盖 schema 19→20 迁移提交前后。测试还包括等待/终态固定、旧代次拒绝、Bundle 与任务循环、权限范围、私密标签、输出错误计费、迟到账单、取消两种顺序，以及业务事实与观察事务的并发交错。

`examples/inference_progress.py` 与 `.al0` 生成相同 IR。独立端点记录一次模型请求；初始 Waiting 经任务中的 resume_infer 推进到 Success，后续循环保留终态。返回时尚有费用预留 1；迟到账单确认费用 1，剩余可用 9。源码重放与 SDK 结果相同且新增事件为 0。

另有两次针对性执行核对终态任务责任：任务返回 Success 后迟到结算，以及 Success 观察提交后、任务发布前取消。后者保持已存 Success 观察，但父任务结果为 Cancelled；两条路径均调用一次并最终确认费用 1。结果见 [终态任务检查](../models/inference_progress_terminal_task_audit.json)，这两次执行不计入上述 40 项单元测试。完整示例报告见 [推理进度示例](../models/inference_progress_demo.json)。

SDK 类型门禁通过：58 个公共方法，25 个基础计划、8 个生成绑定计划、36 个基础负例和 13 个生成绑定负例；另有 7 个必须由共享检查器拒绝的边界。完整回归 869 项全部通过，用时 562.902 秒，见 [回归日志](../models/inference_progress_full_regression.log)。安装包在源码目录外完成推理进度执行、迟到结果与账单、固定观察重放及类型检查，见 [安装包报告](../models/sdk_wheel_audit.json)。整体范围见 [实现状态](08-implementation-status.md)。

这些证据验证指定实现路径和故障位置。已有仿射有限模型尚未纳入本轮观察转移，不据此声明完整模型—运行时对应。

## 后续范围

预算不足和授权失败仍沿既有异常路径传播；提供方明确拒绝尚无独立适配器分类。不能把内部错误、取消栅栏或失效 lease 捕获成普通 Error。下一步接纳协议见 [推理接纳前结果设计](41-inference-preflight-results.md)。ContextView、授权解密/完整性提升、动态计划和真实工作负载仍属于完整目标。
