# 真实 Git 发布适配器与恢复验收

审计修复之后，首个真实工具接入选择 **Git 固定提交发布与引用查询**。`al0/git_adapter.py` 调用实际 Git 客户端及远端 Git 接收服务；本地集成测试使用真实 bare repository，在线验收使用本项目的 GitHub 仓库。它没有用 SQLite 模拟远端业务效果，也不调用模型。

## 契约和权限

`GitPublisher` 的可信宿主配置固定本地对象库、具体远端 URL/绝对路径、专用引用前缀、允许的接收方，以及明确批准的完整 commit ID 集合。计划只能提交 `{commit: <完整对象 ID>}`，不能指定 shell、路径、远端、任意 refspec 或分支名。目标、命名空间和批准集合参与工具版本；工具目录和计划指纹随配置改变。

宿主批准的是提交的**全部可达对象及历史**。SHA 字符串本身公开，并不表示其指向的文件或父提交允许公开；因此批准集合为必填配置。运行时仍先检查请求标签与接收方。Git 对象库、Git 配置、凭据、服务端 hooks、仓库管理员和接收方映射属于可信基。

每个操作派生一个独立引用：

```text
refs/heads/al0/publish/ + digest([operation_id, intent_digest])
```

发布使用明确的空 expected 值：`--force-with-lease=<ref>:`。该形式要求目标引用原先不存在，不采用 remote-tracking 分支的可变值，也不使用一般强制覆盖。每次仅指定一个完整 commit/ref 映射，并关闭跟随 tags、递归 submodule 推送及本地 pre-push hook。Git 的机器可读结果必须确认该引用新建或已经指向相同值，才能返回 Applied。依据见 [git-push 的明确 lease 与输出规则](https://git-scm.com/docs/git-push)。

本适配器没有“合并到 main”的语义。它的业务完成条件是：批准的提交已被发布到该 operation 的固定引用。宿主必须独占并保留这个命名空间；外部管理员重写/删除它会破坏用于恢复的记录。查询读到不同提交会报 EvidenceMismatch，不将其解释为成功。

## 失败、对账与额度

工具目录将 `retry_window` 固定为 0，禁止 Store 创建重发 attempt。进程崩溃、连接失败、超时或未确认的 push 返回保留 Unknown。恢复先读取原 attempt；既有派发权不会再次发送。

`git.query` 通过 `ls-remote --refs --exit-code` 查询精确引用，核对可信 Store 中原 intent 的摘要及批准提交。相同引用和提交构成 Applied 的正面证据；无匹配引用只返回“当前没有业务证据”，不能证明迟到请求以后不会生效。[Git 查询文档](https://git-scm.com/docs/git-ls-remote) 中的无匹配退出码只用于区分一次成功的空查询和传输失败。

`git_dispatches` 是宿主策略额度：每个已授予的派发权计 1，包括未确认、超时及进程退出的 attempt。成功回执立即结算；查询可依据持久 DispatchStarted 记录补结原操作额度。一次发布加一次查询确认消费 2 单位。它不计字节、Git 子请求数、CPU 或真实货币费用，也不是 GitHub 账单；本轮未接入付费模型供应商。

同一个查询位置重放原观察；需要再查时使用新的 `query_key` 并占用新的查询额度。取消只阻止新派发，不保证已进入 Git 传输链路的请求被撤回。查询成功但引用缺失时仍允许保留 Unknown，之后迟到的提交可以合法变为 Applied。

## 可运行入口

`examples/git_publication.py` 同时构造 SDK 和 AL-0 源码，断言共享 IR 完全一致，再经 Broker、Store、授权、接纳、派发和对账执行。独立的 `host.sqlite` 持久保存配置、run 和 grant；在此事务提交之前不会开始外部发布。重复使用状态目录必须匹配原配置。默认额度为 10、授权有效期为一小时；更长周期的授权更新需宿主另行管理。

下列命令会真实发布一个批准的提交。远端必须为具体 URL 或绝对路径；`origin` 这样的可变别名会被拒绝。

```powershell
python -m examples.git_publication `
  --repository E:/AgentLanguage `
  --remote https://github.com/StephCpa/AgentLanguage.git `
  --commit <已经审查并批准发布的完整提交ID> `
  --state-dir <持久状态目录> --recipient public
```

使用同一组参数恢复；若先前查询仍为未知，需要显式追加 `--query-key query-1`。新查询不重发原发布。Git 必须安装且宿主已配置相应认证；传输诊断不直接写入语言结果，以免凭据 URL 进入输出。

## 本轮证据

本机 Git 为 `2.50.1.windows.1`。11 项真实 Git 专项测试及既有对账测试合计 31 项通过，日志为 `models/git_adapter_integration.log`。覆盖：

- 正常发布、SDK/源码相同 IR、持久宿主重启及重放零新增事件；
- Git 已提交但响应丢失，重启后查询确认并补结额度；
- Git 已提交后通过 `os._exit(77)` 退出，独立进程恢复；
- 派发后的请求延迟，空查询、取消、迟到提交与再次查询；
- 空 lease 阻止引用改绑，以及外部故意改绑时对账拒绝；
- 私密标签请求拒绝、未批准对象及 revision/选项语法拒绝；
- 传输失败保持 Unknown，禁止自动 retry；宿主配置漂移改变身份。

在线验收已把仓库原先公开的 `3f6af4a684beafcce83fbfb9b76a56d015f6354c` 发布到：

```text
refs/heads/al0/publish/7804f5d69f105a8bbe1a3a1219e4f71156d080130106f11215c18cbe36f910db
```

结果为 Applied / CompletedClean，确认额度 2、预留 0、未决责任为空，重放新增事件 0。`models/git_adapter_live.json` 保存运行结果；`models/git_adapter_live_oracle.json` 保存通过 GitHub REST 独立回读的引用与提交。该引用保留作为此次操作的恢复记录。本次在线调用验证正常 HTTPS 路径；丢回复、取消交错和崩溃证据来自本地真实 Git 服务测试，不能把它们写成已经在 GitHub 上注入故障。

安装验收脚本也增加了源码目录之外的真实 Git 消费者，检验 wheel 中的适配器、发布/查询和重放。本机重建 wheel 的运行及固定 mypy 验收已通过，结果为 `models/git_adapter_wheel.json`，日志为 `models/git_adapter_wheel.log`。

[CI 运行 40307365400](https://github.com/StephCpa/AgentLanguage/actions/runs/40307365400) 对代码提交 `89934ff2d201c1752ac685ad04f9d232d4283f72` 的四个 job 全部通过：Python 3.10 和 3.12 各运行 961 项测试、无跳过，包含固定 mypy 1.18.2；copies 和 symlinks 两种环境均通过包含真实 Git 消费者的独立 wheel 验收。摘录与 job 记录保存在 `models/git_adapter_ci.json`。

## 后续研究边界

本轮提供一个契约明确的真实外部动作，尚未证明自动代码修复成功率或语言开发收益。接下来应把审查/测试得到的批准提交接入配对工作负载，并保持 SDK 与 AL-0 使用同一 Git 适配器、权限、预算和故障计划。ContextView 接入、真实模型成本/拒绝契约、强 SDK 基线的开发任务测量，以及完整协议对应证明仍在原计划内。
