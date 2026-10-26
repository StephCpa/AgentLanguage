# 开发任务契约与配对评分

原先的单提交发布例继续用于校准。新增 `experiments/development_tasks_v1.json` 固定三个工作流构造任务及其输入、额度、故障位置和预期行为，作为开发预实验轨道。任务使用真实本地 Git 端点；源代码和 SDK 都由相同运行时评分，可选择已有 Docker 构造边界。

## 三个任务

| 契约 | 开发要求 | 评分场景 |
| --- | --- | --- |
| conditional-release | 一个程序根据运行时 Bool 参数决定是否发布；false 分支不接纳/派发工具 | false、true、回复丢失、提交后进程退出，共 4 场景 |
| ordered-release | 两个不同提交分别形成操作；第一项完成对账后才派发第二项 | 正常、第一/第二项回复丢失、第一/第二项提交后退出，共 5 场景 |
| admission-fallback | 正常发布，额度不足返回 Deferred，授权后撤权返回 Denied；保留返回许可和恢复语义 | 正常、零额度、撤权、回复丢失、进程退出，共 5 场景 |

两个前端各执行 14 个场景，共 28 格。评分前先保存每份提交的源码快照，只构造一次，将同一份受检 IR 用于全部场景，宿主在每次执行前继续复核。输入和额度在运行时改变；构造输入不会告知本次评分场景。SDK 可以使用合法的动态命名，只需保留这次构造得到的计划，不要求 Python 构造函数每次产生同一字节串。后续场景标记 `construction_reused`，不得重复累加其构造时间。构造失败也保留在全部分配场景中。整项任务要求所有场景通过且使用同一计划，拒绝、超时或未决结果不会从分母删除。

这三个契约共属 **一个 Git 工作流任务族**。14 个场景、两种前端或重复运行不能被计成独立开发样本；当前没有足够的任务族用于语言效应推断。它们也不是跨模块补丁修复、依赖升级或完整 Agent 任务基准的替代品。

## 外部状态与事件顺序

独立 Git oracle 核对新增引用及其对象集合，接受记录核对实际发布序列。宿主事件再验证：第一项查询已产生 Applied 证据，第二项才获得 DispatchStarted。只看最终引用集合不能发现顺序错误；只看总额度也无法判断费用是否用于规定的对账步骤。

错误对照分别检验：false 分支偷偷发布、交换两个批准提交的顺序、把撤权错误报告为额度不足，以及省略第一项对账而在第二项多查一次。最后一种程序具有正确最终引用集合、发布顺序、总确认额度及 CompletedClean 状态，仍因缺少规定的对账依赖而失败。评分不依赖程序自报“成功”。

故障使用持久协议位置。接受记录为发布分配序号，回复丢失或进程退出可精确落到第一或第二项。撤权发生于首次执行的授权返回之后、接纳之前；恢复阶段不再次注入。故障是否真正到达属于评分条件。正确的 zero-effect 路径无需创建 Git 操作，但必须返回对应结果、没有费用或未决责任，重放也不得增加事件。

## 任务包与提交

```powershell
python -m scripts.development_tasks prepare --output <new-package-directory>
python -m scripts.development_tasks calibrate --output <new-results-directory> `
  --python <python-with-mypy-1.18.2>
```

需要隔离 Python 构造时，添加 `--builder-image <trusted-image>`；SDK 仍依次通过严格 mypy、构造、共享 IR 和宿主复核，不能为了让负例“更明显”而跳过检查。

每个任务包中的 `participant/` 只包含任务说明 JSON、固定 Catalog 和生成绑定。参考答案、评分场景、Git 对象库、端点及运行状态留在该目录之外。以后构造模型提示时只可使用明确批准的参与者材料及两组相匹配的语言文档/样例，不能递归上传整个任务包。参考答案虽然不在参与者目录内，仍是仓库公开的研究材料；本批任务标记为 development，不声称是模型未知的确认性测试集。

SDK 入口保持 `build(catalog: Catalog, commit: str) -> CheckedPlan[str]`，`commit` 为目标提交；另一批准提交的 ID 写在公共任务说明中。AL-0 使用 `main` 入口，条件任务声明 `enabled: Bool` 参数。评分另一组提交：

```powershell
python -m scripts.development_tasks evaluate --package <one-task-package> `
  --al0 <submission.al0> --sdk <submission.py> --output <new-results-directory> `
  --builder-image <trusted-image>
```

单格定位仍使用 `scripts.git_pair_evaluation evaluate`，增加 `--case <declared-case-id>`。案例只能从宿主任务包选择，提交程序不能自定成功条件。完整场景评分保存源码、IR、构造和执行诊断、事件、费用与独立 Git 回读；任务汇总保存契约摘要、顺序、任务级通过情况及计划稳定性。参考答案要求同义 IR，任意合法提交则不因 IR 与参考不同而被拒绝。

## 研究进度

本机参考校准的 28 格全部通过，三项任务在两个前端均完成所有场景并产生相同 IR。开发任务及原配对评测器的 13 项联合回归通过（217.237 秒），日志为 `models/development_tasks_regression.log`；其中还验证了具有动态计划命名的合法 SDK 只构造一次，并不会因与 AL-0 的 IR 字节不同而被误拒。

这些任务与参考答案用于检验不同语言机制的评分链路，尚未运行真实模型生成或修复实验。当前环境未发现模型 API 凭据，提供方、固定模型版本与费用上限仍待确定。报告明确记 `model_calls: 0` 和 `causal_language_benefit: not_measured`，不会将人工编写的参考答案算作模型首次通过率。

后续需接入每轮原始提示、模型输出、诊断、修复、实际 usage/费用和最终外部状态记录；冻结与开发任务隔离的测试集，并继续补充跨模块修复和证据绑定工作负载。费用缺失不能按零成本计算，基础设施失败也不能归因于某个前端。
