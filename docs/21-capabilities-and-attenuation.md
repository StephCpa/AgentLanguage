# Cap 权限衰减与受限工作流调用

更新日期为 2026 年 10 月 25 日。当前实现把工具授权接入受检资源类型，并让权限衰减、调用边界、撤销和恢复共用持久授权链。它实现了原草案 `Cap<K,S>` 中的工具能力子集：源码类型为 `Cap<"tool">`，范围取自可信工具目录。

## 类型和接口

Cap 是可复制的授权引用。复制不会增加权限，也不会复制预算。Permit 仍是绑定具体不可变 Intent 的单次接纳权；Operation 承担已接纳工作的处理责任。Cap、Permit 和 Operation 的职责不能互相替代。

| 表面语法 | 含义 |
| --- | --- |
| `capability("send")` | 从当前调用的权限表绑定 send 的 Cap |
| `attenuate(cap, {max_attempts: 1, constraints: {channel: "reports"}})` | 派生更窄的工具权限 |
| `authorize_with(intent, cap)` | 使用显式 Cap 为该 Intent 请求 Permit |
| `invoke_caps(deliver, {send: reports}, {message: message})` | 用明确的权限表调用工作流 |
| `invoke_caps_with(deliver, funds, {send: reports}, inputs)` | 同时指定资金借用和权限边界 |

SDK 对应 `cap_parameter`、`capability`、`attenuate`、`authorize_with` 和 `invoke_caps(..., budget=...)`。取得和衰减能力要求 `cap:tool@scope` 效应，授权仍要求 `authorize:tool@scope`，接纳仍要求 `admit:tool@scope`。声明效应只描述请求范围，运行时还须持有有效授权。

Cap 可以作为受检分支、同步工作流调用及子任务的参数或返回值。根输入不能通过 `Value("grant-id")` 伪造 Cap，普通 JSON、记录投影和纯函数不能生成它。后续的 [跨任务 Cap 协议](23-task-capability-transfer.md) 为 spawn 固定权限表，并提供显式 spawn_caps 边界。

可运行的 [源码示例](../examples/capability_calls.al0) 与 [SDK 示例](../examples/capability_calls.py) 生成相同 IR。主流程取得 send 能力，将目标 channel 限为 reports、每个逻辑操作的最大尝试次数限为 1，再调用 deliver。deliver 接收显式 Cap，执行 prepare、authorize_with、admit 和业务结果匹配。

## 单调衰减

派生授权保留父授权的 tool 和 scope，只允许以下变化：

- `expires`：绝对期限，只能保持或缩短，创建时必须仍有效。
- `max_attempts`：正整数，只能保持或降低。
- `constraints`：工具输入的顶层字段等值约束，可以添加，也可以重复已有的相同约束。

省略期限或尝试上限会继承父值。省略祖先已有的字段约束不会删除它；授权检查沿整条父链验证约束。新约束若与任一祖先冲突，则拒绝衰减。相等使用规范化 JSON 比较，因此布尔值 true 与整数 1 不相等。

受检 IR 对记录输入校验字段存在及值的 schema；Json 输入允许任意合法字段名。非记录输入不能添加非空字段约束。当前不支持嵌套字段路径、任意可执行谓词、运行时计算约束或改变工具范围的字符串规则。

`max_attempts` 约束每个 Permit 所对应的逻辑操作，不是整棵授权树的总调用配额。持有同一个 Cap 可以为不同 Intent 请求多个 Permit；它们仍须分别通过预算接纳。跨操作的资源总量由 Budget 和账本控制。

## 受限调用边界

`invoke_caps` 的权限表完整替换 callee 的环境权限表；空表表示没有环境工具权限。未列出的工具不会回退到宿主更宽的授权。callee 中的普通 invoke、分支、循环及新建任务继承这一权限表。infer、observe、check 和 reconcile 的默认授权路径也使用当前表。

只替换环境权限还不够：显式传入的保护引用也可能携带权限。调用前按参数类型检查：

| 参数 | 边界检查 |
| --- | --- |
| Cap | 它必须是该工具边界 Cap 本身或其派生后代 |
| Permit | 它绑定的 grant 必须满足同样的后代关系 |
| Operation | 经该操作的 Permit 找到 grant，再验证后代关系 |
| Intent、OpRef、Budget、数据与检查证据 | 不携带可用于授权或派发的工具权限，沿各自原有规则检查 |

