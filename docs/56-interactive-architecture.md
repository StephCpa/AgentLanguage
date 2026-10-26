# AgentLanguage 交互架构图

[打开 HTML 图集](../.archify/architecture-agentlanguage-20261027-010149/index.html)。下载仓库后双击该入口即可浏览；GitHub 文件预览不会执行 HTML。

图集包含总体架构、数据流、恢复时序、模块层次和状态与责任五个视图，均由 Archify 3.2.0 生成。支持动态流光、节点搜索、源码详情、有向路径追踪、缩放、主题切换及导出。

项目证据固定在 `e29b3f6`：AL-0 与 Python SDK 汇入共享 IR，宿主复核后由相同解释器、Broker 和 SQLite 执行；真实 Git 对账以精确操作引用为依据。时序图以发布回包丢失为例，展示单独授权查询和持久证据写入。状态图将控制完成与未决责任分开。

该图集是源码导览，不是性能测量或前端优劣实验结果。ContextView 与独立任务集冻结等未完成工作在图中说明。

[生成说明、检查回执与许可](../.archify/architecture-agentlanguage-20261027-010149/README.md)随 HTML 一起保存；无需付费 API 或外部 CDN。
