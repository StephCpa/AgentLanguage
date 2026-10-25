# 提供方拒绝与已接纳后的派发阻止

状态：本文保留实现前的设计决策，两个阶段已按此接通；当前实现与验证见 [受检提供方结果](44-checked-inference-provider-outcomes.md)。下表的“当前行为”指该设计提出时的基线。接纳前拒绝由 try_infer 处理，已接纳后的两种结果不归入 Rejected。

## 两个不同时间点

第一类是 Operation 已经接纳，但派发前授权已撤销或控制标签已收紧。此时有本地操作与预留，可能尚无远端执行。第二类是提供方收到请求后明确拒绝产生候选内容。此时远端调用已经发生，也可能已经收费。

| 情形 | 当前行为 | 下一步目标 |
| --- | --- | --- |
| 接纳前无授权/预算不足/信息流拒绝 | 固定 Rejected | 保持 |
| 已接纳、派发授权检查失败 | 抛出异常，Started/Operation/预留保留 | 明确的受检阻止路径，不改写接纳决定 |
| 超时或连接中断 | Started/Waiting | 保持，不自动重试 |
| 提供方明确拒绝输出 | 无独立适配器类型 | Started/Error(Refused)，费用独立 |
| 模型输出不符合 schema | Started/Error(InvalidOutput) | 保持 |

## 在派发事务内分类

不要在 Broker 外层捕获所有 AuthorizationDenied 并返回 Waiting。该异常也可能来自错误主体、任务权限边界或其他前置条件，必须先分离派发授权检查的事务位置。

建议提取已有 start_attempt 的事务体，并提供一个只在“当前请求授权或派发信息流检查”处返回 Blocked 的可信内部接口。主体、运行状态、lease、custody、attempt 身份和阶段不符仍抛出。Blocked 不领取派发权、不删除队列、不产生新 attempt，也不释放已预留费用。旧公开 start_attempt 可继续将 Blocked 转换为原异常，以保持旧调用方行为。

推理进度路径随后可以固定 Waiting(Operation) 的 NotDispatched 观察，允许程序继续传递或交还控制；已有 inspect 可以读取业务状态。无需为这种情况偷偷增加现有封闭 Variant 的分支。若需要向程序暴露具体阻止原因，应另行设计受标签约束的状态接口；不能把内部凭证与策略细节放进一般错误文本。

阻止决定与后续观察之间可能发生取消或外部状态变化。最终结果仍必须由观察事务读取一致状态并原子交接控制，不能直接把派发时读到的快照当作最终结果。

## 可信模型响应封套

提供方拒绝需要专用的结构化适配器结果，暂称 `InferenceResponse`，由可信适配器根据提供方协议构造。它应区分 Output 与 Refused，并携带可选实际费用。它不能由模型生成文本中的字段决定；普通 Json 输出即使恰好长成 `{kind: Refused}`，也仍是普通候选数据。

对已有 `Response` 的输出路径保留兼容。新的封套只允许 model 工具使用，action/check/observe 不能借用它绕过原响应协议。封套形状不合法是适配器契约错误，不是模型 Refused。

拒绝标记应在可信业务证据中、普通 output 载荷之外持久化。候选分类器读取这一标记后输出 Error(Refused)，原始自然语言拒绝文本不自动进入公开结果。核心 InferenceError 目前只含 code/tool/version，足以区分 Refused；需要更多原因分类时再扩展明确的版本化协议。

业务状态映射需要与模型工具的语义一致。不能把 Refused 一律映射为 ProvenNoEffect：提供方可能在拒绝前已经计算、调用内部工具或产生其他外部效果。可把“可信终态响应已送达”登记为 Applied，再由推理层区分输出与拒绝；这与现有 Applied 但输出格式错误的路径相同，不声称用户目标成功。公开文档必须说明这一点。

旧 infer 返回 Candidate 而不是 Variant，遇到明确拒绝应有专门异常，不能把拒绝文本当成候选或依赖 schema 偶然报错。infer_result、infer_progress 和 try_infer 的已接纳路径则应共享同一分类器，避免三套错误优先级漂移。

## 验收要求

1. Refused、InvalidOutput、NoResult、Unknown/Pending 和接纳前 Rejected 分别测试；嵌套结果保留 Candidate/Operation 类型边界。
2. Refused 先记业务证据，费用随后记录；两者之间退出进程仍恢复同一拒绝，并允许迟到账单。
3. 模型输出伪造拒绝字段不会改变分类；只有适配器封套及可信证据入口可以声明拒绝。
4. 已接纳后撤权、派发阻止和取消竞争中，不创建新 attempt、不重复预留、不把 Started 改成 Rejected。
5. Waiting 的固定观察、旧代次拒绝、跨任务责任、标签与受限能力边界继续成立。
6. 扩展已有组合调度模型和独立端点对照，加入阻止派发、Refused 与迟到账单；运行源码/SDK/生成绑定/wheel 验收。

完成这两个阶段之后仍需推进 ContextView、授权解密/完整性提升、受检动态计划、一般循环控制、完整实现对应及真实工作负载。它们不因推理结果接口扩展而被视为已完成。
