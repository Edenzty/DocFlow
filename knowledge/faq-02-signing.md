document_id: faq-02
title: FAQ：签名后JSON和路径为何不能改
source: docflow-original:faq-02;grounded-in-edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1
version: docflow-kb-v1-2026-10-25
kind: faq
---

# FAQ：签名后JSON和路径为何不能改

## JSON字节与摘要不一致

EdgeSecLab签名绑定最终发送的原始请求体SHA-256，不是抽象JSON对象。先签名 {"sampling_interval_s":10}，再发送带不同空格或键序的JSON，可能返回401 invalid_signature。应先固定UTF-8请求体字节，基于同一字节生成签名并发送；不要生成签名后再交给另一套json参数重新序列化。

排查时比较本机计算的请求体摘要、方法、原始路径及查询字符串是否一致，不把密钥或签名原文贴入工单。即使同一业务含义，原始字节不同也不是同一签名消息。

## 路径查询与方法必须一致

签名包含大写HTTP方法及原始路径和查询字符串。签好GET后改成PUT、改device-a为device-b、改变原始查询内容，都会改变签名消息。正确做法是对最终目标重新签名；即便重新签名正确，A访问B仍应被设备授权检查以403拒绝。

HMAC保证持钥方签名与消息完整性，不提供内容加密，也不能在密钥泄露后辨认真实操作者。不要把“摘要验证通过”解释为“设备绝对可信”。
