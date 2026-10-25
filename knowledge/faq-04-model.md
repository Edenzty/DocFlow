document_id: faq-04
title: FAQ：Agent未启用、缺密钥和预算耗尽
source: docflow-original:faq-04;grounded-in-edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1
version: docflow-kb-v1-2026-10-25
kind: faq
---

# FAQ：Agent未启用、缺密钥和预算耗尽

## 密钥已配置为何没有AI结果

默认 python -m edgesec_lab demo 是离线安全演示，不会因为存在DEEPSEEK_API_KEY就自动调用模型。需要真实Agent时显式增加 --agent。启动该命令的进程必须能读取DEEPSEEK_API_KEY；另一个终端设置的临时变量不会自动传到已运行进程。项目不自动加载.env。

缺密钥时报告Agent状态unavailable、错误码missing_api_key、模型请求0次；独立固定规则报告仍保留。不要把设备密钥当成模型密钥，也不要通过截图展示环境变量值。

## 预算不足与失败不能当作安全

每次EdgeSecLab审计上限为6次模型请求、10次工具调用、单次50条、总计200条记录。截断、预算耗尽、来源缺口或无效JSON会导致不完整或失败；固定规则结果仍可查看，Agent未完成不能被写成“模型确认全部安全”。重复查询同一记录也计入200条。

超时或提供商错误先看安全错误码和实际请求数，避免反复运行整批付费评测。用不带 --agent 的命令确认本机接口正常，并保留失败记录。API请求发生后即使客户端超时，也不能假定零费用。
