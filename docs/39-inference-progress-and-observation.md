# 未决推理：仿射结果与持久观察设计

状态：本页保留实现前的设计论证。infer_progress/resume_infer、模型 Operation 边界和固定观察现已实现，验收与实际语义见 [受检推理进度](40-checked-inference-progress.md)。下文“当前实现”“下一步”等时态描述的是设计时的基线；接纳前拒绝和提供方拒绝仍未完成，后续设计见 [推理接纳前结果](41-inference-preflight-results.md)。

## 建议的操作与结果类型

保留原 infer 与 infer_result，新增一对语义不同的操作：

```text
infer_progress(model, request)
    -> InferenceProgress<Model, T>
resume_infer(move operation: Operation<Model>)
    -> InferenceProgress<Model, T>

InferenceProgress<Model, T> = Variant<{
    Success: Candidate<T>,
    Error: InferenceError,
    Waiting: Operation<Model>
}>
```

infer_progress 负责首次授权、接纳和驱动已有排队请求；resume_infer 只处理已经存在的逻辑操作，不能据此创建新 slot、授权或 attempt。外部响应不确定时，后续观察读取已有事实。需要付费对账时，仍须显式使用对应工具与预算。

整个结果类型因 Waiting 分支而具有仿射义务，即使某次选中 Success 或 Error，也不能重复匹配同一个容器。Success 仍是 Candidate。循环可在 Waiting 分支继续观察，在终态分支保留既有结果；现有固定次数 repeat 尚不提供提前 break。

如果同时携带显式预算，应使用 `Bundle<{progress: InferenceProgress<...>, funds: Budget}>`。终态分支也须保留或归还该预算，不能把是否仍有 Operation 当作预算义务是否存在的判断。

## 当前实现中必须先调整的边界

当前共享 IR 的资源参数、Bundle/Variant 资源叶子以及 SDK 的 operation_parameter/opref_parameter 要求工具为 action。运行时内部模型调用虽然已有 Operation，但它尚不能作为受检模型资源返回。

下一步应只扩展 Operation/OpRef 的工具种类范围；Intent 和 Permit 的公开构造仍保持各自协议。不能为了导出模型 Operation 而统一放松所有保护类型。Broker 必须核对持久操作的真实工具、run 和接收方权限表，生成绑定需要区分模型的输出类型与操作的工具身份。

已有能力边界校验会从 Operation 追溯原 Permit 与授权父链，可作为复用点；这不等于模型 Operation 的公开传递已经经过验证。

## 观察身份与控制权

每一次 resume_infer 都需要由程序位置、activation 和循环 index 确定的独立观察身份。首次结果选择和对应资源交接必须持久化：

- Waiting：保存观察到的未决状态，增加 Operation 代次，返回该次固定交接回执。
- Success/Error：保存终态数据结果，关闭本次输入操作的控制引用；费用责任仍由既有账本承担，不能因不再返回 Operation 而释放未结算预留。
- 重放：返回该观察的固定结果和历史代次，不根据当前最大代次重建控制权。

当前 inference_results 以 Operation 为主键，只保存终态。Waiting 不能写入该表，否则首次等待会覆盖后续观察。应新增按观察身份索引的记录，终态数据可引用现有固定结果；语言结果、控制权转移和任务 lease 检查需要明确的原子提交边界。

读取到 Unknown 后、另一线程恰好送达终态事实时，允许本次观察固定为 Waiting，再由下一次观察推进。不能拼接“旧等待标签”和“新终态载荷”。具体实现须选择一致的快照及提交顺序，并测试这一交错。

## 接纳前错误与取消

完整推理错误代数还需要覆盖尚未产生 Operation 的情况。预算不足、授权不满足和提供方拒绝不能混为一个通用失败类别。接纳前拒绝应按调用位置固定首次决定，避免恢复时因预算或授权变化而自动开始一次原先被拒绝的调用。

应复用已有 Admission 的事务原则，明确哪些拒绝在本地已确定、哪些属于提交不确定。内部存储错误、无效 worker lease、契约冲突和取消栅栏不能通过捕获所有异常变成可恢复的普通 Error。

任务取消先发生时，旧 worker 不能发布新的 Waiting 或终态结果。观察提交先发生时，取消必须接管新代次。历史观察回执可以用于重建执行，但不能让已取消任务重新获得操作控制权。提供方迟到业务事实与账单仍可按既有可信入口处理。

## 下一轮验收

1. 输出类型保留模型工具身份与 Candidate 来源；action Operation、另一模型 Operation、裸字符串 ID 均不能冒充输入。
2. Unknown/Pending 产生携带真实控制引用的 Waiting；跨任务、Bundle、Variant、循环及受限调用保持边界与代次。
3. 同一观察重放保持原分支；下一观察可从 Waiting 推进到终态；观察本身不重新调用模型。
4. Success/Error 不丢失迟到费用和取消责任；旧 Waiting 引用不能再次驱动新工作。
5. 在派发、观察选择、代次变化、终态固定、任务发布/接收各边界退出进程，并与独立端点账单比较。
6. 对账收费、预算不足、撤权、取消和终态回执并发分别覆盖，不能以顺序成功示例代替。
7. 源码、SDK 与安装包消费一致；已有 infer/infer_result 与旧合法程序身份保持兼容。

后续仍须完成 ContextView、授权解密/完整性提升、受检动态计划、完整实现对应及真实工作负载实验。该接口设计不缩减这些总体目标。
