document_id: guide-06
title: EdgeSecLab v2 评测复现与结论边界
source: edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1:docs/V2_GUIDE.md;docs/V2_EVALUATION.md;docs/V2_RESULTS.md
version: edgesec-v2.0-curated-2026-10-25
kind: guide
---

# EdgeSecLab v2 评测复现与结论边界

## 离线规则评测

python -m edgesec_lab evaluate --suite all --output reports/evaluation-all 检查两套共28组合成场景，默认只运行规则，不调用模型。默认不写 --suite 时使用原14组development；新增holdout为14组。输出目录必须新建。单元测试执行 python -m pytest -q，与真实模型评测是不同验证。

输入标签不进入设备数据库、模型摘要或工具结果，每例每轮使用独立只读快照。开发集可以用于修改提示；看过留出集结果再调优后，该留出集就不再是未见测试，下一次泛化评价需新数据。

## 显式真实评测

python -m edgesec_lab evaluate --suite holdout --live --runs 3 --workers 3 --output reports/evaluation-live 会产生真实模型费用。workers并行独立审计会话，不是多Agent协同。每次审计仍最多6次模型请求，不因并行提高单次预算。401、402、403、429或连续传输错误会停止后续新审计，已发出的请求仍可能有费用。

scripts/compare_agents.py 默认仅预检冻结材料和预算，只有 --live 才运行两版比较。必须保存协议、实际请求数、token、耗时、错误和未知用量；不能删失败样本后只报成功比例。

## 当前实测与不能外推的结论

2026-10-24，同一新增14组holdout各运行3次：冻结v1业务状态匹配21/42，v2匹配35/42；v2仍有7次错误。该轮v2共132次模型请求，42次审计全部完成。正常类别安全误报两版均为3/9，没有下降。原14组development单次v2回归为13/14。这些是EdgeSecLab历史实测，不是DocFlow效果。

数据是项目内构造的少量合成场景，不是外部盲测或生产准确率；不能把35/42写成通用安全检测准确率。引用存在、评分状态匹配也不证明文字语义正确。报告应保留误判、提醒噪声与人工语义复核，而不是声称Agent比确定规则更可靠。

## 学习与工程验收

399项自动化测试通过和39项安全演示通过是当前EdgeSecLab工程验证记录，不代表学习者已会手写这些代码。学习者应亲自掌握一条请求的认证授权链、nonce与配置审计事务链、只读工具到证据报告链。历史周报中的118项、17步等数字应注明阶段，不与当前全量结果混用。
