# AL-0 源码前端与共享 IR

本轮为已实现的 IR 增加独立源码入口。`.al0` 文件经过词法分析、带位置的 AST、名称解析和静态展开，交给原有 `check_plan`，再生成同一 `CheckedPlan`。Python SDK 和源码共用类型、效应、证据、任务消费及推测写入规则；源码不另设权限通道。更新日期为 2026 年 10 月 25 日。

这是当前可执行核心的表面语法。完整语言草案中的嵌套表达式、一般能力借用和跨任务/循环资源传递仍需扩充语义。它们没有被删出目标，也不能通过当前解析器隐式调用宿主代码。

后续已加入 [纯表达式与持久条件分支](16-pure-and-conditional.md)，以及 [fn、call 与 repeat](17-functions-loops-and-fuel.md)。嵌套表达式与一般资源控制语法仍在后续范围内。

## 基本程序

```text
workflow repair-example() -> Text
uses {"infer:code-model", "check:patch-tests",
      "authorize:repo.merge@repo", "admit:repo.merge@repo"} {
    let request: Text = "Fix the off-by-one error at base version 7.";
    let draft = infer("code-model", request);
    let tested = check("patch-tests", draft);
    let intent = prepare("repo.merge", tested);
    let permit = authorize(intent);
    let operation = admit(intent, permit);
    let status = await(operation);
    let result: Text = match_business(status, {
        NotDispatched: "Waiting", Pending: "Waiting",
        Unknown: "NeedsReconciliation", Applied: "Done",
        ProvenNoEffect: "NeedsRevision"
    });
    return result;
}
```

该程序保存在 `examples/repair.al0`，与 `repair.ir.json` 和 `repair_sdk.py` 的规范化 IR、程序指纹相同。`uses` 是显式边界，不会因程序调用了某个工具就被自动扩大。执行时宿主仍必须绑定实际 grant，声明效应不授予权限。

变量名成为稳定 IR 节点身份，workflow 名成为计划名。注释、空白、换行方式和源文件路径不进入计划指纹；修改数据常量、节点名、计划名、效应、标签或工具目录会改变相应执行身份。前端不承诺重命名节点后仍能在原 run 中继续执行。

## 语法约定

```text
module     := (workflow | function)+
function   := "fn" name "(" parameters? ")" ("->" type)?
              ("uses" "{" "}")?
              "{" statement* "return" name ";" "}"
workflow   := "workflow" name "(" parameters? ")"
              ("->" type)? "uses" "{" effects? "}"
              "{" statement* "return" name ";" "}"
parameter  := name ":" type
statement  := "let" name (":" type)? "=" expression ";"
expression := data | builtin "(" arguments ")"
type       := schema
            | Data<schema> | Candidate<schema> | Observation<schema>
            | Checked<schema, predicate, ...> | Operation<"tool-name">
            | Intent<"tool-name"> | Permit<intent_parameter> | OpRef<"tool-name">
            | Admission<"tool-name"> | Cap<"tool-name">
            | Budget
```

根入口与纯函数输入接收 Data、Candidate、Observation、Checked；任务输入和结果另支持 Cap。分支与 invoke 可声明 Intent、Permit、Operation、OpRef、Budget 和 Cap 参数，资源绑定由受检父调用建立；其中 Permit 的实参引用更早的 Intent 参数。Operation、OpRef、Budget 和 Cap 可用于结果注解，Intent 和 Permit 的一般返回仍未开放。普通数据 schema 如 Text、Int、Bool、Json 或可信目录中的 Patch，等价于 Data<schema>。Checked 输入在运行时仍核对真实、同运行且绑定到数据的回执。资源规则见 [分支资源传递](18-resource-branches.md)、[Budget 工作流调用](20-budget-workflow-calls.md)、[Cap 权限衰减](21-capabilities-and-attenuation.md) 和 [跨任务 Cap](23-task-capability-transfer.md)。

字面量和 `match_business` 必须声明数据 schema；其他绑定可以省略注解，由共享检查器推导。可选注解和 workflow 返回类型是断言，不能把 Candidate 转成 Data 或把 JSON 转成 Permit。Checked 注解可以要求推导值已有的谓词子集，不会消除额外证据。

标识符使用 ASCII 字母或下划线开头，后续允许字母、数字、下划线和连字符。工具名、效应名使用双引号字符串。字符串及转义遵循 JSON；数据域只含有界整数、字符串、布尔值、null、列表和记录。记录键可省略双引号，列表与映射允许尾逗号；重复键拒绝。负整数可作为普通数据，但预算向量仍只允许非负整数。`//` 是行注释；字符串里的 `//` 是数据。

每个 workflow 是 SSA 绑定序列，以一个 `return name;` 结束。不支持赋值、隐式字段提取、别名表达式、嵌套调用表达式或任意用户函数调用。中间值须显式命名，这也使持久节点身份可审查。

## 当前操作表

