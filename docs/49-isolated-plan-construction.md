# 隔离计划构造与宿主 IR 复核

配对评测器原先只允许已审阅的本地 Python。为了接入 coding agent 提交，新增显式 Docker 构造后端：`--builder-image <可信镜像>`。未选择这个参数时仍是原有可信本地模式；选择之后，Docker、镜像或清理失败都会记为基础设施失败，不会降级成宿主执行 Python。

## 边界与执行顺序

宿主读取并复制提交源码，固定本轮 Catalog 和批准提交。容器只看到四个文件：提交源码、工具目录 JSON、该目录的生成绑定、包含 commit ID 的请求。容器不能访问 Git 对象库、端点、任务参考答案、宿主工作区或运行授权。宿主在构造完成并复核 IR 之后才创建 run 和 grants。

SDK 先在一个容器内通过固定 mypy 1.18.2 严格检查，再在另一个容器内导入 Python 并构造计划。候选代码不能修改已经结束的类型检查进程。AL-0 在相同策略的容器中编译。输出只以有大小限制的 UTF-8 数据文本返回；宿主使用自己的原 Catalog 重新执行 `check_plan`，忽略容器内 Python 对象、受检包装和目录指纹声明。合法 IR 之后仍由共享解释器与 Broker 检查和执行。

每个容器都使用：无外部网络、只读根文件系统、非 root UID/GID、删除全部 Linux capabilities、no-new-privileges、只读输入挂载，以及私有的 64 MiB `/tmp`。不挂载 Docker socket、不传入宿主环境变量、不共享 PID 命名空间，也没有可写宿主输出目录。额外限制为 512 MiB 内存与相同总 memory+swap、1 CPU、64 个进程；默认执行期限 90 秒，总 stdout/stderr 上限 2 MiB。镜像日志驱动关闭，避免候选输出无限进入 daemon 日志。

这些设置基于 [Docker run 参数](https://docs.docker.com/reference/cli/docker/container/run)；内核、Docker daemon、运行镜像及其依赖是可信基，边界不能抵御容器逃逸漏洞，见 [Docker 安全边界](https://docs.docker.com/engine/security/)。本后端用于隔离研究评测中的程序构造，不宣称提供完整恶意代码托管平台。

宿主从 Docker inspect 回读实际策略及容器状态，核对网络、只读挂载、权限、用户和资源限额。超时或输出超限时强制删除整个容器，包含其子进程；正常结束也删除容器。不能确认删除时拒绝接受结果。仅杀死 Docker CLI 不足以确认容器已经结束，因此不会据此继续执行 IR。

## 镜像与复现

构建脚本只向 Docker 发送 `al0` 的 Python/类型标记文件、构造入口和固定 mypy 配置，不发送整个仓库。镜像绑定实现摘要和策略版本；调用前解析到不可变 image ID，并核对与宿主当前实现一致。镜像必须由可信宿主明确构建和选择；标签与摘要检查用于发现陈旧镜像，不是对任意第三方镜像的来源认证。基础镜像标签和依赖下载发生在构建阶段，实际运行的镜像 ID 会保存到报告。

在有 Linux Docker daemon 的环境中：

```bash
python -m scripts.build_plan_builder_image --tag al0-plan-builder:local \
  --output models/isolated_builder_image.json
python -m scripts.git_pair_evaluation pilot --output <new-folder> \
  --builder-image al0-plan-builder:local
python -m scripts.check_isolated_builder --image al0-plan-builder:local \
  --output models/isolated_builder_acceptance.json
```

`evaluate` 和 `compare` 同样接受 `--builder-image`。构造后执行真实 Git 的宿主 Python 仍可通过 `--python` 选择；容器内类型工具已经固定，不需要在宿主另装 mypy 来使用该后端。命令不自动安装 Docker 或拉取缺失的运行镜像。

## 验收与后续范围

CI 新增独立 Linux Docker job，构建真实镜像并执行：

- 两个前端在三种 Git 故障调度中形成相同 IR，并通过真实发布/恢复评分；
- SDK 字段类型错误在导入 Python 之前被拒绝；
- 合成宿主秘密与环境变量不可见、输入及实现文件不可写、容器只有 loopback 接口、权限已被删除；
- 容器返回伪造的 `mint_permit` IR 时由宿主拒绝；
- 大量输出、无限循环及其子进程触发限制后，容器被删除；
- 镜像不可用时没有本地构造回退，没有创建运行数据库或外部效果。

本机 Windows 未安装 Docker，WSL 也未检测到容器运行时。本机预检只验证白名单构建上下文和无回退路径，不能算作容器隔离验收；实际隔离结果以 Linux CI 的独立 job 为准。

2026 年 10 月 26 日，[CI 40316268154](https://github.com/StephCpa/AgentLanguage/actions/runs/40316268154) 对提交 `f055fb30ae45135b0ff9eca6970b2d95fa8c4458` 的五项 job 全部通过。真实 Docker 验收用时 50.296 秒：六格配对发布/恢复、九项隔离和清理判据全部通过，结束后没有残留构造容器。Python 3.10/3.12 全量回归与两种 wheel 环境也通过。包含镜像 ID、实际容器策略、退出状态、恶意输出/超时探针及宿主复核拒绝结果的原始报告已归档至 `models/isolated_builder_acceptance.json`，不含运行授权映射。

隔离后端解决的是接收生成程序的执行边界。独立任务集、模型配置与费用记录、逐轮诊断/修复记录，以及跨模块代码修复和证据绑定仍需继续接入。参考答案的成功率仍不构成语言开发收益证据。
