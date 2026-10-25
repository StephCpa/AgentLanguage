# SDK 构造接口的静态类型验收

本轮为 Python SDK 建立可运行的类型验收，修复工作流、条件分支、循环和任务结果的类型丢失。初始验收覆盖 `PlanBuilder` 的 48 个公共构造方法，资源包扩展至 52 个，Variant 扩展至 55 个，infer_result 扩展至 56 个，推理进度接口扩展至 58 个，try_infer 扩展至 59 个，使用固定版本 mypy 1.18.2 的严格模式。类型验收沿用共享 IR 检查、解释器与账本；当前运行时数据库版本为 schema 21。

## 修改及其作用

`CheckedPlan[T]` 现在保存 Python 层的结果类型关系。`finish(Ref[T])` 返回 `CheckedPlan[T]`；`invoke`、`invoke_caps`、`call`、`repeat`、分支和任务构造沿用这个结果类型，`spawn` 返回 `Task[T]`，`join` 和 `race` 恢复 `T`。这不添加 IR 字段，也不改变规范化编码、程序指纹或持久身份。

因此，调用返回 Text 的子工作流后把结果写成 `Ref[int]`，或把 `Task[str]` 当作 `Task[int]`，可以直接由 Python 类型工具拒绝。过去一些接口的结果只有未参数化 Ref，或返回类型变量与子计划无关，不能支持这项保证。

已有未参数化的 `CheckedPlan` 注解仍可运行；采用严格类型检查时应补上具体结果参数，或在确为动态解码边界时明确使用 `CheckedPlan[Any]`。

Cap、Intent、Permit、Operation、OpRef、Snapshot 和 TaskOutcome 的类型参数也沿接口传播。`parameter` 对已知 Data/Candidate/Observation/Checked 种类保留相应包装。`infer` 和 `observe` 的参数现在接受共享 IR 已允许的数据、候选、观察和已检查值，修复此前注解过窄而拒绝合法数据流的问题；`prepare` 仍要求普通数据或 Checked，不能把 Candidate 直接准备为动作。

```python
from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder, Ref

def child() -> CheckedPlan[str]:
    b = PlanBuilder('child', Catalog())
    return b.finish(b.literal('message', 'Text', 'hello'))

b = PlanBuilder('parent', Catalog())
value: Ref[str] = b.invoke('result', child())
# 将上行写成 Ref[int]，mypy 会在调用处拒绝。
plan: CheckedPlan[str] = b.finish(value)
```

## 验收证据

`scripts/check_sdk_types.py` 执行以下验收并生成 `models/sdk_type_audit.json`：

| 项目 | 当前结果 | 防止的误判 |
| --- | --- | --- |
| 公共方法注解与正例调用 | 59 / 59 | 未测试的方法不能因文件整体通过而被算作覆盖 |
| 严格检查正例源码 | 原有正例、生成器/绑定/调用及可信模型响应正例，共十一个文件通过 | 不靠忽略全部导入或容忍正例错误过关 |
| 正例实际构造 | 26 个基础计划被共享 IR 接受，含资源包、资源 TaskOutcome、race、Variant、infer_result、资源循环、infer_progress/resume_infer 与 try_infer | Python 类型通过不等于能构造合法 AL-0 计划 |
| 指定负例 | 40 个用例在各自标注行被拒绝 | 任意导入错误或无关错误不算负例成功 |
| 静态接受、共享检查器拒绝 | 7 个边界用例符合预期 | 不把 affine、依赖身份及动态 schema 规则归功于 Python 泛型 |
| Catalog 生成绑定扩展 | 9 个 IR 正例、14 个指定行静态负例 | 对固定目录的字段及工具身份建立可复现检查 |

负例包括模型输入类型不符、Candidate 绕过检查、用数据申请授权、资源类别混用、不兼容的许可和能力类型、对借用 OpRef 执行 await、任务与作用域混用、非整数资源项、非 Bool 条件、不同分支结果、工作流/任务返回类型错误、循环状态不一致、混合类型 race，可信模型响应的非法 kind/非整数费用，以及 pack/unpack/take 的类别误用。脚本要求预期错误行集合与实际集合完全相同，保存每项诊断代码和消息。

负例是手工选择的回归集合；开发错误率及语言收益由后续配对任务实验测量。

正例同时使用 `assert_type` 验证推导结果，而不是只判断“没有报错”。这些类型断言只服务开发验收；普通运行时测试在没有 typing 扩展依赖时仍可构造和检查计划。

## 两层检查的职责

| 规则 | 本轮 Python 类型工具 | 共享 SDK / IR 检查 |
| --- | --- | --- |
| 已声明输入、包装类别及工作流结果类型 | 检查 | 检查实际 schema 与种类 |
| Permit/Operation/Budget 等类别混用 | 检查可辨认的静态类型 | 检查实际节点与资源类型 |
| 同一许可被使用两次 | 不能据此证明 | 按 SSA 节点与消费集拒绝 |
| 同一工具的另一 intent 使用原许可 | 静态类型可能相同 | 按精确 intent 绑定拒绝 |
| 同类型引用来自另一构造器 | 静态类型相同 | 按构造器身份拒绝 |
| 字符串工具名对应的实际种类 | 依赖宿主声明 | 对照 Catalog 拒绝 |
| 字符串 schema 与值一致 | 依赖声明或推断；不解析 schema 字符串 | Catalog 与 IR 校验 |
| Python bool 冒充整数预算 | Python 的 int 子类型关系不能排除 | 数量校验拒绝 |