以下 `value`、`task`、`scope` 等均是先前定义的值名；tool 是字符串；label 是数据字面量，budget 可以是资源字面量或先前向量值的引用。

| 源码表达式 | IR 行为 |
| --- | --- |
| `infer(tool, value)` / `observe(tool, value)` / `check(tool, value)` | 受介导调用；保留 Candidate、Observation、Checked 区分 |
| `restrict(value, {readers: [...], taints: [...]})` | 单调合并标签；公开 readers 写 null |
| `add(a,b)`、`eq(a,b)` 等前缀纯运算 | 降为封闭 compute，按操作校验输入并推导类型 |
| `field(value,"field")` / `record("Schema",{field: value})` | 按已声明记录 schema 读取或构造，不保留原 artifact 回执 |
| `resources({resource: value})` | 从 Int 值构造普通 Json 资源向量，保留来源种类与标签；详见 [动态预算](22-dynamic-budget-vectors.md) |
| `if(condition, then_workflow, else_workflow, {parameter: value})` | Bool 分支、显式捕获及持久首次选择 |
| `borrow_operation(operation)` | 建立可复制的只读 OpRef；不移走 Operation 处理责任 |
| `try_admit(intent, permit)` | 消费许可并产生持久 Admission；可加 Admission 类型注解 |
| `split_budget(vector)` / `split_budget_from(parent, vector)` | 从当前资金或指定 Budget 划出子预算 |
| `join_budget(funds)` | 消费 Budget 并归还当前可用份额 |
| `invoke(workflow, inputs)` / `invoke_with(workflow, funds, inputs)` | 持久工作流调用、资源参数和显式资金来源；详见 [预算与调用](20-budget-workflow-calls.md) |
| `capability(tool)` / `attenuate(cap, options)` | 绑定当前工具能力、派生期限/次数/字段等值约束更窄的能力 |
| `authorize_with(intent, cap)` | 使用显式 Cap 授权，工具与 Intent 必须一致 |
| `invoke_caps(workflow, caps, inputs)` / `invoke_caps_with(workflow, funds, caps, inputs)` | 限制环境工具表与显式保护实参；详见 [受限调用](21-capabilities-and-attenuation.md) |
| `match_admission(value, {Admitted: branch, Returned: branch, NoPermit: branch}, captures)` | 穷尽匹配接纳结果，结果引用恰好消费一次；详见 [许可返还](19-admission-outcomes.md) |
| `call(function, {parameter: value})` | 调用明确声明的纯函数，共享执行额度 |
| `repeat(count, limit, initial, body, {parameter: value})` | 运行时有界状态循环；带效应时保存进度 |
| `prepare(tool, value)` | 构造不可变 Intent |
| `authorize(intent)` | 使用当前调用权限表中的 grant 请求许可 |
| `admit(intent, permit)` | 消费许可并接纳操作 |
| `inspect(operation)` / `await(operation)` | 本地观察或受介导派发/观察 |
| `reconcile(tool, operation)` | 独立授权、计费和出口检查的查询 |
| `match_business(snapshot, {Applied: data, ...})` | 五种业务状态穷尽匹配 |
| `scope(budget, speculative_bool)` | 创建持久作用域及预算分配 |
| `spawn(scope, child_workflow, budget)` | 创建无输入子计划 |
| `spawn_with(scope, child_workflow, budget, {parameter: value})` | 显式传递数据、证据或 Cap，固定继承的权限表 |
| `spawn_caps(scope, child_workflow, budget, caps, inputs)` | 显式权限表及 Cap 输入/返回；详见 [任务权限](23-task-capability-transfer.md) |
| `join(task)` / `handoff(task)` | 消费任务控制权 |
| `join_outcome(task)` / `cancel_outcome(task)` | 固定带标签的任务结果观察 |
| `match_task(outcome, {Completed: branch, ...}, {parameter: value})` | 四种结果分支及显式捕获 |
| `race([task_a, task_b, ...])` | 同一推测作用域内的持久选择 |
| `close_scope(scope)` | 消费已处理所有子任务的作用域 |

源码不能安装工具、checker 回调、Python 模块或权限对象。所有调用名来自封闭操作表。字符串始终是数据，不能因为内容像 Python 或 AL-0 就在执行时变为代码。

## 模块、任务与分支

一个文件可声明多个 workflow，允许前向引用。值引用只能指向当前 workflow 中更早的参数或绑定；子 workflow 不隐式捕获父局部变量。`spawn_with` 和 `match_task` 的捕获必须列出。TaskOutcome 分支的保留参数 `result` 由运行时绑定，不能被捕获映射覆盖。

```text
workflow check-child(patch: Candidate<Patch>) -> Checked<Patch, TestsPassed>
uses {"check:patch-tests"} {
    let tested = check("patch-tests", patch);
    return tested;
}
```

