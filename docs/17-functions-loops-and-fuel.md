# 纯函数、有界循环与共享执行额度

更新日期为 2026 年 10 月 25 日。本轮加入纯函数声明和调用、按运行时次数执行的有界循环、循环进度恢复，以及跨函数和任务共享的解释器步数额度。循环体既可纯计算，也可在声明边界内调用工具。

## 纯函数

```text
fn accumulate(value: Int, delta: Int) -> Int {
    let next = add(value, delta);
    return next;
}

workflow main() uses {} {
    let value: Int = 7;
    let delta: Int = 2;
    let result = call(accumulate, {value: value, delta: delta});
    return result;
}
```

fn 使用相同参数类型、schema、标签和证据规则。IR 函数计划含 `pure: true`；函数体只允许 parameter、literal、restrict、compute、field、record、if、call 和 repeat。声明非空外部效应，或经分支、循环、嵌套调用间接引入工具与任务操作，都会被检查器拒绝。prepare 虽不立即发送请求，也不在当前纯函数构造集合中；函数输出限于可转移的数据与只读证据值。

call 必须引用明确声明的纯函数，不能把一个普通 workflow 或伪造的 CheckedPlan 元数据当作函数。函数可调用其他函数，但不支持递归、函数指针、闭包、宿主 FFI 或动态导入。源码前向引用允许，静态引用环拒绝；嵌套深度和展开后的 IR 仍受原有限制。

参数按名称精确绑定，不做隐式捕获。调用在当前执行上下文中求值，不创建子任务或新的外部预算。函数参数标签合并到调用控制上下文，未使用参数也保守参与限制。直接返回同一 Checked 参数可以保留原回执；投影、算术和重建记录仍按已有规则失去原 artifact 的检查证据。

函数内部的 if 只根据本次实参计算，不写 BranchChosen。纯循环也不写循环历史；同一函数的不同调用不会错误复用另一次调用的选择。根 run 的创建、输入验证与绑定仍由可信宿主负责，纯函数声明不绕过 Checked 输入的入口验证。

## 有界状态循环

repeat 是显式有界的状态折叠，不是静态复制 N 份循环体：

```text
fn step(index: Int, state: Int) -> Int {
    let next = add(state, index);
    return next;
}

fn sum(count: Int) -> Int {
    let initial: Int = 0;
    let result = repeat(count, 1000, initial, step, {});
    return result;
}
```

参数依次是运行时 count、静态 limit、初始 state、循环体计划和显式捕获。count 必须是 Int schema；limit 是 0 至 1,000,000 的整数常量，进入循环时检查 `0 <= count <= limit`，失败时不会执行第一轮。零轮返回初始值。

循环体必须有 `index: Int` 和 `state: T` 参数，返回同一个 T，包括类型类别、schema 和谓词集合。index 从 0 到 count−1。其余参数与捕获映射精确对应；捕获不能替换 index 或 state。当前 T 只能是 Data、Candidate、Observation、Checked，不包含 Permit、Task、Scope 或 Budget。

固定状态类型保证零轮和多轮结果含义一致。Candidate 状态不能因循环而变成 Data，Checked 状态若被变换，必须在带效应的循环体中重新取得所需证据，不能把旧回执沿用到新状态。循环次数、初始状态、捕获、每轮结果与控制标签均参与信息流合并；以私密次数计算出的常量结果也不自动公开。

循环体若是 workflow，可声明工具调用、条件分支或结构化任务，但其效应必须包含在父计划中，并递归遵守推测写入策略。本阶段的数据循环要求每轮资源生命周期在循环体内闭合，未决外部费用继续由原账本承担。后续 [受检资源循环](38-checked-affine-loops.md) 已支持跨轮移动及不同资源清单返回，并通过独立交接回执与游标/代次事务恢复。

## 带效应循环的恢复

schema 升至 10，新增 `loop_frames`。以 `(run, activation, node)` 为键，保存：

- 首次 count、limit、捕获值及控制标签；
- 下一个待执行 index；
- 最后一个已提交的状态值、证据引用及标签。

首次进入时固定输入，不因恢复时新观察到的 count 或捕获而改变已有循环。每轮使用包含 index 的稳定分支路径，因此同名工具节点和作用域在不同轮次有不同身份；同一轮重放则复用原身份。

循环体成功返回后，状态和游标在一个事务中推进。游标未提交前崩溃，恢复重建这一轮并复用其已有操作、费用和结果；已提交则从下一轮开始，跳过完成的循环体。框架不为跳过的旧轮次创建新的历史或派发。

