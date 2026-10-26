# 查询未确认状态与构造诊断

上一轮 DeepSeek 预实验中，三份 SDK 输出经统一单围栏提取后都通过了四个既有场景，但程序最后返回常量 `Published`。四个场景的查询都能确认发布，因此不足以检测“忽略查询结果却报告成功”。本轮使用保存的响应补测，不调用模型，不更改原来的材料、请求、响应和主评分。

## 单独版本化的覆盖扩展

`experiments/query_outcomes_v2.json` 给 conditional-release 增加一个单独评分的 `publication-unconfirmed` 场景。它沿用 v1 的 Git 对象、Catalog、scope 和绑定，复制任务元数据后只运行新场景；原来的四场景契约与摘要保持不变。

宿主在发布操作已取得持久派发权、但尚未执行 Git push 时注入连接中断。候选程序随后执行真正的 `git ls-remote`，查询结果为空。适配器只返回“没有业务确认”的查询响应，不注入虚假的 Applied、Pending 或 ProvenNoEffect。宿主虽知道故障发生位置，但这项知识不是候选程序获得的业务证据。

正确程序返回 `Unknown`，不重新派发发布操作，并保留一个未解决的业务义务，最终状态为 `CompletedWithObligations`。两次派发的配额均按现有宿主契约结算。这里的成功指正确处理不确定状态，不表示发布目标已经完成。评分分别检查真实引用未改变、一次发布尝试、真实查询没有确认、义务保留、配额结算和恢复重放不增加事件。

`scripts/check_query_outcomes.py` 先校准两组参考程序，再通过 Docker 评估六份已保存模型输出。两组应用相同的单围栏提取函数，除此之外不修改代码。每份原始和实际评分源码都记录摘要；这属于事后的覆盖探查，不能并入原始生成成功率，也不能把同一任务的三次输出当成三个独立任务。

Pending、ProvenNoEffect、查询本身的传输故障以及延迟到达的真实发布尚未覆盖。Git 引用暂时不存在不能证明一个迟到的推送永远不会生效。

## 诊断改进

AL-0 隔离编译入口现在直接输出已有的诊断编号、`submission.al0:行:列` 和 hint，避免把 Python traceback 当成修复信息。完整隔离策略和容器状态仍保留在审计记录中；下一次修复提示只接收编译器输出、退出状态和超时/输出限制信息。可信本地评分也保留结构化源码诊断。

CI 增加实际容器探针，验证错误位置和提示确实从编译入口传出。单元测试用真实本地 Git 验证两组参考程序的 Unknown 处理，并验证把查询结果丢弃、改返 Published 的程序只在返回值判据上失败。历史预实验提示不追溯改写；诊断变化是否改善未来模型修复，需要新的预先定义实验。

## 复现

先运行 `scripts.score_deepseek_pilot` 创建并核验冻结的 Linux 任务包，再运行：

```sh
python -m scripts.check_query_outcomes \
  --bundle experiments/deepseek_pilot_v1/captured.json \
  --package /tmp/al0-deepseek-pilot-v1/tasks/conditional-release \
  --output .git/query-outcomes-ci --builder-image al0-plan-builder:ci
```

输出只归档评分摘要，不上传包含运行授权的 host.json 或数据库。实际模型调用仍为此前六次，已记录费用上界仍为 0.0169878 美元，实际账单未知。