完整任务结果程序见 `examples/task_outcomes.al0`。成功分支接收 Checked 结果并修改模拟仓库；失败与等待分支分别返回 NeedsRevision 和 NeedsReconciliation。源码与对应 SDK 程序生成完全相同的嵌套 IR。

workflow 引用按静态计划展开，尚无跨文件模块导入和递归调用。所有声明都会检查，包括未选作入口的声明。入口选择顺序为：显式 entry；单 workflow；名为 main 的 workflow。多个 workflow 且没有 main 时要求调用者选择入口，不猜测最后一个声明。

编译子计划时保留推测上下文；嵌套工具写入不能利用独立声明绕过 speculative 规则。参数和效应仍由共享检查器验证。展开过程按实例累计节点数、嵌入计划字节数和深度，超过 IR 限制时提前拒绝，避免有向无环引用图的重复展开造成指数级中间对象。

## 诊断与源码映射

诊断包含 `code`、`message`、`span`、`hint` 和 `related`。span 是源路径、半开字符范围 `[start,end)`、起止行列；行列从 1 开始，按 Unicode 码点计数，tab 计作一个字符而非视觉列宽。未知值定位到引用位置，重复定义关联原定义，证据或仿射错误定位到当前操作并关联其输入定义。不能归属于单个节点的整体检查错误定位到 workflow。

词法/语法错误为 AL8001，名称解析、入口与展开错误为 AL8002，CLI 文件错误为 AL8003。共享检查器的 AL6001、AL1001、AL3002 等错误码保留。当前每次编译报告第一个错误，尚未做多错误恢复或编辑器增量解析。

错误消息不附带字符串字面量、源码片段或宿主凭据。source map 只保存文件和定义范围，不拷贝数据常量。调用者若需要在界面中展示源码，应独立控制该文件的访问权限。

source map 与计划分开保存，按 workflow 名和节点名定位；它不赋予能力、不进入计划指纹。当前没有自动把所有运行时异常、任务动态 activation 和分支恢复轨迹映射回源码的调试器。该工作仍需结合持久任务及分支路径完成。

## API 与命令行

```python
compiled = compile_source(text, trusted_catalog,
                          filename="repair.al0", entry="repair-example")
result = Interpreter(broker).run(compiled.plan, run, principal, grants)
```

`compiled.workflows` 保存各声明的已检查计划，`compiled.source_map` 保存诊断位置。参数数据仍通过 Interpreter 的 inputs 绑定。源码编译只读取提供的文本与可信目录，不创建 run、不调用适配器、不生成 grant。

```powershell
python -m al0.frontend examples/repair.al0 --demo-catalog
python -m al0.frontend examples/task_outcomes.al0 --demo-catalog --entry task-outcomes
python -m al0.frontend examples/repair.al0 --demo-catalog --emit-ir plan.json --source-map plan.map.json --json
python -m examples.frontend_demo
```

`--demo-catalog` 只使用本地修复示例的目录。真实宿主可提供 `--catalog catalog.json`，格式为：

```json
{
  "catalog_version": 1,
  "schemas": {"Row": {"name": "Text"}},
  "tools": [{
    "name": "records.read", "kind": "observe",
    "input_type": "Text", "output_type": "Row", "scope": "records",
    "upper_bound": {"units": 1},
    "recipients": [],
    "output_label": {"readers": ["owner"], "taints": ["database"]}
  }]
}
```

schema 按依赖顺序声明；reconciler 在它指向的工具之后声明。工具字段对应 Tool.define，拒绝未知或可执行回调字段。该目录由可信宿主选择；接受一个目录文件用于静态检查，不等于授权其中工具运行。

输入文件为 UTF-8，可带 BOM，最多 1 MiB；词法 token 最多 100000，模块绑定和展开后的 IR 均受节点上限约束，数据和 IR 嵌套继续受共享限制。输出文件不能与源码、目录或彼此使用同一路径。编译失败不输出新 IR；CLI 返回非零状态。`--json` 输出机器可读成功记录或诊断。

## 当前证据与剩余工作

前端专项测试覆盖所有现有 IR 操作、三种输入形式的 IR 等价、源码位置、SSA/affine/effect/evidence 拒绝、递归与重复展开限制、受限目录加载、CLI 输出和运行时标签拒绝。任务结果场景分别验证成功、检查失败和等待，且可从源码执行切换到 SDK 恢复，事件不增加。

`python -m examples.frontend_demo` 的实际结果为 Done；源码与 SDK 的 IR 相同；恢复新增事件为 0；确认费用为 4、预留为 0，模拟业务修改一次。这是共享语义和恢复兼容性的证据，不是语言开发效率优于 SDK 的实验结果。测试总数以 [实现状态](08-implementation-status.md) 为准。

后续 M4 工作仍包括资源控制语法、嵌套表达式、完整源码诊断与运行时位置映射；M5/M6 仍需组合语义验证和同运行时开发实验。当前前端提供了执行与对照入口，尚不能替代这些验收。