通用 SDK 的工具与保护值类型参数不自动编码每一个工具名、权限范围、谓词集合或 intent 身份。后续 [Catalog 生成绑定](26-catalog-type-bindings.md) 为固定目录的不同工具生成独立名义标记，但权限范围、谓词和具体 intent 仍必须通过 Catalog、授权及绑定检查。泛型值不是可在运行时使用的授权凭证。

`compute`、`field`、`record` 和 `resources` 的结果仍明确标注为 `Ref[Any]`：它们涉及注册 schema、字段选择和 Candidate/Observation 的合并规则。本轮没有把这些结果错误地标为普通 Data。字典形式的工作流参数、动态种类、动态工具绑定和 JSON 解码的 `CheckedPlan[Any]` 也保留动态边界。负例与覆盖计数不代表这些边界已经获得静态证明。

生成绑定已为固定 schema 的字段投影补上准确类型与来源重载，详见 [Catalog 类型绑定](26-catalog-type-bindings.md)；上述通用动态接口边界仍然存在。因此，后续强基线实验应将 **生成绑定 + mypy + SDK 构造 + 共享 IR 检查** 作为完整 SDK 入口，并与 **AL-0 源码编译 + 同一个 IR 检查** 比较。两边共有的许可绑定或资源消费拒绝不能被计为独立语言的收益。动态接口精化以及开发任务和故障条件下的对照仍待完成。

## 工具配置与分发

`pyproject.toml` 增加可选开发依赖 `typing = ["mypy==1.18.2"]`，运行时依赖仍为空。SDK 使用实际 Python 文件内的注解，没有另写一套可能与实现漂移的 SDK stub。

配置使用 `strict = true` 和 `follow_imports = "silent"`。直接传入的 SDK 与验收文件进行严格检查；被导入的其他内核模块仍提供可分析的类型信息，但其内部错误不在本门槛中报告。因此，这不是整个解释器或数据库内核已经通过严格静态检查的声明。`silent` 和把导入替换为 Any 的 `skip` 不同，行为依据 [mypy 官方导入说明](https://mypy.readthedocs.io/en/stable/running_mypy.html#following-imports)。

包内新增 `py.typed`，并通过 setuptools package-data 将其打包，使安装后的消费者可发现内联注解；分发规则依据 [Python 类型分发规范](https://typing.python.org/en/latest/spec/distributing.html)。`scripts/check_installed_typing.py` 在源码目录之外创建临时消费环境，从本地 wheel 安装 AL-0，验证实际导入来源、mypy 的 SDK 来源、正例构造和类型检查，以及一项错误工作流结果的拒绝。该消费者不安装运行时依赖，不通过 MYPYPATH 指回源码。

本地构建的 `al0_reference-0.1.0-py3-none-any.whl` 已通过这项安装验证。运行时及 mypy 均确认使用临时环境中已安装的 SDK；消费者的合法计划可构造并通过类型检查，把子工作流结果错误写为 `Ref[int]` 则在指定行被拒绝。结果保存在 `models/sdk_wheel_audit.json`，该 wheel 尚未发布到包索引。后续提供方结果扩展也在独立消费环境验证了 Output、Refused、Blocked、迟到费用、固定重放及可信响应的类型正反例。

本机 mypy 2.4 的原生构建及其必需的 librt 模块遇到 Windows DLL 文件占用，最终工具链固定为不依赖 librt 的 mypy 1.18.2 纯 Python 构建；报告保存 `compiled: no`。仍检查同一组正反例，不降低严格规则，也不增加运行时依赖。

## 复现

在项目根目录、安装开发依赖的环境中运行：

```powershell
python -m pip install -e ".[typing]"
python -m scripts.check_sdk_types
python -m tests.type_cases.positive
python -m tests.type_cases.runtime_only
python -m unittest tests.test_sdk_typing -q
```

也可通过 `--python` 指定独立的类型工具环境，例如本机的 `.venv-typing-pure\Scripts\python.exe`。如需纯 Python mypy，可在新的开发虚拟环境中使用 `pip install --no-binary=mypy mypy==1.18.2`。

分发检查：

```powershell
python -m pip wheel . --no-deps --wheel-dir dist/typing-audit
python -m scripts.check_installed_typing --wheel dist/typing-audit/al0_reference-0.1.0-py3-none-any.whl
```

构造接口报告写入 `models/sdk_type_audit.json`，安装包报告写入 `models/sdk_wheel_audit.json`。单元测试在未安装 mypy 时会明确跳过可选静态检查；该跳过不能算静态验收通过，独立命令始终要求固定版本工具并在缺失时失败。CI 可设置 `AL0_TYPING_PYTHON` 选择工具环境、`AL0_TYPING_REPORT` 保存结果。本轮完整回归启用了该检查。

审计修复后，wheel 脚本对 `--python` 只做绝对路径转换，保留 POSIX venv 符号链接所决定的环境；wheel 文件本身仍可解引用。`.github/workflows/ci.yml` 已配置 Python 3.10/3.12 全量与固定 mypy，以及独立 symlinks/copies venv 安装验收。SDK 的 Cap 来源推断也改为共享 IR 类型查询，覆盖 Bundle/Variant 及连续衰减，具体矩阵和本轮结果见[审计修复](46-audit-remediation.md)。
