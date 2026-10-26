# AgentLanguage 交互架构图集

双击 [index.html](index.html) 打开统一入口。五张 HTML 均嵌入 Archify 查看器、样式和字体，可独立离线浏览；点击源码链接时需要网络。首次打开通常是静态模式，点击右上角「静态」即可启用连续流光动画；系统的减少动态效果偏好仍会生效。

| 图 | 解释的问题 |
|---|---|
| [总体架构](architecture.html) | AL-0、SDK、共享 IR、解释器、Broker、SQLite 和 Git 如何衔接 |
| [数据流](dataflow.html) | 模型响应如何经过解析、隔离构造、宿主复核与独立评分 |
| [恢复时序](sequence.html) | 发布已发生但回包丢失后，如何显式查询与记账 |
| [模块层次](tree.html) | 语言、持久执行与研究工具各自负责什么 |
| [状态与责任](lifecycle.html) | 控制结束与未决责任为何必须分开 |

可用交互：节点详情及固定版本源码链接、节点搜索、上下游与有向路径追踪、缩放、四种视觉风格、深浅主题、演示模式、Live/Still 动画及查看器内导出。树图连线表示职责归属；时序图间距不表示测量耗时；状态图展示主要综合状态，不能当作所有数据库字段转换的穷举。

图中架构以真实 Git 发布切片为主。模型推理、任务、资源包、循环与信息流在模块层次和说明卡中概括。ContextView、完整语言目标和独立任务集研究冻结尚未全部完成。当前图集不对两个前端的性能或安全优势作经验结论。

## 来源与生成

- 项目源码固定到 `e29b3f694d81eb06607eb3da26ea14a38034fc11`。
- 使用 [Archify 3.2.0](https://github.com/tt-a1i/archify/tree/81c6be8d87090670c45d08a8f97f9d29c5af0bec)，工具源码固定到 `81c6be8d87090670c45d08a8f97f9d29c5af0bec`。
- 实际运行上游 `bin/archify.mjs finalize` 生成五张 HTML，未修改上游模板或查看器。`index.html` 是本项目添加的导航入口。
- `make_candidates.py` 保存本项目的图内容；相邻 JSON 为 Archify 原生输入，均含源码证据。生成过程不调用付费模型 API。
- 五张图通过 showcase validate、deliver、check、browser-check。回执保留在本目录及 review 子目录中；dataflow 的当前回执在 `review-2/dataflow/`。旧回执对应旧字节，不应用作新文件的验收证据。
- 数据流图有三处自动布线交叉，交叉处采用断线区分；已检查截图，保留「任务契约」这条必要输入关系。不是连接汇合点。

在项目根目录，以 Node.js 18+ 和本地 Chrome 重建（工具路径按实际位置替换）：

```powershell
python .archify/architecture-agentlanguage-20261027-010149/make_candidates.py
python .archify/architecture-agentlanguage-20261027-010149/render.py --archify C:/path/to/archify/archify/bin/archify.mjs
```

`render.py` 按顺序运行五次完整 finalize，并将新浏览器证据放到独立目录。源码证据需要本地 Git 中仍保留上述项目提交。重建只更新图文，不执行项目工作流，不访问模型密钥。

## 许可

本图集含 Archify 的嵌入式软件，按上游 Apache-2.0 及历史 MIT 声明分发；字体保留 SIL OFL。完整上游 `LICENSE`、`LICENSE-MIT`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 与字体许可位于 [licenses](licenses/)。图中的项目内容不因嵌入查看器而改变原项目权利归属。
