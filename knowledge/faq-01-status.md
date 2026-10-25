document_id: faq-01
title: FAQ：401、403、422和409怎么排查
source: docflow-original:faq-01;grounded-in-edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1
version: docflow-kb-v1-2026-10-25
kind: faq
---

# FAQ：401、403、422和409怎么排查

## 401与403的最小排查

本机EdgeSecLab中，401应先看 reason：missing_or_duplicate_auth_headers 是缺失或重复头，invalid_signature 是摘要不一致，timestamp_out_of_window 是时间超窗。时间超窗时签名可能正确，不能把所有401都归为身份伪造。

403且reason=device_forbidden表示已验证密钥与目标设备归属不符。默认A使用key-a访问device-a，B使用key-b访问device-b。若A读B返回403，优先核对目标路径和密钥编号，不要关闭授权或改数据库把它放行。日志中保留 request_id、verified_key_id、device_id、status_code、reason，避免提供密钥值和签名原文。

## 422后再发为何变409

当签名、时间和归属已通过，非法配置体首次返回422并消耗nonce。原样重发带同一key_id与nonce，第二次会因nonce_reused返回409。修复请求体后应重新构建合法请求、生成新nonce与当前时间、重新签名，而不是继续原样发送旧请求。

采样间隔必须是1到3600的严格整数，0和字符串"20"都不合法。被拒后读取数据库或另发合法读取请求检查最后合法值仍保留；不要只看422就认定状态一定正确。

## 只有访问失败能否诊断

只有“设备访问失败”或“接口不行”不足以定位根因。需要追问HTTP方法、目标路径、返回状态码、reason、使用的key_id、请求发生时间与是否重试；可提供脱敏request_id便于关联。不要索要API密钥、设备密钥或完整认证头。没有这些信息不能断言必然是签名、权限或重放问题。
