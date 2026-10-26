# R1–R3 修复与下一轮实验门槛

本轮按固定提交 `f9b2224a321252c3a4b2f2bc7db02ca932980796` 的复核意见修复 Git 对账、评分失败门禁和故障分类。审核给出的 F1–F7 关闭结论限于原反例及已检查组合，不扩大成整体形式化证明。本轮没有模型调用，也不改写六次历史响应及其原始评分。

## R1：只接纳精确 Git 引用的证据

[Git 官方文档](https://git-scm.com/docs/git-ls-remote) 规定 ls-remote 的参数按引用尾部匹配，所以完整名称也可能匹配 `refs/original/` 中的备份。适配器现在先验证行格式和重复记录，再按完整引用名相等筛选。唯一精确目标且 OID 正确才产生 Applied；只有备份、没有精确目标时仍没有业务确认，包括 Git 退出码为 0 的情况。错误 OID、重复或非法行仍拒绝。

真实 Git 回归覆盖：丢失发布回执后增加合法备份仍可完成对账；只有备份时不把它当成成功证据；原有不可变引用篡改仍被拒绝。改动不扩展为 main 合并、多引用事务或网络 exactly-once。

## R2：评分有效性独立于模型任务结果

主评分和单围栏敏感性评分共用 `evaluation_contract.require_valid_evaluations`。所有已有评分报告先落盘，再遇到 `infrastructure:*` 或 `static_tool_failure` 时使 CLI 非零退出。普通语法错误、业务错误及预定资源限额失败仍是有效实验观测。Unknown 补测和 journal 分类复用同一基础设施判别。

回归分别把基础设施故障注入主轨道和敏感性轨道，并检查完整报告保留。另用真实 CLI 子进程和真实评估器、合成的 Docker 启动不可用故障验证非零退出；这里不把合成启动故障声称为实际 Docker 服务宕机。

新生成报告明确区分 `pr_head_sha` 与 `executed_commit_sha`，后者是 Actions 实际检出的提交，可能是临时合并提交。旧 `code_commit` 字段暂保留为执行提交的兼容别名；历史 provenance 不追溯改写。

## R3：统一资源分类，识别可信编译器故障

预先规定：三个构造阶段的 OOMKilled、超时、输出超限统一为 `construction_limit`，计作限额下的提交失败，而不是自动给予重试的基础设施故障。

AL-0 只把专用退出码与完整 SourceError 诊断记录判为 `static_rejection`；其他可信编译器异常为 `static_tool_failure`。mypy 继续按固定版本的源码诊断区分源码和工具错误。SDK 构造阶段执行候选 Python，普通候选异常仍为 `build_failure`；这与可信编译器内部异常的归属不同，不能把任意候选代码异常都当成基础设施错误。

非 JSON 或缺少必要结构的 docker inspect 输出统一转为 BuilderUnavailable，保留报告且执行容器清理，不创建运行数据库。测试明确区分合成三阶段 OOM/崩溃分类、合成 inspect 异常，以及 CI 中实际触发 SDK 容器 OOM 的探针。

## 共同协议与可编译材料

`source-or-single-fence-v1` 是下一轮可选择的共同解析协议：接受原始源码或恰好一个完整的共同白名单围栏，拒绝空输出、混合说明、多块或不完整围栏。仅规范换行和去除外层围栏，不修改内部代码。结果保存原始文本、提取文本、解析器版本及两种摘要。纯文本是否为合法源码由实际前端判断，解析器不猜测没有围栏的自然语言说明。

`experiments/teaching_v2` 提供与 Git 任务答案无关的运行时参数 → 动态记录 → 字段读取例子。全部注册例子必须由真实 AL-0 编译器和强类型 SDK 构造器检查，且得到相同 IR。SDK 的动态 record 字段正确性仍由共享 IR 检查，生成绑定提供字段访问和参数类型。纯 Catalog 暴露的空 TOOL_TAGS 类型注解问题也已修复。

新协议及材料是独立的预检产物。现有 pilot v1 调用器和 journal 默认协议仍是原始源码；新的整套实验必须显式接入并冻结新协议后才启动，不静默替换旧实验的行为。

## 判据负例与结果口径

| 边界 | 校准证据 |
| --- | --- |
| 未确认却返回 Published | `test_discarding_query_and_returning_published_fails_only_result_check` |
| 空查询被解释成 NoEffect | `test_empty_query_cannot_be_reported_as_no_effect` |
| 崩溃恢复后额外发布 | `test_duplicate_publication_after_crash_is_not_a_successful_recovery`：真实 Git 收到 first/resume 两份发布，判失败 |
| 取消或查询故障后隐去责任 | `test_query_transport_failure_and_cancel_do_not_erase_history` 验证真实运行仍保留两项义务；`test_claimed_clean_status_cannot_hide_unresolved_obligations` 注入错误状态视图并拒绝 |
| 每场景重新构造或偷换源码 | 原有动态 UUID SDK 只构造一次且 IR 稳定的测试，加上 `test_reconstructed_plan_from_another_submission_is_rejected`；容器边界继续隔离 host 状态 |

这些是具体负例和不变量回归，不是穷尽所有错误程序。Pending 与 ProvenNoEffect 的测试应建立在能提供相应证据的契约上，不能把 Git 空引用改称这两种状态。

每个新评分行单独记录：

- `safe_task_success`：是否满足本场景的动作、结果、配额、状态和恢复契约。
- `business_goal_completed`：本次运行是否在独立 Git oracle 中留下目标提交集合，且没有删除/覆盖；不包含返回文字、配额或恢复安全的判断。Skipped、Deferred、Unknown 均不能仅凭安全处理而算发布完成。
- `false_success_claim`：返回 Published，但目标集合未达成，或相关发布操作缺少 Applied 业务确认。它是当前 Text/Git 任务的明确判据，不是通用的语言级证明。

每臂摘要分别统计安全通过、业务完成与错误成功声明的场景数；场景数仍不能当作独立任务数。

## 新实验尚需冻结的内容

`experiments/next_pilot_protocol_v1.json` 记录本轮预先确定的错误/输出协议，以及调用前仍必须固定的任务集、材料、模型参数、交叉平衡顺序、共享费用账本和基础设施重评上限。本轮不宣布完整下一轮研究已经冻结：当前三个开发契约属于一个 Git 任务族，还需要独立任务及其负例校准，不能用更多同任务调用扩大样本。

正式实验应按任务配对，两臂共享 Catalog、运行时、故障计划、预算和修复次数。拒绝、超时、无输出、格式失败及预算耗尽保留在任务分母；基础设施重评只使用相同已保存源码、另记评估身份，不增加模型修复机会。已有六次调用及 0.0169878 美元保守预留继续计入原 1 美元总账本。

未来若提供机器可验证的成功声明，应另行设计两前端共享的 IR/运行时证据类型：绑定 operation、intent、目标和适配器版本，出口复核，文本仅作说明。这是后续语言设计事项，本轮没有新增或宣称实现 Published(receipt) API。
