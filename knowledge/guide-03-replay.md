document_id: guide-03
title: EdgeSecLab v2 防重放与事务
source: edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1:docs/WEEK3_GUIDE.md;edgesec_lab/replay.py;docs/ARCHITECTURE.md
version: edgesec-v2.0-curated-2026-10-25
kind: guide
---

# EdgeSecLab v2 防重放与事务

## 时间窗口与重复请求

请求签名时间 X-Timestamp 与服务器当前整数秒的绝对差不得超过300秒，正负300秒均允许，正负301秒拒绝。时间超窗返回401，reason=timestamp_out_of_window；签名可能已经验证，因此401不一定表示伪造签名。等待SQLite写锁后还会重新检查时间。

服务端以 (key_id, nonce) 为唯一键持久登记请求。已使用的同一密钥nonce再次出现返回409，reason=nonce_reused。A与B合法使用相同nonce字符串互不冲突，因为密钥编号不同。request_id 是服务端为每次访问生成的关联编号，同一签名请求重发也会有新的request_id，不能仅靠它防重放。

## 原样重放实验

安全演示先合法写入60，再用新请求写入10，然后原样重发旧的写60请求。预期原样重发409，A最终仍10。四线程同时发送同一写25请求时，预期恰好一个200和三个409。重启服务并复用同一个数据库与密钥后，旧nonce仍返回409。

missing_replay 是独立缺陷实验，故意关闭时间窗口和nonce检查；其中旧60、新10、重放旧60会把配置恢复为60。它不代表主安全服务缺少防重放，serve也不提供关闭保护的命令行选项。

## nonce登记与业务事务

SQLite写事务按 BEGIN IMMEDIATE、重新检查时间、插入nonce、修改配置、写成功审计、COMMIT 顺序执行。唯一约束与写锁防止并发请求同时消费同一nonce。若业务或审计写入失败，nonce和业务更新一起回滚。持久数据库使重启后仍能识别旧nonce；当前实验未实现nonce长期容量清理。

先独立检查nonce是否存在、再另起事务修改配置会有并发竞态。不能用进程内字典替代数据库唯一约束，也不能仅为通过请求而清空nonce表。

## 哪些响应消耗nonce

通过签名、时间窗口和设备归属后，成功GET、POST、PUT会消耗nonce；请求体不合法的422和无测量数据的404也会登记nonce。签名错误、时间超窗、跨设备403不会登记nonce。内部事务失败则回滚该事务内的nonce及业务变化。

因此同一非法配置请求第一次422，原样重发可能409；403被拒后，同一nonce首次用于合法目标仍可能成功。不要把先前未登记的拒绝误判为成功重放。

## 采集时间与业务幂等

collected_at 是测量数据采集时间，与签名时间不同。历史数据可用当前X-Timestamp和新nonce重新签名补传；不能因为采集日期旧就拒绝合法补传。新nonce重新签名被视为新请求，可能新增重复测量记录。防重放不等于业务幂等；网络超时也不证明服务端没有执行，不能盲目换nonce自动重试写请求。