因此，即使父程序持有更宽的 Cap、已经授权的 Permit 或已经接纳的 Operation，也不能把它们作为参数带入更窄的调用。结构上无父子关系的兄弟授权，即便碰巧有相同约束，也不能替代后代证明。规则明确且可判定，暂不做一般策略蕴含求解。

已经由调用者接纳的 Operation 若在调用边界被拒绝，其原预留仍然存在；拒绝调用不会抹去既有责任。受限 callee 通过静态效应检查也不代表所有可能路径都有足够权限，缺失或过期的能力可以在运行时被拒绝。

## 当前授权与历史恢复

设 `ancestors(g)` 包含 g 及其所有祖先。对新的授权、接纳、派发和重试，运行时要求：

```text
∀ a ∈ ancestors(g): active(a) ∧ now < expires(a)
∀ child → parent: recorded_parent_epoch(child) = epoch(parent)
∀ a ∈ ancestors(g): arguments satisfy constraints(a)
tool(g) = tool(intent) ∧ scope(g) = scope(intent)
```

接纳和派发还验证 Permit 记录的叶节点 epoch。父授权撤销会使所有后代不能开始新工作；撤销一个子授权不撤销父或兄弟。即使宿主未来重新启用父记录，旧派生链也会因 epoch 不匹配而失效。当前没有面向 AL-0 程序的重新启用接口。

这些检查在各自的 SQLite 事务内与撤销排序。派发提交已经赢得顺序后，撤销不能召回已获派发权的请求，也不能阻止其后到达的业务或费用证据入账。撤销排队工作的权限同样不等于自动释放预留，仍需遵循取消与责任处理协议。

恢复旧 binding、旧派生记录、已完成操作时允许取得原来的历史引用。引用恢复不重新授予权限：接下来如果发生新的接纳或派发，仍会复核整条当前授权链。调用参数的后代关系检查只判断结构，避免把读取已完成工作的历史误当成新授权；实际工具操作负责检查活性。

## 持久身份与标签

schema 13 新增两个表：

- `capability_bindings`：键为 `(run, activation, node)`，保存工具、首次绑定的 grant 和标签。
- `grant_derivations`：保存新 grant、父 grant、父 epoch、稳定派生键、衰减请求及标签。

派生 grant 和其父链记录在同一事务创建。相同键恢复相同 grant；用同一键换父授权、期限、次数或约束会报 ReplayMismatch。并发创建同一派生位置只得到一个子授权。解释器的键包括任务身份与完整调用路径，授权链最多含 64 个节点。

已有 invocation_frames 的内容增加显式 caps 表。第一次调用冻结实参、资金身份、控制标签及权限表；重放更换权限表会报 ReplayMismatch。普通 invoke 保持原来的继承行为。

Cap 绑定、派生和恢复保留信息流标签；派生不会降低父 Cap 的标签。显式授权和受限调用把 Cap 的标签加入控制上下文。若 Intent 在更宽松的控制上下文中准备，随后使用受限标签的 Cap 授权，原有 intent_context 检查可以拒绝它；取得能力不构成解密或完整性提升。

v12 到 v13 只新增持久表，保留旧 grant、许可、账本和调用帧身份。已有合法 IR 的指纹不变，新构造参与新计划指纹。具备 Python 或数据库写入权限的宿主仍属于可信基；本实现没有把恶意宿主插件变成受检 AL-0 程序。

## 验证与剩余工作

```powershell
python -m examples.capability_calls
python -m unittest tests.test_capabilities -q
```

示例结果为 Done，外部发送一次，确认费用为 1、预留为 0；一次能力绑定和一次衰减，恢复新增事件为 0。41 项专项测试覆盖衰减、继承约束、撤销及 epoch、各操作阶段的复核、显式参数越权、非动作工具、嵌套调用与任务、Cap 分支/返回、标签、迁移和源码等价。六组独立进程退出覆盖绑定、派生和调用帧提交前后；恢复后不重复授权记录或外部动作。全量结果见 [实现状态](08-implementation-status.md)。

`python models/capability_audit.py` 枚举授权、接纳、派发、撤销、恢复及迟到收费的有限切片。正确变体为 60 状态、120 转移；仅检查叶节点的变体产生四步祖先撤销反例，仅替换环境权限表的变体产生五步越界派发反例。状态边界与可进展控制见 [模型结果](06-model-results.md)。

尚需研究一般能力种类、结构化 scope、动态约束表达、仿射句柄跨任务移动、循环资源状态，以及与资源和信息流组合的形式化对应。当前测试建立可执行证据，尚不能推导完整非干扰、强活性或语言相对 SDK 的开发收益。
