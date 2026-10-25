document_id: guide-02
title: EdgeSecLab v2 身份签名与设备归属
source: edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1:docs/WEEK2_GUIDE.md;edgesec_lab/security.py;edgesec_lab/models.py
version: edgesec-v2.0-curated-2026-10-25
kind: guide
---

# EdgeSecLab v2 身份签名与设备归属

## 认证头与签名原文

设备接口必须带四个认证头，且每个只能出现一次：X-Key-Id、X-Timestamp、X-Nonce、X-Signature。X-Timestamp 是Unix秒数字符串；X-Nonce 为32位小写十六进制；X-Signature 为64位小写十六进制。每个设备使用独立32字节密钥，密钥不能写入工单或报告。

HMAC-SHA256原文依次用换行符连接：EDGESEC-HMAC-V1、密钥编号、大写HTTP方法、原始路径及查询字符串、时间戳、nonce、原始请求体的SHA-256摘要；末尾没有换行。客户端对最终发送的原始字节签名。JSON空格或键顺序变化都可能改变原始字节，不能签名后再用另一种序列化发送。服务端用 compare_digest 比较摘要。

## 身份认证与设备授权

HMAC校验成功后还要执行服务端设备归属检查。默认 key-a 只允许操作 device-a，key-b 只允许操作 device-b；读取与修改都检查。用A的有效签名读取或修改B，返回403，reason=device_forbidden，verified_key_id=key-a。更换请求中的设备编号不会自动获得权限。

缺失认证头、伪造签名、签名后篡改请求体通常返回401。声称身份 claimed_key_id 来自请求，已验证身份 verified_key_id 来自成功验签；不能把声称key-a的伪造请求归因于真实A。签名有效表示持有相应密钥的一方签名，不证明具体操作者身份；HMAC不加密通信内容。

## 配置输入校验

PUT /devices/{device_id}/config 的 sampling_interval_s 必须是严格整数，范围为1到3600，包含两端。0、3601、字符串"20"、布尔值和额外字段都不符合配置模型。签名、时间与归属通过后，请求体不合法会返回422，reason=validation_failed，而不是200。

合法更新后再发送非法请求，要检查HTTP状态、数据库最终值和对应审计事件。非法配置不应把上次合法值重置为默认值；A和B的数据也不应互相影响。比如A先合法改20，再发送0，预期A仍20、B仍60。

## 手工验证归属

终端一执行 python -m edgesec_lab serve。终端二执行 python -m edgesec_lab config device-a --key-id key-a --interval 15，再执行 python -m edgesec_lab config device-b --key-id key-a --interval 99。第二条应返回403，CLI退出码1；用 python -m edgesec_lab config device-b --key-id key-b 读取，B仍应保留原值。

serve默认复用 secrets/device-keys.json 和 data/edgesec-lab.sqlite3。demo则使用独立临时数据库与内存密钥。自定义 --port 或 --keys 时，客户端与服务端必须保持一致。不要删除既有密钥文件作为修复403的常规手段。
