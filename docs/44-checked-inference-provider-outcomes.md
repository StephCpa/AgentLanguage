# 受检提供方拒绝与派发阻止

更新日期：2026 年 10 月 26 日。本文实现 [第 43 项设计](43-inference-provider-outcomes.md) 的两个阶段：已经接纳的请求可以在派发受阻后返回 Waiting；可信模型适配器可以返回明确拒绝。沿用 schema 21，不新增语言节点或改变既有结果 Variant 的分支集合。

## 已接纳后的派发阻止

`Store.try_start_attempt` 与旧 `start_attempt` 共用事务体。新受检入口先检查运行状态、主体、任务权限、lease、custody、操作关闭状态、attempt 归属及阶段；只有随后的当前请求授权与信息流检查可以转换为 `DispatchBlocked`，其他异常照常传播。旧接口仍抛出原异常，并保留授权检查先于 attempt 阶段检查的历史错误优先级。

受阻时 attempt 保持 Queued，预留保持 Reserved，不领取派发权、不调用适配器、不新建 attempt。`infer_progress`、`resume_infer` 及 `try_infer` 的 Started 路径随后使用原子观察接口生成 Waiting(Operation)。Started 不改成 Rejected；固定观察重放不重新派发。一个新的观察位置可以重新检查仍处于 Queued 的请求，但不能重新发送已取得派发权的 attempt。

派发阻止与观察不是同一事务。期间发生取消时，观察事务遵循既有取消和持有者规则；不会把过期快照固定为新的成功交接。取消尚未派发的队列可以释放预留，退款始终属于原 attempt 的出资分配。即使 Operation 已转移，或原任务分配已关闭，也不能把退款归给新的持有者。

对程序只返回既有 Waiting 分支。内部阻止原因不自动成为公开错误消息。旧 `infer` 和 `infer_result` 的派发异常行为保留；它们没有可继续携带 Operation 的 Waiting 返回分支。

## 可信响应封套

适配器可从 `al0.execute` 或 `al0.inference_response` 导入：

```python
InferenceResponse('Output', output='candidate', actual={'u': 1})
InferenceResponse('Refused', actual={'u': 1})
InferenceResponse('Refused')  # 实际费用稍后结算
```

`kind` 只允许 Output/Refused；Refused 不能携带候选输出。该封套只用于 infer 工具。旧 `Response` 保留兼容，其普通 output 即使包含 `inference_outcome` 等同名字段，也不会成为拒绝标记。

封套在可信业务证据的 output 之外保存版本化 `inference_outcome`。共享分类器检查标记形状和业务状态，再区分以下结果：

| 业务证据 | 推理结果 | 费用处理 |
| --- | --- | --- |
| 尚未派发、Unknown、Pending | Waiting，或旧终态接口的 Suspended | 保留既有预留 |
| Applied + Output，满足输出 schema | Success(Candidate) | 独立结算 |
| Applied + Output，不满足 schema | Error(InvalidOutput) | 独立结算 |
| Applied + Refused | Error(Refused) | 独立结算 |
| ProvenNoEffect | Error(NoResult) | 不据此自动免除费用 |
| 不合法的可信封套 | ContractError | 不将适配器错误伪装成模型拒绝 |

Refused 对应 Applied，含义是可信终态响应已到达，不是用户目标已完成，也不证明提供方没有内部计算或其他效果。旧 `infer` 遇到明确拒绝抛出专用 `InferenceRefused`（AL4005）；三个结果接口使用同一分类器。

业务证据先提交，费用随后提交。两者之间崩溃仍可恢复同一拒绝；迟到账单继续按原 attempt 结算。费用超上界仍触发原账本规则，不能被 Error(Refused) 吞掉。标签、固定观察身份、旧代次拒绝和任务取消规则沿用原协议。

## 可复现证据

`tests/test_inference_provider.py` 新增 30 项测试，包括 11 个拒绝路径和 3 个受阻路径的真实进程退出位置，以及跨任务返回、取消竞争、迟到费用、普通输出伪造字段、非法封套和致命前置条件。包含存储、对账与接纳的 117 项相关回归通过。首轮全量回归还检出了旧派发接口异常优先级的兼容问题；修复保留旧接口顺序，同时维持新受检入口的 attempt 前置验证；修复后的组合协议与提供方专项合计 41 项通过（204.359 秒）。

[源码示例](../examples/inference_provider.al0) 与 [SDK 示例](../examples/inference_provider.py) 生成相同 IR。[示例报告](../models/inference_provider_example.json) 包含三条路径：Output 调用一次并成功；Refused 调用一次，随后结算费用 1；Blocked 不调用端点，取消后恢复全部 10 单位可用额度。三者重放均返回原结果，新增事件为 0。单位是本地实验资源单位。

[运行时对照](../models/inference_provider_conformance.py) 对迟到拒绝、账单、撤权、取消、观察排列生成 120 条调度，并对未派发队列的观察、取消与重放生成 6 条调度，共 618 步。报告记录 5,760 组拒绝路径检查和 126 组受阻路径检查，部分组包含多个断言。它检查真实 Store 与独立端点；覆盖一个调用的有限调度，不覆盖任意程序、多 attempt、全部任务或 SQLite 交错。见 [对照报告](../models/inference_provider_conformance.json)。

本轮 SDK 类型验收仍覆盖 59 个公共方法、26 个基础计划与 9 个生成绑定计划；增加可信响应正例后共 11 个源码文件，基础负例增至 40 个，生成绑定负例 14 个，共享检查器专属拒绝 7 个。最终全量回归 **932 项通过，用时 825.434 秒**，见 [修复后回归日志](../models/inference_provider_fixed_full_regression.log)。新 wheel 在源码目录之外安装，Output/Refused/Blocked 消费路径与类型正反例均通过，见 [安装包报告](../models/sdk_wheel_audit.json)。首轮失败日志保留在 `models/inference_provider_full_regression.log`；当前权威结果为修复后日志。整体边界见 [实现状态](08-implementation-status.md)。

在项目根目录可分别复现专项测试、源码/SDK 示例和有限调度对照：

```powershell
python -m unittest tests.test_inference_provider -q
python -m examples.inference_provider
python -m models.inference_provider_conformance
```

## 后续接口

接纳、派发、响应分类与费用责任已经分开。下一项为 [ContextView](45-context-views.md)：把实际发送的上下文、来源、策略、接收者与预算口径纳入可恢复的输入契约。授权解密、完整性提升、受检动态计划、一般循环控制和同运行时真实工作负载对照仍属于完整目标。
