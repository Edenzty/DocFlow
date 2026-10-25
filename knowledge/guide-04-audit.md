document_id: guide-04
title: EdgeSecLab v2 日志审计与离线报告
source: edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1:docs/WEEK3_GUIDE.md;docs/V2_GUIDE.md;docs/V2_ASSESSMENT.md
version: edgesec-v2.0-curated-2026-10-25
kind: guide
---

# EdgeSecLab v2 日志审计与离线报告

## 导出与独立审计

serve持续运行时，可以执行 python -m edgesec_lab export-logs --output reports/manual-events-01.jsonl 导出审计记录。离线样例可执行 python -m edgesec_lab audit --logs docs/examples/week3-events.jsonl --output reports/audit-01。导出文件和报告目录都不能覆盖旧结果，重复实验使用新名称。audit只读输入日志，不修改设备配置或原始JSONL。

默认权限表为key-a到device-a、key-b到device-b。自定义环境使用 --policy 指向真实JSON权限映射；不能根据日志声称的身份猜权限。JSONL输入最多10MiB、10000事件；格式错误、无时区时间、重复event_id或超限会明确失败。重复request_id作为审计证据保留，不等于重复event_id。

## 固定规则与证据

固定规则检查连续认证失败、跨设备访问、重复request_id或同一已验证身份的nonce、未经授权的成功操作。连续认证失败阈值是同一声称身份300秒内连续至少五条 auth_status=failed；一次非认证失败事件打断该身份的连续计数。

规则状态分为已拦截、违规操作已生效、待人工确认。4xx支持被拒，违规2xx支持已被接受；配置确实改变还需核对 before_interval 与 after_interval。5xx、日志缺口或重复request_id不能直接证明成功或完全未生效。发现数量不等于攻击次数，同一事件可参与多个发现。

## 程序结论与AI主张

v2 的 operation_assessment 将固定规则结论、Agent独立主张和两者分歧分开展示，不用程序正确答案改写模型输出。设备操作状态为 normal、blocked、succeeded、unknown；已观察到违规成功时保留 succeeded，同时如实标注来源缺口。没有发现且输入非空、处理完整、无已知源缺口和关键权限限制，才可汇总normal。

Agent findings描述设备操作，observations记录不可信文字和数据质量提醒。一段日志写着可疑命令不等于设备操作成功。complete_for_input=true只表示已处理提供的事件，不证明上游未漏日志或输入外没有攻击。

## 离线HTML与引用

report.html自包含，不加载外部脚本和字体，不上传日志。事件区可按文字、HTTP状态、设备和动作筛选；点击证据引用会清除筛选并展开对应事件。动态日志和模型文字统一HTML转义；固定筛选脚本只操作本地DOM，不调用网络、不执行日志文字。

引用能点开仅证明它指向本次输入事件，不能证明自然语言推理正确。人工应核对事件、身份、权限、响应、配置前后值以及覆盖限制；不要把界面显示正常当成生产安全保证。
