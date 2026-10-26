# 生成与修复实验记录

`scripts/generation_journal.py` 为一个开发任务的 AL-0 与强 SDK 两组保存原始请求、响应、逐轮修复和评分。它导入提供方回执，不发送网络请求。真实提供方适配器、固定模型选择和调用费用上限仍待配置；本轮校准使用明确标记的 fixture，模型调用数为零。

## 固定材料和分配

初始化时保存任务契约、Catalog、生成绑定、参与者材料，以及共享说明和两组语言材料。两组先各分配一个任务，未请求、拒绝、错误、超时和基础设施故障都保留在记录中。每组使用相同的 `max_attempts` 和独立的 `budget_usd_per_arm`，一组用完预算不会挤占另一组。模型、提供方和采样参数在初始化时固定，回执的模型标识必须匹配。

模型请求只含 `participant/` 的三个文件、明确提供的说明材料、该组先前输出及评分反馈；不会递归读取任务包或加入参考答案。宿主在 SQLite 中保存完整评分契约，包含本地 Git 端点路径；它不属于模型请求。两组材料的字符数分别记录，字符数不等于 token 数，记录器也没有证明材料内容和示例难度已匹配。正式实验仍需固定经过检查的材料及计数方法。

每份响应通过 JSON pointer 提取源码，不删除代码围栏、不自动修正程序。原始文本完整保留；提交给既有文件前端时统一 CRLF/CR 换行为 LF，并记录这一规范化，避免 Windows 文本写入再次转换 CRLF。单组评分只执行所分配的前端，不用参考答案补齐另一组。该轮程序只构造一次，用同一 IR 执行任务所有场景。

## 轮次与恢复

轮次依次经过 `requested → received → evaluating → evaluated`。请求在导出前先保存；导入响应与占用费用在同一事务中提交。同一回执重复导入不创建新轮次，不同内容不能覆盖既有响应。尚有未处理请求或评分时不能再预约该组的新请求，达到成功或尝试上限后也不继续生成。

输出进入现有评测器，完整源码、IR、构造诊断、运行事件和独立 Git oracle 保存在轮次目录。用于下一轮提示的反馈只选择失败分类、检查项及构造诊断，不包含授权表、外部状态快照和 oracle 对象。单项诊断最多 16,000 字符，截断会明确标记；完整诊断仍在原报告中。重复场景不重复附上同一次构造诊断。

评分启动前先保存 `evaluating`。如果评分已写出完整报告而记录器尚未提交结果，再次执行 `evaluate` 只导入原报告；如果没有完整报告，则提示检查原进程和产物，不自动重新执行可能已产生外部动作的程序。该状态可能表示进程仍在运行，也可能表示中断，不能仅靠数据库标记断言进程已死。当前没有任意中断点的自动评分恢复器。

SDK 类型诊断在本地和 Docker 两条路径共用分类：普通类型错误及 mypy 1.18.2 的 `[syntax]` 语法错误属于提交拒绝；没有相应诊断的工具退出属于 `static_tool_failure`。评分基础设施故障不会被报告为语言语义错误。它仍保留在已分配任务和尝试中，后续统计应另外报告其比例。

## 用量与费用

导出请求前记录一个正的费用上界，已知收费加未知收费的预留不得超过该组预算。费用使用十进制字符串，支持九位小数。缺少 `cost_usd` 时总费用为 `null`，不会当成零；预留一直保留到显式迟到结算。实际收费超过该请求上界时仍保存真实报告值，并阻止该组继续预约，不能通过拒收账单隐藏超支。

这些限制只约束记录器允许导出的请求。上界由调用者提供；记录器没有依据真实 tokenizer、价格或提供方硬限制验证它，也无法阻止人在外部另行调用模型。因此它不是真实提供方的支付网关或费用上限保证。导入的 usage、账单及模型标识是宿主报告，原始提供方 JSON 同时保留，尚未通过服务端查询认证。

`usage` 要么缺失，要么包含非负整数 `input_tokens` 和 `output_tokens`；可附加缓存、推理 token 等原始分类。缺失的统计合计保留未知，不能用已知子集合冒充总量。原始请求、响应和本机记录时间可以重建记录过程；导入延迟包含人工操作，不能当成模型延迟或纯修复时间。原始响应无需包含私有思维链。

## 命令

先用 `scripts.development_tasks prepare` 创建开发任务包。为单个任务准备配置 JSON：

```json
{
  "provider": "<selected-provider>",
  "model": "<fixed-model-version>",
  "parameters": {},
  "evidence_mode": "provider_import",
  "max_attempts": 3,
  "budget_usd_per_arm": "1.00"
}
```

数值只是配置格式示例，不代表已批准调用或实际价格。材料 JSON 必须含三个非空字符串 `shared`、`al0`、`sdk`。不要把凭据放在模型参数、材料或回执中。

```powershell
python -m scripts.generation_journal --journal <new-journal> init `
  --package <one-development-task> --config <config.json> --materials <materials.json>
python -m scripts.generation_journal --journal <journal> reserve --arm al0 --upper-usd 0.10
python -m scripts.generation_journal --journal <journal> show-request --request-id <id>
python -m scripts.generation_journal --journal <journal> receive --request-id <id> --receipt <receipt.json>
python -m scripts.generation_journal --journal <journal> evaluate --request-id <id> --builder-image <trusted-image>
python -m scripts.generation_journal --journal <journal> summary
```

`reserve` 和 `show-request` 仅输出保存的请求 JSON，均不调用模型。回执格式为：

```json
{
  "request_id": "<journal-request-id>",
  "model": "<fixed-model-version>",
  "status": "output",
  "raw_response": {"text": "<exact-source>"},
  "output_pointer": "/text",
  "usage": {"input_tokens": 100, "output_tokens": 200},
  "cost_usd": null
}
```

`raw_response` 保存实际回执，pointer 随其真实结构设置；示例字段不表示任何提供方 API 结构。其他状态为 `refused`、`error`、`timeout`，它们不执行源码。未知费用可用 `settle --request-id … --cost-usd … --evidence …` 补记；原始回执不会被覆盖。重复执行 `summary` 不触发任何实验动作。

真实模型输出必须指定 Docker 镜像。`--trusted-fixture` 只对初始化为 `fixture` 的已审阅代码开放，不能用它绕过 `provider_import` 的构造隔离要求。数据库和轮次目录是本地实验工作区，含原始输入输出及宿主状态；公开归档选取汇总和明确检查的报告，不直接上传整目录。

## 校准与尚未完成的工作

`scripts/check_generation_journal.py` 分别给两组一份语法错误程序，再导入各自参考答案。它检查首次失败、诊断进入修复请求、最终通过、未知费用保留、迟到零费用结算及单组执行。这个过程是人工 fixture 驱动的记录器验收，不是模型成功修复了程序。CI 在真实 Docker 后端运行同一流程，并保留 `generation_journal_ci.json`。

本地联合回归 23 项通过（241.325 秒），随后对最终费用/恢复逻辑及 mypy 分类补验的 11 项专项测试通过（14.785 秒）。日志分别为 `models/generation_journal_regression.log` 和 `models/generation_journal_focused.log`；两次集合存在重叠，不应合计为 34 个不同测试。

记录器目前只支持公开的 development 任务。仍需真实提供方适配器及其计数/收费验证、匹配语言材料、实际模型实验、跨模块代码修复和证据绑定任务，以及不反馈评分细节的独立测试集。当前报告始终声明 `causal_language_benefit: not_measured`；不能把 fixture 轮次或故障场景作为独立开发样本。
