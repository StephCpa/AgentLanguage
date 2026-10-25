# 受检资源包：混合任务结果与完整接收

更新日期：2026 年 10 月 26 日。

本阶段实现 [资源结果包设计](30-resource-result-bundles.md) 中的命名包、嵌套包、依赖类型、任务输入与正常返回。一个任务可以一起返回普通数据、检查证据、Cap、Intent、未消费 Permit、Operation 和 Budget，再由父方原子接收。后续 [资源任务结果](32-resource-task-outcomes-and-race.md) 已接通资源 TaskOutcome/race；一般业务资源 sum type 已在 [受检业务 Variant](34-checked-business-variants.md) 实现；固定次数资源循环已在 [受检资源循环](38-checked-affine-loops.md) 接通。

## 语言接口

```text
workflow forward(packet: Bundle<{
    request: Intent<"send">,
    permission: Permit<request>,
    funds: Budget,
    note: Text
}>) -> Bundle<{
    request: Intent<"send">,
    permission: Permit<request>,
    funds: Budget,
    note: Text
}> uses {} {
    return packet;
}
```

包类型是封闭的结构接口；至少包含一个仿射字段。只包含普通数据的结构继续使用现有 record。嵌套 Bundle 具有自己的封闭字段依赖。

```text
let packet = pack({request: request, permission: permission, funds: funds, note: note});
let opened = unpack(packet);
let returned-request = take(opened, "request");
let returned-permission = take(opened, "permission");
let returned-funds = take(opened, "funds");
let returned-note = take(opened, "note");
```

pack 在静态环境中移动 Permit、Operation、Budget 和嵌套 Bundle，禁止原变量再被使用。它本身不改变资源的数据库 owner，也不重新划拨资金；跨任务时才执行 custody 转移。

unpack 消费原包，take 每个字段一次。程序必须取完该接口的全部字段；取出的数据可以照常复用，Operation 和 Budget 恢复各自的处理义务，Permit 保持仿射性质。Unpacked 只用于当前程序中的 take，不能返回、跨任务传递或通过普通 field 访问。

当前实现采用 unpack/take 两步形式；一次性源码解构可以在以后展开为相同 IR。它不会把资源包降为可任意复制的 JSON record。

## Permit 依赖与身份

pack 必须同时包含 Permit 和它实际绑定的一个 Intent。静态接口将该关系规范化为包内字段名；不存在的目标、错误工具、不同 Intent 或同一 Intent 的歧义重复字段都会被拒绝。运行时再核对 Permit 数据库记录中的完整 Intent。

子程序局部 SSA 名字不进入返回接口。两个程序即使使用不同的局部 Operation 名字，也可返回相同的包类型。拆包产生的 Intent 身份使用带标签的内部元组，与任意用户 SSA 字符串处于不同命名空间，防止原始 JSON IR 用特殊字符串伪造身份相等。

SDK 增加 `pack`、`unpack`、`take`、`bundle_parameter`，共 52 个公共构造方法。bundle_parameter 从已检查的 Bundle 结果计划取得接口。`Task[Bundle]` 和 `Ref[Bundle]` 保留包类别；动态字段名的 take 返回 `Ref[Any]`，字段类型、依赖和仿射规则由共享检查器验证，不计作 Python 静态推导保证。

SDK 对从 take 得到的 Intent/Cap 做前缀类型查询。该内部查询可以暂时保留未完成义务，但不能作为执行凭据：finish、子计划构造和 Interpreter 都重新做完整检查；未拆完的包无法执行。

## 持久值、权限与标签

Value 的 bundle 元数据记录封闭接口，data 保存各字段的值记录；每个仿射叶子包含 ResourceRef。普通数据参数不能接收这样的 Bundle Value。字段 schema、检查回执、工具、run、Permit 依赖以及资源类别都按声明核对。