这是一份滚动状态检查点，不为每轮复制完整状态快照。审计事件记录 LoopStarted 与每次 LoopAdvanced，工具自身的操作和回执仍独立持久保存。尚未提交的轮次继续遵守已有的描述绑定、重放冲突和 Unknown 协议，不通过检查点推断远端没有发生效果。

循环体等待丢失的回执时，游标不推进，已派发 attempt 的预留不释放；恢复也不会重新发送该 attempt。计算或工具预算耗尽时同样保留已完成进度和已经发生的费用，错误不是整轮外部事务回滚。

新建循环或推进游标都检查当前 run 与 worker 代次，禁止越序推进、越过冻结的次数、跨 run 或旧 worker 写入新进度。重放可以读取已有事实。迁移保留既有预算、任务和条件记录；不含新函数/循环节点的计划保持原指纹。

## 执行额度

```python
execution = ExecutionBudget(steps=100000, timeout=30)
result = Interpreter(broker).run(
    plan, run, principal, grants, execution=execution)
```

每个执行节点消耗一个步骤，每次实际进入循环体另消耗一个步骤。默认额度为 100000；函数、嵌套循环、分支与结构化子任务共享同一个线程安全计数器，不能各自重置额度。直接使用 TaskScope 的宿主也可传入同一个 ExecutionBudget；未指定时，该作用域的子任务共用一份默认额度。

耗尽抛出 `ExecutionLimitExceeded / AL6003`，不会执行下一个节点。作用域取消、责任接管与迟到回执处理仍可完成，不要求再获得计算额度才能清理资源。并行任务之间的额度竞争可能决定哪个任务先失败，因此测试检查总体上限及责任状态，不承诺固定调度顺序。

可选 timeout 使用单调时钟，在节点、轮次与返回边界协作检查。它不能强行中止已经运行的可信 Python 回调；适配器仍需自己的请求超时与进程隔离。回调晚于期限返回时，已发生的业务与费用事实仍先写入账本，再在返回边界报告超时。

这份额度属于一次宿主执行调用，不是持久货币预算或跨恢复的总 CPU 配额。恢复可由宿主提供新额度，但已消耗的外部费用不会重置。纯函数计算在恢复时重算，已完成的带效应循环则从游标恢复，通常消耗更少本地步骤。

节点步数是操作粒度计量，不是 CPU 指令计数。已有 1 MiB 纯值上限继续生效，但总活跃值、宿主适配器、并发结果队列和解释器进程内存尚未形成完整硬上限。严格内存隔离、跨恢复计算配额和资源竞争侧信道保证仍在后续范围内。

## 源码、SDK 与示例

SDK 使用 `PlanBuilder(..., pure=True)` 定义函数，再用 `call` 与 `repeat` 构造同一 IR。

```python
next_value = body.call('next', accumulate,
                       inputs={'value': state, 'delta': index})
result = parent.repeat('total', count, 10, initial, body_plan)
```

`examples/bounded_batches.al0` 在每轮调用纯函数计算状态，再调用本地模拟 emit。配套目录只用于静态签名检查；运行示例不访问网络。

```powershell
python -m al0.frontend examples/bounded_batches.al0 --catalog examples/bounded_batches.catalog.json --entry bounded-batches
python -m examples.bounded_batches
```

实际结果：

| 项目 | 结果 |
| --- | --- |
| 最终值 | 6 |
| 四次 emit 输入 | 0、1、3、6 |
| 确认费用 / 未决预留 | 4 / 0 |
| 首次解释器步骤 | 35 |
| SDK 恢复步骤 | 3 |
| 恢复新增事件 | 0 |
| 源码与 SDK 规范化 IR | 相同 |

`tests/test_loops.py` 的专项验证覆盖纯度与递归拒绝、实参绑定、类型不变量、动态次数限制、零轮、共享额度、协作超时、外部费用限制、Unknown 恢复、嵌套游标、标签保留以及 v9 迁移。`tests/loop_worker.py` 在循环入口提交前后和状态推进提交前后强制进程退出，恢复后的独立适配器调用记录都保持 0、1、3、6，没有重复调用。

这些是当前实现的具体证据。仿射资源循环、提前退出/异常控制流、闭包和嵌套表达式语法、完整本地资源隔离及组合语义证明仍需继续完成。测试总数以 [实现状态](08-implementation-status.md) 为准。
