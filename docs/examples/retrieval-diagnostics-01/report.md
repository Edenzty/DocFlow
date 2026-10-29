# DocFlow 检索诊断

只校验冻结语料、编号与标签并重算排名；不能独立证明历史运行来源，不能评判回答或语义支持。

本次模型API请求：0。旧报告不覆盖，旧分数不信任，使用冻结标签与实际排名重算。

| 检索 | 有证据题 | micro Recall@5 | Hit@5 | MRR@5 | 标注章节精确率macro | nDCG@5 | 重复章节槽位 |
|---|---:|---:|---:|---:|---:|---:|---:|
| keyword | 26 | 0.9655 | 0.9615 | 0.9423 | 0.2538 | 0.9385 | 19 |
| vector | 26 | 0.7931 | 0.8462 | 0.6923 | 0.2308 | 0.7250 | 26 |
| hybrid | 26 | 0.8966 | 0.9615 | 0.8494 | 0.2474 | 0.8536 | 24 |

标注章节精确率只统计既有目标，不是全部语义相关性；空结果/失败在macro按0计，无证据题不计分。
nDCG按原始片段位置计，重复章节只首个命中得分；未对排名去重后重排。

## 需要查看的案例

| 题号 | 检索 | 原因 |
|---|---|---|
| test-01 | vector | partial_targets |
| test-01 | hybrid | partial_targets |
| test-02 | vector | no_target_retrieved |
| test-14 | vector | no_target_retrieved |
| test-14 | hybrid | partial_targets |
| test-15 | keyword | no_target_retrieved |
| test-15 | vector | no_target_retrieved |
| test-15 | hybrid | no_target_retrieved |
| test-17 | vector | no_target_retrieved |

按预期决策分组的分母、逐题排名编号与重复槽位见diagnostics.json；没有评估模型回答。