嵌套资源清单使用规范化字段路径。任务保存原包模板和整个资源转移清单，模板中的原代次必须与清单一致。恢复根据固定转移回执替换各叶子的代次，不查询当前 owner 来刷新引用。重复资源、跨模板别名、字段路径/普通参数冲突或不一致模板在转移前拒绝。

输入模板、普通任务输入、权限表和所有资源移动处于同一 spawn 事务。返回包的值记录和所有资源进入同一 finish_task 暂存事务。join 在一个事务中接收整个清单，随后依据固定接收回执重建包。再次转交后，旧包和旧 join 回执保持原代次。

包内 Cap、Permit 和 Operation 都参与跨任务及受限调用的权限表检查；包不是绕过权限包含关系的容器。原始 root 参数入口仍不允许把宿主拼装的 Bundle 当成受检资源实参。

pack 合并成员标签与当前控制标签，take 再合并包标签；嵌套包、持久调用帧和任务恢复保留字段记录。标签传播可能保守地限制整个包，但不会因拆包丢失私密字段对控制流的影响。

上述类型与结果完整性检查由共享检查器、Broker 和 TaskRunner 执行；直接 Store 调用仍是可自定义业务 payload 的可信宿主接口，不被当作受检语言入口。

数据库结构仍为 schema 17；新增元数据只用于新 Bundle 接口。此前合法计划的 IR 指纹和普通 Value 记录保持不变。资源控制、费用归属和 TaskLease 的原有检查继续执行。

## 实际执行与验收

```powershell
python -m examples.resource_bundles
python -m unittest tests.test_resource_bundles -q
```

[源码](../examples/resource_bundles.al0) 与 [SDK](../examples/resource_bundles.py) 生成相同 IR。子任务返回五个字段：已接纳 Operation、尚未消费 Permit、对应 Intent、Budget 和说明文本。父方把整包交给第二个任务，再接收、关闭来源作用域、拆包并完成两项行动。独立 SQLite 端点记录两次调用及两次收费，根预算确认费用 2、可用 8、预留 0；重放新增事件 0。[实际输出](../models/resource_bundles_example.json) 保留了端点记录与账本汇总。

31 项专项测试覆盖混合包、嵌套输入/返回、完整拆包、重复别名、普通 JSON/根入口拒绝、错误依赖、SSA 身份碰撞、过期模板、标签、包内 Cap 权限边界，以及直接观察不能越过 Escrow。

8 个真实进程退出位置覆盖整个包的再次移入、发布、首次接收和再次接收，分别在事务内及提交后退出。恢复使用同一持久程序和原转移记录，每条路径最终均产生两个端点调用和两次确认收费，没有额外重放事件。

2026 年 10 月 26 日完整回归通过 662 项测试，用时 410.897 秒，见 [回归日志](../models/resource_bundles_full_regression.log)。源码覆盖测试将新示例纳入全部 IR 操作集合的精确比较。

严格类型验收覆盖 52 个公共方法、16 个基础计划与 5 个生成绑定计划、28 + 12 个负例，并明确 take 的动态字段边界，见 [类型报告](../models/sdk_type_audit.json)。[安装包报告](../models/sdk_wheel_audit.json) 确认从仓库目录之外安装 wheel 后，可执行嵌套 Bundle 参数/返回/拆包/预算归还与重放，并对消费计划做严格类型检查。总体保证与仍缺失的机制见 [实现状态](08-implementation-status.md)。

## 仍需推进的接口

正常 join 已可接收完整包；后续 [资源任务结果](32-resource-task-outcomes-and-race.md) 已接通 TaskOutcome 的 Completed 包接收及 race 原子接收。一般业务资源 sum type 与成功/业务拒绝分支已在 [受检业务 Variant](34-checked-business-variants.md) 接通。异常不能猜测原输入中哪些资源仍未消费，也不能制造一个看似完整的返回包。

一般循环控制、ContextView、解密/完整性提升协议、完整模型对应及真实工作负载与同运行时 SDK 对照实验仍在总体目标中。
