# 固定提交审计修复与组合验收

本轮处理针对 `08a32b08b0c1af7381e1764d59d513b8c2755da3` 的七项审计发现。范围是信息流出口、派发期限、任务代次、执行位置、运行结束责任、SDK 构造等价性及安装验收。ContextView 仍停留在设计与有限模型阶段。

## 修复与验证对应

| 发现 | 实现变化 | 组合回归 |
|---|---|---|
| F1：任务标签漏掉间接操作来源，handoff 暴露内部快照 | `plan_label` 对共享检查器的节点类型递归收集 Operation、OpRef、Snapshot 及 Bundle/Variant 成员的工具输出上界；handoff 返回带标签的控制回执，合并任务上界与实际结果标签 | Operation 参数经过 await/match_business；Completed、Failed、Waiting、Cancelled、handoff、race 六条出口分别拒绝 public、允许 owner；嵌套资源接口及 OpRef |
| F2：已入队 retry 越过端点期限后仍能派发 | 两种派发 API 在写入 DispatchStarted 的同一事务内重新检查 `now < first_started + retry_window`，边界相等即拒绝 | 两种 API × 截止前/恰好截止/截止后 × 重启/不重启；独立端点数据库清除过期键，业务效果始终一次 |
| F3：旧父工作线程可通过 handoff/异常退出取消新代次子树 | TaskScope 各退出路径传递 parent lease；Store 对 join、handoff、close、已有 scope/spawn 的恢复检查同一控制 actor 和当前 lease；另设可信宿主 supervise 接口 | epoch 1 被 epoch 2 替换后，handoff、异常清理、已有 scope/spawn 恢复均拒绝且不修改子任务；当前父 lease 和显式监督仍可清理 |
| F4：任意根节点名可碰撞嵌套位置编码 | 所有根及嵌套节点统一使用 `site:` + `digest(['site', path, node])` | 根节点名等于旧 branch 键时产生两个操作；同内容不同位置各调用一次，恢复不再调用；site 风格名称仍作为普通输入编码 |
| F5：finish 阻止已有队列，与责任语义不符 | 派发已有 attempt 不再检查“新工作”门槛；admit/retry 保留该门槛以及注册对账例外 | Completed/Failed × 两种派发 API × 正常/取消/撤权；已有派发允许，新 admission/retry 拒绝，迟到费用按原 attempt 结算 |
| F6：Bundle Cap 连续衰减被 SDK 错拒 | `_cap_spec` 使用共享 IR 的 `_type_of`，不再自行追溯少数节点来源 | Cap、参数/invoke、Bundle、Variant、分支、任务 join 六种来源；连续衰减与独立追加的 IR 完全相同，运行时次数及字段限制有效，恢复结果相同 |
| F7：wheel 门槛 resolve 解释器导致离开符号链接 venv | 解释器路径仅 absolute，不解引用符号链接；wheel 路径仍 resolve | 可移植路径回归；Linux CI 分别创建 `--symlinks` 和 `--copies` venv 并检查实际模式，再在源码外运行已安装消费者 |

专项测试位于 `tests/test_audit_regressions.py` 和 `tests/test_audit_combinations.py`。矩阵的多种情形使用 subTest，不能把情形数与 unittest 方法数相加报告。

### handoff 的语言结果

`TaskScope.handoff` 及语言 handoff 节点现在产生 `Value({'task': ..., 'state': ..., 'disposition': ...}, label=...)`。内部结果、预算、资源记录不进入此普通数据回执。可信 Store 的 `handoff_task` 仍返回内部快照，供宿主检查；它不是受检语言出口。

保守上界也可能限制只返回 Operation、尚未读取工具结果的任务。若父计划将在 join 后派发该操作，应在准备 Intent 前声明相应控制标签；先准备较弱 Intent，再通过任务返回引入更强依赖的旧写法会被拒绝。资源交接回归同时验证旧写法不调用适配器、预先声明标签的写法正常执行并恢复。

### 派发与计费期限

期限拒绝抛出 ContractError，包括受检派发 API；它不被误分类为授权或流策略阻止。拒绝不产生派发权、不调用端点，retry 仍为 Queued，预留仍保留。显式 cancel 可释放该未派发 retry 的预留，但首次已暴露 attempt 的预留及迟到收费责任仍存在。

此检查保证本地授予派发权时未过期。授予后网络延迟可能跨过远端去重期限；适配器仍须依赖真实端点的接收期限/持久去重契约，或避免该类重发。模拟端点结果不证明真实供应商满足这些条件。

