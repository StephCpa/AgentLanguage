# Catalog 驱动的 Python 类型绑定

AL-0 源码前端和 Python SDK 共用同一份 Catalog、IR 检查器与持久运行时。为了使 SDK 成为有竞争力的实验基线，本轮增加 `al0.codegen`：从 Catalog 的固定快照生成 Python schema、工具接口和字段投影。生成代码携带目录指纹，构造器在每次使用以及 `finish` 时核对目录。

这项设计消除了部分手写 schema 注解与实际目录脱节的问题。生成绑定本身没有改变 IR 编码或当时的数据库 schema 15，也不会向宿主申请权限、执行工具或调用模型。后续的 schema 16 attempt 出资记录见 [跨任务资源协议](27-affine-task-transfer.md)。

## 接口生成

```powershell
python -m al0.codegen --demo-catalog --output examples/generated_repair_bindings.py
python -m al0.codegen --demo-catalog --output examples/generated_repair_bindings.py --check
python -m al0.codegen --catalog examples/bounded_batches.catalog.json --output generated_batches.py
```

目录文件沿用源码前端的数据格式，不导入任意 Python 工厂。`--check` 在文件缺失或过期时失败，不改写文件。输入和输出不得是同一路径。

```python
from al0.demo import catalog
from al0.sdk import PlanBuilder
from examples.generated_repair_bindings import Bindings

b = PlanBuilder('repair', catalog())
g = Bindings(b)
request = g.literal_Text('request', 'repair the bug')
draft = g.infer_code_model('draft', request)
tested = g.check_patch_tests('tested', draft)
intent = g.prepare_repo_merge('intent', tested)
permit = b.authorize('permit', intent)
operation = b.admit('operation', intent, permit)
```

完整可检查计划见 [generated_repair.py](../examples/generated_repair.py)。生成接口委托给现有 PlanBuilder；运行时依旧需要宿主提供的 grants、授权、预算和工具适配器。

| Catalog 内容 | 生成接口 | 保留的关系 |
| --- | --- | --- |
| 基本类型和记录 schema | literal、parameter；记录使用 TypedDict | 字段键、嵌套字段及值类型 |
| 记录字段 | field 投影重载 | 字段结果类型与 Candidate/Observation 来源 |
| 每个工具 | 独立 Tool 标记、capability 和 cap_parameter | 工具之间的名义身份 |
| infer、observe、check | 对应种类的方法 | 输入与输出 schema、候选/观察/检查包装 |
| action | prepare、Intent/Operation/OpRef 参数 | 动作工具身份沿资源链传播 |
| reconcile | 目标工具专属的 reconcile | 不接受另一工具的 Operation |

工具标记是 Python 类型参数，不是权限凭证或动作的返回值。即使两个动作的输入、输出和 scope 完全相同，也生成不同标记，从而拒绝把其中一个动作的 permit 或 cap 交给另一个动作。该区别只覆盖工具级身份；同一工具的两个具体 intent 仍由 IR 检查绑定。

生成记录按依赖顺序排列；原始名字以字符串字面量保存，Python 标识符经过确定性的规范化和冲突消解。Unicode、关键字、空格、引号和连字符都不直接拼接成可执行代码。`SCHEMA_TYPES`、`TOOL_TAGS` 和以元组为键的 `METHOD_NAMES` 提供原名到生成名的映射。发生命名冲突时后缀取决于完整目录的排序，因此目录变更后必须重新生成并重新类型检查调用方。

## 来源与证据

字段投影遵循现有 IR 规则：

| 来源 | 投影结果 |
| --- | --- |
| Data[Record] | Data[Field] |
| Candidate[Record] | Candidate[Field] |
| Observation[Record] | Observation[Field] |
| Checked[Record] | Data[Field] |

最后一行意味着记录整体的检查回执不自动适用于一个新字段值。生成器不将它保留成 Checked。信息流标签仍由共享运行时传播；Python 泛型不承担标签检查。

`compute`、异构 `record`、动态字段名和 `resources` 等通用接口仍有动态类型边界。当前生成器精化固定 schema 的字面量、参数、字段和工具调用，不声称解决所有动态数据流推导。

## 目录固定与变更

`Bindings(b)` 首先核对当前目录与生成文件的指纹，并为该 builder 固定目录。工具版本、费用上界、scope、信息流声明、schema 和新增工具等变化都会使后续调用失败。即使调用方随后直接使用原始 SDK 或直接执行 `b.finish`，也不能绕过这次固定。

失败发生在新增节点或效应之前。原本没有使用生成绑定的 builder 仍允许按既有方式配置目录。目录固定针对整个 Catalog，可能拒绝与当前程序无关的新增工具；这是当前选择的简单且明确的兼容性规则，尚未采用依赖子集指纹。

目录被视为宿主配置，生成期间应保持稳定；这不是并发热更新协议。Python 宿主代码也仍在信任边界内。此机制检查普通构造和配置漂移，不防止宿主主动篡改私有属性或跳过入口。

## 验收证据

`tests/test_codegen.py` 的 14 项测试通过，覆盖确定性生成、依赖排序、特殊名字与冲突、快照隔离、目录漂移、原始 SDK/finish 的漂移拒绝、字段来源、CLI、运行时值检查、真实执行及重放。

固定 mypy 1.18.2 验收扩展到 11 个源码文件。基础计划现为 26 个（后续增加任务资源、资源包、TaskOutcome/race、Variant、infer_result、资源循环、推理进度与 try_infer），40 个负例和 7 个共享检查器专属拒绝保留；9 个生成绑定计划被共享 IR 接受，14 个负例在各自预期行被拒绝。新增负例包含字段缺失/多余/类型错误、嵌套字段错误、工具输入错误、错误工具种类、浮点 JSON、Candidate 来源丢失、跨工具 permit/cap 混用、错误对账目标及模型 Operation 工具身份。报告为 [sdk_type_audit.json](../models/sdk_type_audit.json)。

生成版修复计划与手写 SDK 的规范化 IR 完全相同；在独立 SQLite 运行库及端点中，结果、事件轨迹、预算和业务状态一致。再次执行不新增事件。这是具体程序的差分证据，不等于普遍语义等价定理或开发效率提升。

安装包消费验收由 `scripts/check_installed_typing.py` 执行：在源码目录之外的临时环境安装 wheel，调用安装后的生成器，执行生成接口的正例，再用 mypy 检查正例和错误输入。结果保存至 [sdk_wheel_audit.json](../models/sdk_wheel_audit.json)。完整回归的权威计数见 [实现状态](08-implementation-status.md)。

## 静态保证的准确边界

Python TypedDict 是结构类型。字面量的多余字段能被类型工具拒绝，但更宽的 TypedDict 变量可能兼容较窄类型；形状相同的两个 schema 也可能静态兼容。生成 API 不把这些性质当成精确 schema 证明。Catalog 的值检查继续要求字段集合精确一致，IR 继续检查声明 schema。Python 的 bool/int 子类型关系与整数范围同样需要运行时校验。参见 [Python 类型规范](https://typing.python.org/en/latest/spec/typeddict.html) 和 [mypy TypedDict 文档](https://mypy.readthedocs.io/en/stable/typed_dict.html)。

Python 类型工具也不证明许可单次使用、精确 intent 身份、跨构造器引用身份、有效宿主授权或预算充足。强基线实验应使用 **生成绑定 + mypy + SDK 构造 + 共享 IR 检查 + 同一运行时**，不能把这些共享拒绝规则算作 AL-0 独立语法的收益。下一步需要扩展真实开发任务与故障调度下的配对实验，并继续处理跨任务仿射资源、ContextView 和完整语义对应。
