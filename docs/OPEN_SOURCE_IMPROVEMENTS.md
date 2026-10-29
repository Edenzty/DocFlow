# 从开源评测设计到自己的检索诊断

这次参考 [Ragas](https://github.com/vibrantlabsai/ragas)，在原有 BM25、BGE、RRF 检索上增加了**可重算、可定位失败的离线诊断**。没有安装或集成 Ragas，没有复制其源码；这是对指标分层与 ID 匹配思路的独立实现。新增功能不调用生成模型、不重新运行向量检索、不动用原有 40 次模型请求预算。

## 参考什么，自己改了什么

核对日期为 2026-10-29。Ragas 当前仓库所有者为 `vibrantlabsai`，所读源码版本为 `298b68274234c060deacab3cf5fb52aa3a20e885`。核对了 [README](https://github.com/vibrantlabsai/ragas/blob/298b68274234c060deacab3cf5fb52aa3a20e885/README.md)、[IDBasedContextPrecision 实现](https://github.com/vibrantlabsai/ragas/blob/298b68274234c060deacab3cf5fb52aa3a20e885/src/ragas/metrics/_context_precision.py) 和 [Apache-2.0 许可证](https://github.com/vibrantlabsai/ragas/blob/298b68274234c060deacab3cf5fb52aa3a20e885/LICENSE)。这里使用来源链接与设计说明，不将整个项目包装为自己原创的评测框架。

Ragas 的 ID 指标给出的启发：在有稳定证据编号时，可用明确的参考 ID 比较检索结果，不必先付费调用另一个模型当裁判。DocFlow 的标签是 `(document_id, section)`，所以按照**标注章节**比较，并额外处理同一章节被切成多个片段、没有证据标签、查询失败等情况。

原项目已经有 Recall@5、MRR@5；这次保留它们，增加：

- **标注章节精确率**：命中的标注章节数 / 返回的不同章节数。它只衡量相对现有标签的匹配情况，不能认为未标注章节一定不相关。
- **nDCG@5**：相关目标越靠前得分越高；相同目标章节只在第一次出现时得分，保留原始片段位置，不先去重再重排。
- **重复章节槽位**：五个片段里有多少次重复章节，用于定位上下文重复，不能直接称为无效内容。
- **失败分组**：检索异常、空结果、没有命中目标、只命中部分目标；按题号定位，再按预期决策 `answer`、`needs_info`、`out_of_scope` 汇总。
- **历史排名核对**：验证冻结资料 SHA-256、语料指纹、题号覆盖、分区和片段编号；拒绝错误编号、重复片段、扩展上下文冒充 Top5。重算结果不信任旧报告中填写的分数。

这些是检索诊断，不是回答语义评测。人工审核与引用语义复核仍需另外完成。

## 十分钟动手

在项目根目录打开 PowerShell，执行：

```powershell
.\.venv\Scripts\python.exe -m docflow_lab analyze-retrieval --input docs/examples/v2-retrieval-01/retrieval.json --output reports/my-retrieval-diagnostics-01
```

不需要 API 密钥，不需要下载 BGE，也不读取向量索引。输出目录已存在时换一个名称，防止覆盖历史。

打开输出目录的 `report.md` 和 `diagnostics.json`。可先对照[本次实际离线结果](examples/retrieval-diagnostics-01/report.md)：原有 30 题中 26 题有标注证据、合计 29 个目标章节；4 题无标注证据单列。BM25 召回 28/29，nDCG@5 为 0.9385；BGE 为 23/29、0.7250；RRF 为 26/29、0.8536。新诊断是在**保存的历史排名**上重算，没有生成新的真实模型效果。

练习只做三件事：

1. 找到报告里的 `test-15`。三路都没命中标注目标，说明先需要看检索和标签，不能把后续回答失败一概归因为生成模型。
2. 在 `diagnostics.json` 找同一题的 `ranked_chunk_ids` 和 `target_ranks`，对应知识库与冻结题目的 `evidence`，解释“正确章节有没有出现、排第几”。
3. 用自己的话说明：为什么高召回不等于高回答正确率；为什么 BM25 在这个小语料上领先时，应该保留它作默认，而不是因为向量方案更复杂就替换。

完成后再运行有针对性的验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_retrieval_diagnostics.py tests/test_retrieval_v2.py
```

不要改冻结问题或标签来提升数字。若有标签遗漏，记录问题，另建有版本的新数据，不沿用旧盲测表述。

## 如何理解新增指标

假设目标章节是 A、B，实际五个片段对应 X、A、A、B、Y：召回是 2/2；返回四个不同章节，标注章节精确率是 2/4；A 第 2 位、B 第 4 位得到排序分，重复的 A 不重复得分。这比只看“两个目标都找到”更能发现排序与重复。

逐题空排名的精确率为 `null`（没有分母）；汇总 macro 时，有标注但失败/空排名按 0 计入，防止删掉困难题后均值上升。无证据标签题的相关性指标全为 `null`，不能仅因检索返回结果就判定误报。nDCG 使用二值标注，不声称区分轻度/高度相关。

`source_mode_claim` 只是旧报告的模式声明。即使指纹和标签核对通过，也不能据此独立证明历史排名确实来自某次模型或某种运行；被删掉问题文字的公开报告只能核对题号和冻结资料。已有 holdout 已经被观察，本次属于回归，不是新盲测。

代码阅读顺序：`rank_metrics` 计算一题 → `summarize` 保留分母 → `diagnose_report` 核对来源与分组 → `analyze_saved` 写独立报告，均在 `docflow_lab/retrieval_diagnostics.py`。新跑 `evaluate-retrieval` 时，也会在 `retrieval.json` 的 `ranking_diagnostics` 中附上同口径结果。

面试可讲：“我参考 Ragas 的证据 ID 匹配思路，在项目里独立实现了检索诊断。除了检查是否召回目标，还检查排序、重复和失败分组，并明确区分检索分数与回答正确率。”个人是否已经掌握，要以自己运行并能解释结果为准。
