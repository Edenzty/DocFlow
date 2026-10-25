document_id: faq-06
title: FAQ：VS Code上手与工单信息收集
source: docflow-original:faq-06;grounded-in-edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1
version: docflow-kb-v1-2026-10-25
kind: faq
---

# FAQ：VS Code上手与工单信息收集

## VS Code相对导入报错

运行 edgesec_lab/demo.py 出现 attempted relative import with no known parent package，通常是直接执行了包内文件。打开整个edgesec-lab仓库根目录，选择已安装依赖的Python解释器，在终端运行 python -m edgesec_lab demo，或选择 EdgeSecLab: demo 调试配置。无需删除代码里的相对导入，也不建议临时到处修改sys.path。

选择解释器不等于安装依赖；用同一解释器执行 python -m pip install -e ".[dev]" 后再运行。若终端暂时无输出，检查是否停在断点，按F5继续，并等演示结束输出完整JSON。不要单独运行测试文件代替pytest收集。

## 需要追问的工单信息

用户只说“运行不起来”“设备失败”“Agent有问题”，应先询问完整命令、当前目录、Python解释器、错误码或异常最后几行，以及期望操作。接口问题再问方法、路径、key_id、状态码和reason；模型问题问是否显式 --agent、missing_api_key等安全错误码和请求计数。

仅凭一句模糊描述不能给出确定根因。不要索要API Key、设备secret、完整签名、数据库备份或含凭据的环境变量截图。必要时可提供已脱敏request_id和报告中对应证据。

## 知识库覆盖边界

本知识库仅覆盖EdgeSecLab当前v2本机实验的启动、HMAC与授权、防重放、日志审计、只读Agent和评测使用。它没有公司生产设备手册、真实云账号配置、铁路专用设备参数、招聘政策、医疗或理财资料。对库外问题应明确说明未覆盖并请用户补充可信文档，不编造生产操作指令或保证。
