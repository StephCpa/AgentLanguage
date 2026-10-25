# AL-0 来源与证据登记

检索与核对日期：2026 年 10 月 25 日。以下只登记本轮实际访问且用于设计的来源。论文中的实验属于作者报告，不是 AL-0 复现结果；原方案两份有限模型另有实际复现记录。

补充核对日期：2026 年 10 月 26 日。为 [ContextView 设计](45-context-views.md) 查阅以下官方文档：

| 来源 | 用于约束设计的事实 | 不据此推出什么 |
| --- | --- | --- |
| [Claude token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting) | 结构化输入计数；文档将计数称为估计；存在不支持的请求类别 | 不把计数响应或经验余量当作严格 token 上界，也未实际调用其 API |
| [Transformers v5.20.0 chat templates](https://huggingface.co/docs/transformers/v5.20.0/chat_templating) | 模型模板决定控制 token；模板后再次分词需要避免重复特殊 token | 不把普通文本长度当作完整请求计数；未安装或运行 Transformers |

## 用户提供的材料

原始附件位于 `C:\Users\76790\.codex\attachments\66c57ef3-73f2-4209-829f-702c8e2aeb82\已粘贴的文本.txt`。

[面向 LLM Agent 的编程语言调研与 AL-0 核心设计](https://chatgpt.com/space/page_569857d5b1c481919e6f2c17712e58e0)包含完整方案和两个 Python 模型。本轮读取其正文并提取代码，复现结果见模型报告。

## 直接相关研究

| 来源与固定版本 | 本轮用于支持什么 | 不据此推出什么 |
| --- | --- | --- |
| [Etas v1](https://arxiv.org/html/2607.17780v1) | 类型、效应、请求轨迹和动态检查义务是直接近邻；第 2、5、6、8 节区分语言核心与解释器能力 | 不把论文的证明草图当机械化证明，不断言全部并发能力缺失 |
| [Language-Based Agent Control v2](https://arxiv.org/abs/2605.12863v2) | 生成代码在外围类型及策略环境中检查，是强宿主语言对照 | 不预设独立语言优于该方法 |
| [Quasar v2](https://arxiv.org/abs/2506.12202v2) | 内部程序与外部调用分离、效应驱动功能；条目注明 COLM 2026 | 不把其功能示例等同于持久预算协议 |
| [Turn](https://arxiv.org/abs/2603.08755) | 作为 Agent 语言背景近邻登记 | 本轮未据此推断恢复或资源安全范围 |
| [Atomix v2](https://arxiv.org/html/2602.14849v2) | 效果结算、推测与并发外部效果；多端点边界 | 不推导任意异构端点原子提交 |
| [Approve What Exactly v1](https://arxiv.org/abs/2610.21063v1) | 规范化效果、可信展示、审批绑定及单次使用已有工作 | 不将新增 approve 关键字视为研究贡献 |
| [Fault-Tolerant Budget Conservation v1](https://arxiv.org/html/2610.00349v1) | 故障预算守恒是直接近邻；第 6、7.7、7.8、8 节给出协议与实现前提 | 不将取消、恢复和预算守恒称为未研究问题 |
| [Authorization for Self-Modifying AI Agent Populations v1](https://arxiv.org/html/2610.00347v1) | 分叉、替换、回滚与撤权的授权守恒已有研究 | 不将 AL-0 的回滚不复活授权单独作为新颖性 |
| [Agent Contracts v3](https://arxiv.org/abs/2601.08815v3) | 多维资源约束和委托资源守恒已有框架 | 不把 allocation 约束自动当作故障外部费用保证 |
| [CaMeL v2](https://arxiv.org/abs/2503.18813v2) | 控制与数据流分离及能力策略 | 不推导 AL-0 自动获得相同非干扰性质 |
| [Fault Tolerance via Idempotence](https://www.microsoft.com/en-us/research/publication/fault-tolerance-via-idempotence/) | 幂等计算组合与容错已有长期研究基础 | 不把稳定操作 ID 或恢复本身称为新发明 |

新增的两项 2610.00349 与 2610.00347 在检索条目中给出 2026 年 9 月 29 日的提交时间；本研究用 arXiv ID 和访问版本标识它们，不从编号月份推断事件发生月份。

## 官方工程文档

| 来源 | 用途 |
| --- | --- |
| [Temporal Activity Execution](https://docs.temporal.io/activity-execution) | 查阅持久活动执行背景，不使用截取不到的细节支持额外主张 |
| [Microsoft Durable Task programming model](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-types-features-overview) | 编排重放和 activity 至少一次执行的边界 |
| [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | checkpoint 和 store 的职责；原 durable-execution URL 当前重定向到此页 |
| [Stripe Idempotent requests](https://docs.stripe.com/api/idempotent_requests) | 官方文档区分 API v1/v2 幂等行为，支持契约版本化设计 |
| [Rust Ownership](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html) | 2026-10-26 核对单一所有者与移动基础；不把内存释放等同于外部效果撤销 |
| [SQLite Atomic Commit](https://www.sqlite.org/atomiccommit.html) | 2026-10-26 核对事务原子性及底层假设；该文主要描述 rollback journal，本项目 WAL/FULL 配置另由实际进程退出验证 |

以上网页可变；正式实验需冻结适配器、端点 API 版本和访问日期，并保存实际配置。文档能力不等于本项目已经成功集成相应服务。

## 实验来源

| 来源 | 用途 |
| --- | --- |
| [AgentDojo](https://arxiv.org/abs/2406.13352) | 不可信工具数据与提示注入的评测环境 |
| [τ-bench](https://arxiv.org/abs/2406.12045) | 业务政策、终态及多次运行可靠性 |
| [τ²-bench](https://arxiv.org/abs/2506.07982) | 用户与 Agent 都能改变共享环境的任务设计 |

## 检索范围和仍需核对的内容

本轮先重查原方案直接近邻，再以 agent programming language、resource budgets、crash recovery、affine permissions、authorization revocation、budget conservation 等组合检索，补充协议层及持久执行工作。仅用原论文与官方资料支撑技术比较。

原方案中的 LMQL、DSPy、SGLang、BAML、FIDES、Progent、AgentSPEX 等保留为下一轮广泛生态对照候选；本轮未重新验证它们的全部现行功能，因此没有重写一份声称覆盖全领域的能力排行榜。

后续最关键的源码核对对象是 ETAS 的受检 IR 与恢复实现、LBAC 的生成程序接口，以及两项守恒协议的可执行 artifact。当前已有证据足以修正新颖性定位，尚不足以宣称 AL-0 已在实现能力上超过这些近邻。
