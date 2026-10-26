# Unicode 引用与可恢复的实验入口

本轮处理固定提交 `e62ae2f5b8fc1069b7c8e88313c2839bb49ea46b` 的复验意见。R2、R3 和 inspect 报告问题不重开；R1 的原 ASCII 备份反例已修复，本轮补齐 Unicode 和原始字节边界。历史六次模型输出及其评分没有被替换，没有新增模型调用。

## Git 查询按字节解释

`git ls-remote` 查询现在直接捕获 bytes，只按 LF 分记录、TAB 分字段。引用名按 [Git 的字节规则](https://git-scm.com/docs/git-check-ref-format) 检查；不使用 Unicode `isspace()`、`splitlines()` 或替换式解码。筛选时把已知目标引用编码成 UTF-8，做精确字节比较。只有匹配目标且 OID 正确才产生 Applied；不相关备份保持原始身份，包括非 UTF-8 字节名称。

真实 Git 回归分别覆盖 U+00A0 和 U+2028 备份：精确目标存在时均可对账结算，只有备份时均保持无确认。既有错误 OID 和重复记录拒绝继续保留。另有纯解析器测试检查非 UTF-8 名称不会被替换，以及 ASCII 禁止字符、路径组件和重复记录的拒绝。这些是针对已报告边界的验收，不声称穷尽所有 Git 环境。

## 保留精确多重集口径

本轮选择保留既有实现：`business_goal_completed` 比较目标 commit 的精确多重集，包含任务规定的出现次数。重复发布同一 commit 两次，即使两个操作都 Applied，也未完成“只发布一次”的业务目标；此时返回 Published 会令 `false_success_claim=true`。这不是纯 distinct commit 存在性指标。[上一轮说明](54-audit-r1-r3-and-experiment-gates.md) 已据此修正文案，崩溃后重复发布的负例也断言三个结果为 false/false/true。Unknown 补测的历史结果不变。

## 同一请求可以追加受控评估

新 journal 在初始化时显式选择 `source-or-single-fence-v1` 和 `max_evaluation_attempts`，不会把老 journal 自动迁移或重新解释。

每个 `evaluation_attempt` 有自己的 UUID 和序号，绑定请求、前端、原始输出摘要、提取源码摘要、解析器版本、任务契约和 Catalog。首次评估与后续基础设施重评都保存在追加记录中；已完成结果不可覆盖。`rounds.evaluation` 仅作为最新完成评估的进度视图，旧故障始终保留在 evaluation_attempts 中。

只有最新完成结果属于 infrastructure_failure，且尚未达到预定总评估次数，才可显式重评。`max_evaluation_attempts=2` 表示首次加至多一次重评。源码、格式或业务错误不能借此获得免费重试。正在评估时重复进入只返回原身份；有完整落盘报告时可导入恢复，没有完整报告时停止并要求检查进程，绝不自动重复外部副作用。

基础设施故障期间不得新申请模型修复；重评解决故障后，由有效结果决定结束或允许下一次有计数、有预算的模型修复。模型请求次数、原始响应和 token 费用不会因重评改变。远程结果导入也要求显式 evaluation_id，并继续核对源码/契约/Catalog 与 Docker 执行来源。

## 共同解析器和教学材料进入同一请求链

`scripts.next_pilot` 的初始化会实际编译 `teaching_v2` 的所有注册例子，并检查两前端 IR 相同，然后把对应源码追加到两组材料，保存到不可变配置。共享说明和 system 提示都使用同一围栏规则。评估读取保存的响应，通过同一解析器保存原始/提取文本和摘要，再执行前端；混合说明、多块或空输出被记录为格式失败，不启动构造器。

v2 导出带独立版本；旧 pilot scorer 拒绝把它按历史原始源码协议评分。旧 journal、旧提示和六次捕获记录继续使用旧协议。

发送仍复用现有固定 DeepSeek 传输器。新模式会先检查冻结状态、明确的两臂调用顺序以及前一输出/评估是否已处理；成功的臂跳过余下修复位置，不把这些位置转给另一臂。这里接通了执行流程，不代表独立任务集已完成研究冻结。

## 共享费用责任不会随新 journal 重置

独立 SQLite 账本保存授权 ID、总上限和每个请求的未知费用预留或已知账单。新 journal 必须绑定同一账本身份；请求创建和共享费用预约在一个 ATTACH 事务中完成。两数据库要求使用 rollback journal，避免在 WAL 配置下误称多文件事务原子提交。

额度检查跨任务、跨前端执行。收到合法 usage 后，保守上界收窄会同步到共享账本；收到实际账单则同步已知费用，不能把估计写成付款额。超过单请求上界的实际费用如实保留并阻止后续请求。并发测试验证两个 journal 不能同时预约超出总额。

实际本机账本 `.git/experiment-budget-v1.sqlite` 已从原 `.git/deepseek-pilot-v1/journal` 导入六项责任：总授权仍为 1.00 美元，保守占用 0.016987800，剩余 0.983012200，实际总账单未知。新真实提供方入口要求账本包含归档的全部六个请求，且上限不能超过原授权；空账本不能被误用于新任务。

导入后若历史 receipt/费用变化，预约先阻断。显式 `reconcile-history` 可以追加同步记录、导入合法的晚到账单或上界收窄，不改变账本 ID、不删除旧责任，也不接受悄悄增加的历史请求。新 journal 不能覆盖或重建已有账本文件。

## 使用与停止规则

账本仅首次创建一次；本机已有该文件，不要重新运行 init：

```sh
python -m scripts.shared_budget --ledger .git/experiment-budget-v1.sqlite init \
  --total-usd 1.00 --legacy-journal .git/deepseek-pilot-v1/journal
python -m scripts.shared_budget --ledger .git/experiment-budget-v1.sqlite inspect
```

每个任务计划必须包含 status、contract_digest、source_protocol、model_attempts_per_arm、max_evaluation_attempts 和 call_order。进入 frozen 状态还要求 task_set_id、independence_basis、order_basis；这些是研究者事前说明，不是自动证明任务独立。真实任务必须采用经校准、冻结的包和相同构造环境。

```sh
python -m scripts.next_pilot --pilot TASK_SESSION init --package TASK_PACKAGE \
  --ledger .git/experiment-budget-v1.sqlite --plan TASK_PLAN.json --builder-image BUILDER_IMAGE
python -m scripts.next_pilot --pilot TASK_SESSION evaluate --request-id REQUEST_ID \
  --builder-image BUILDER_IMAGE --retry-infrastructure
```

本机已建立 preflight 状态的实际入口，0 个新请求；其发送尝试在读取凭据或发起网络调用前被未冻结门禁拒绝。独立任务集及其负例、任务级平衡顺序、最终材料和模型/价格配置仍需在实际研究调用前固定。当前 v1 提供方配置仅用于流程校准，不能把复用配置当成新实验已完成冻结或新的价格核验。

`scripts.check_prospective_journal` 在 CI 用标明为 fixture 的参考源码验证完整流程：不存在的 Docker 镜像使首评失败、禁止新请求、相同源码使用可用镜像重评成功；两臂共 2 个模拟请求、4 个评估身份，0 次模型调用。单元测试另覆盖合成编译器故障、评估次数上限、待完成评估不重复执行、不可变结果、格式拒绝、共同材料/传输路径和并发预算。