### 工作线程与监督权限

嵌套作用域的宿主调用必须传当前 `parent_lease`。不带 lease 不再隐式获得嵌套子树控制权。`supervise_task`、`supervise_scope` 为可信宿主显式取消入口，拒绝在 `resource_access` 工作线程上下文内使用；TaskScope 不在 lease 失败后回退为监督操作。

历史结果和已冻结回执可只读查询；产生新控制变更或重新调度已有子任务须验证当前 actor。Python 宿主、调度器和数据库仍属于可信基，这不是针对恶意 Python 插件的隔离机制。

## 版本与旧记录

数据库 schema 仍为 21。`al0/semantics.py` 独立声明 `FLOW_SEMANTICS=2`、`EXECUTION_SEMANTICS=2`，两者加入 Catalog 指纹，进而改变 CheckedPlan 指纹。原始 IR 文本可以重新检查为新计划，但旧 run/task 的程序指纹不能由此自动更新。

新解释器拒绝以新计划恢复旧 run，也拒绝通过 TaskRunner 返回旧已完成任务的缓存结果。回归使用审计基线的精确 Catalog 编码（flow_semantics=1、无 execution_semantics 字段），检查拒绝前后事件不变。仅升级表结构不证明旧公开标签正确，也不授权把旧节点键转换成新位置。

本轮不提供自动语义迁移。原账本可查询；可信宿主可显式监督取消、保存已暴露责任并接受迟到费用。需要继续业务流程时，应制定保留已有 operation/attempt 责任的显式迁移方案，不能简单新建 run 并重发可能已生效的动作。生成的 Python Catalog 绑定也须重新生成。

## 复现与证据

在原提交上先运行八个针对性测试，得到六项断言失败和两项异常，覆盖七个发现（F3 有两条路径）。这是缺陷复现日志，不能当验收成功：`models/audit_08a32b0_reproduction.log`。F7 的本地测试模拟路径解引用，不冒充实际 POSIX 虚拟环境验收。

[GitHub Actions 运行 40240346716](https://github.com/StephCpa/AgentLanguage/actions/runs/40240346716) 对提交 `62c49e7d0fe56bf31991d6e6d1a7d8e40dbfcc0b` 的四个 job 全部通过。Python 3.10.22 完成 950 项测试，用时 124.939 秒；Python 3.12.15 同样完成 950 项，用时 132.847 秒，两者均无跳过且启用 mypy 1.18.2。Linux symlinks/copies 两种实际 venv 均通过源码外已安装 wheel 消费者验收。结果与日志摘录保存在 `models/audit_ci_acceptance.json`，完整日志及类型报告可从该运行的 artifacts 取得。

本机还通过 170 项边界回归（`models/audit_boundary_regression.log`）、新增 SDK/生命周期矩阵（`models/audit_sdk_lifecycle.log`）、123 项兼容回归（423.820 秒，`models/audit_compatibility_regression.log`）及重建 wheel 消费者（`models/audit_wheel.log`）。首次 Linux 全量发现四处旧测试与新语义不一致：三个直接注入/查询仍使用旧节点键，一个未提前声明任务返回带来的控制依赖；均已修正，并保留低标签 Intent 被拒绝的负例。最终验收采用修正后的完整运行，不将首次失败日志计为通过。

```powershell
python -m unittest tests.test_audit_regressions tests.test_audit_combinations -q
$env:AL0_TYPING_PYTHON='.venv-typing-pure/Scripts/python.exe'
$env:AL0_TYPING_REPORT='models/sdk_type_audit.json'
python -m unittest discover -q
python -m pip wheel --no-deps . --wheel-dir dist/typing-audit
python -m scripts.check_installed_typing --python .venv-typing-pure/Scripts/python.exe --wheel dist/typing-audit/al0_reference-0.1.0-py3-none-any.whl
```

类型工具环境须预先安装 `mypy==1.18.2`。CI 位于 `.github/workflows/ci.yml`：Ubuntu Python 3.10/3.12 跑完整回归并强制类型验收，另两个 Python 3.12 job 分别验证 symlinks/copies venv 的已安装 wheel 消费者。日志及类型报告作为各 job artifact 保存；管道启用 pipefail，不能由 tee 掩盖失败。此流程不发布到包索引。

修复和测试支持这七项反例及所列组合的收敛；完整形式证明、真实适配器契约验证及 AL-0 对强 SDK 的配对实验仍是后续工作。
