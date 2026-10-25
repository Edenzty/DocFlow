# DocFlow V2 离线验收演示

真实模型API请求0次；离线Agent夹具仅验证工具与工单流程，不代表LLM回答质量。

状态：passed；通过：21/21。

| 检查 | 结果 | 说明 |
|---|---|---|
| background_job_created | 通过 | 通过后台队列创建只读Agent工单。 |
| background_job_completed | 通过 | 在10秒轮询窗口内完成后台离线生成。 |
| awaiting_human_review | 通过 | Agent生成后停在人工审核，未自动审批或提交。 |
| read_only_tool_trace | 通过 | 保存当前工单上下文读取与知识库检索的真实工具执行轨迹。 |
| quote_validation_recorded | 通过 | 仅验证逐条引文存在，不将其当作语义支持证明。 |
| citations_from_actual_evidence | 通过 | 草稿引用来自本次真实BM25召回的片段。 |
| unreviewed_submit_denied | 通过 | 未审核的草稿不能提交。 |
| followup_creates_new_version | 通过 | 用户补充信息产生v2，保留v1及原始问题。 |
| conversation_history_preserved | 通过 | 补充信息作为新消息保存，没有覆盖原问题。 |
| approval_binds_draft | 通过 | 审批绑定当前版本及草稿、证据、质量记录的摘要。 |
| manual_edit_invalidates_approval | 通过 | 人工编辑产生v3，v2审批失效。 |
| manual_edit_resets_quote_quality | 通过 | 人工编辑后不沿用旧的逐条引文校验结果。 |
| old_version_review_denied | 通过 | 旧版本审批不能批准v3草稿。 |
| edited_unreviewed_submit_denied | 通过 | v3必须重新审核后才能提交。 |
| restart_preserves_review | 通过 | 关闭并重建Engine后，SQLite检查点恢复v3审核，不重新生成。 |
| committed_then_timeout | 通过 | 模拟本地回复已写入，但提交响应超时。 |
| restart_retry_recovers | 通过 | 再次重启后只重试失败提交，复用已经存在的同一回复。 |
| repeated_submit_one_reply | 通过 | 重复提交仍只有一条本地模拟工单回复。 |
| idempotency_event_recorded | 通过 | 幂等重放留有审计事件。 |
| three_versions_preserved | 通过 | 原始生成、补充生成、人工编辑三个版本均可追溯。 |
| zero_llm_requests | 通过 | 所有生成明确使用离线Agent客户端，无模型API请求。 |

公开demo.json只保留状态、版本、审批、工具名称和计数，不复制草稿、消息、文档摘录或本地路径。
完整原始报告和SQLite状态保留在本地reports/v2-demo-01，未上传运行数据库，也未修改正常工作台的工单。
