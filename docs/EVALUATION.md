# 评测：把检索、生成和工程性质分别验证

评测材料包括`data/evaluation/development.json`10题、`holdout.json`20题、`freeze_manifest.json`与[冻结协议](../data/evaluation/protocol.md)。知识库与标注文件在真实调用前冻结并验证SHA-256。当前标签是开发者手工构造的小数据，不是外部盲测；首次看过留出结果再调优后应另建新测试集。

实际数字统一放在[RESULTS.md](RESULTS.md)及对应原始报告，本页定义口径，不填写预期效果。

新增[参考Ragas的离线检索诊断](OPEN_SOURCE_IMPROVEMENTS.md)：复用保存的三路Top5，核对冻结语料与标签，重算标注章节精确率、nDCG与失败分组。支持 `analyze-retrieval` 命令，无API请求；未标注的章节不能直接判为不相关。

## 先运行离线检索比较

```powershell
# 不调用真实生成模型；vector需要预先构建
.\.venv\Scripts\python.exe -m docflow_lab build-index
.\.venv\Scripts\python.exe -m docflow_lab evaluate --suite development --method both --output reports/eval-dev-01
.\.venv\Scripts\python.exe -m docflow_lab evaluate --suite holdout --method both --output reports/eval-test-01
```

也可先用`--method keyword`跳过向量依赖。只有真实BGE＋Chroma跑出的结果才标为向量检索实测；测试注入的嵌入替身只验证索引协议。索引缺失或模型加载失败应记录失败，不把关键词结果偷偷替代进去。输出目录使用新名称保留历史。

所有方法使用同一语料、切分和问题。Top5固定，相关证据标注为`document_id＋section`；同章多个片段不重复得分。库外题没有标注证据，不能因为检索器总能返回5个片段就称可回答。

| 指标 | 分子 / 分母 |
|---|---|
| Top5证据召回率 | 命中的标注证据对 / 全部有证据题的标注证据对 |
| 题级完全召回 | 所需标注证据全部命中的题 / 有证据题 |
| 题级任一召回 | 至少一条标注证据命中的题 / 有证据题 |

另可报告每题召回率的算术平均（macro recall）；它与按全部证据对汇总的召回率分母不同，应同时注明。关键词BM25分数和余弦相似度单位不同，不直接比较大小，不当作正确概率。项目不使用未校准的0.5阈值证明问题可回答；如果未来加拒答阈值，只可用开发题确定，记录后冻结，不使用留出标签调参。

## 真实模型评测与40次总预算

```powershell
.\.venv\Scripts\python.exe -m docflow_lab budget
.\.venv\Scripts\python.exe -m docflow_lab evaluate --suite development --method vector --live --output reports/eval-live-dev-01
.\.venv\Scripts\python.exe -m docflow_lab evaluate --suite holdout --method vector --live --output reports/eval-live-test-01
```

命令只有显式`--live`才使用DEEPSEEK_API_KEY。`--method both`只比较两条检索，生成统一使用向量Top5，每题只生成一次；报告`generation_method`明确写出实际来源。`--method keyword`则使用关键词证据生成。全部30题单轮最多30个请求，不是两方法各30次。先比较证据召回，再阅读生成效果；不要不加计算地多轮付费重跑。

预算账本位于根目录`.runtime/model-budget.sqlite3`，所有命令和工作台共用40次上限。每次请求网络前原子预留，超时、无效JSON和提供商错误也计入；不自动返还或无限重试。无密钥失败与已发出的请求要区分；没有usage不能假装零token。模型输入只含问题与实际召回片段，不能包含标签、标准事实或冻结题号。

## 回答与引用

| 检查 | 可以说明 | 不能说明 |
|---|---|---|
| 草稿结构合法 | 字段和decision符合合同 | 所有内容正确 |
| 引用存在性 | 编号属于这一次实际Top5 | 文字结论受到该段支持 |
| 决策匹配 | answer/needs_info/out_of_scope与冻结预期一致 | 追问内容或每条事实必然合理 |
| 人工语义复核 | 在明确样例中检查支持、遗漏与错误 | 自动外推生产准确率 |

`required_facts`与`forbidden_facts`是人工复核提纲，合理同义表达不应因为措辞不同判错。未做语义核查标为pending，不填0或100%；若展示字符串匹配代理指标，须单独注明。需要追问题应索要必要脱敏上下文，不能索要密钥；库外题应承认没有可信依据，不编造生产配置。

离线Mock是摘录替身，能够支持页面、恢复和校验测试，不是LLM效果。离线决策匹配不可包装成真实AI准确率。真实失败纳入总题数分母，保留原题、实际Top5、模型输出、安全错误类别与用量，不删失败后只报成功样本。

## 工程性质的独立验证

自动化测试应覆盖：未审核不能提交、驳回不能提交、旧版本批准不能复用、重启继续审核、提交写成后超时再试仅一条回复、重复点击、无效引用、恶意文档不能操作、预算并发预留不超过40、密钥与认证头不进入结果。

这些性质应核对数据库与实际状态，而不是仅看模型说“我不会提交”。真实系统只保证本机模拟边界，没有集成企业CRM，不宣称跨第三方网络调用也恰好一次。未安装向量依赖造成的skip与通过分别报告。

## 如何读错误案例

先区分检索缺证据、模型没用证据、引用不合法、信息不足判断错误和工程失败，再决定改哪层。比如Top5缺归属段落，不能直接归因模型推理；Top5有正确段落但声称A可以读B，则应保留模型事实错误。查看全部失败和误报，不只挑选漂亮演示。

最终报告至少说明语料与冻结版本、检索配置、分区题数、指标分子分母、是否真实模型、请求数与token、耗时口径、错误、人工复核状态以及尚未覆盖的场景。
