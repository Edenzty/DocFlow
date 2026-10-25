document_id: guide-05
title: EdgeSecLab v2 只读Agent与调用预算
source: edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1:docs/WEEK4_GUIDE.md;README.md;docs/ARCHITECTURE.md
version: edgesec-v2.0-curated-2026-10-25
kind: guide
---

# EdgeSecLab v2 只读Agent与调用预算

## 何时调用真实模型

python -m edgesec_lab demo 默认不调用模型，即使环境中已有密钥。只有显式增加 --agent 才为主安全实例执行真实DeepSeek审计；隔离缺陷实验仍用固定规则报告。已有JSONL可用 python -m edgesec_lab audit --logs docs/examples/week3-events.jsonl --output reports/agent-01 --agent。

DEEPSEEK_API_KEY只从启动进程的环境变量读取，不自动读取.env，不写入代码、日志或GitHub。当前默认模型deepseek-flash，关闭思考；可用DEEPSEEK_MODEL或 --model 覆盖。模型API密钥与本机设备HMAC密钥用途不同，不能混用。真实调用可能产生费用。

## 三个只读工具

Agent仅能提出 query_logs、get_events、get_access_policy 三类工具调用。query_logs按时间、设备、声称或已验证身份、动作、状态等白名单字段筛选并分页；get_events按事件编号核实明细；get_access_policy返回身份到设备列表的映射，不含密钥。

工具使用固定SQL、参数绑定、SQLite只读连接和同一读取快照，不接受任意SQL，没有shell、网络搜索、设备修改或删日志工具。模型只能整理和解释，不能代替接口执行放行，也不会自动封禁设备。

## 6次模型与200条记录

EdgeSecLab每次审计最多6次模型请求、10次工具调用、单次至多50条返回记录、累计200条记录，重复读取也计入。query_logs默认每页25条。工具结果的 has_more 说明仍有未交付记录；get_events 的 missing_event_ids 表示不存在，omitted_event_ids 表示存在但被预算截断。

达到预算、分页截断、来源缺口或模型未看完输入，应标注覆盖不完整，不能把少量正常日志外推为全部安全。这些是EdgeSecLab的每次审计上限，不能当作DocFlow项目的总模型请求预算；两个项目预算独立。

## 失败降级与输出校验

缺少密钥时Agent状态 unavailable、错误码 missing_api_key、模型请求数0。网络超时或提供商错误保存安全错误类别，不写可能泄露密钥的完整异常。无效JSON、编造或未查询的事件引用会要求修正，但仍受既有预算限制；失败或不完整时保留独立固定规则报告，不伪造模型确认安全。

程序检查输出结构、事件存在且确实查询、设备与时间匹配以及基本响应状态支持；这些检查不能保证因果推断、操作者判断或全部语义正确。人工仍要区分facts、inferences、missing_information和next_checks。
