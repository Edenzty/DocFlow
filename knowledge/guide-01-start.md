document_id: guide-01
title: EdgeSecLab v2 本机启动与演示
source: edgesec-lab@4433f8b2c68874a55d00f92b0495f7878def60c1:README.md;docs/WEEK1_GUIDE.md;docs/FIRST_HANDS_ON.md
version: edgesec-v2.0-curated-2026-10-25
kind: guide
---

# EdgeSecLab v2 本机启动与演示

## 运行目录与包入口

EdgeSecLab 是本机合成安全实验，要求在整个仓库根目录运行。Windows 示例目录为 D:\personal development\edgesec-lab。使用 Python 3.11；新环境先执行 python -m venv .venv，再执行 .\.venv\Scripts\python.exe -m pip install -e ".[dev]"。之后使用同一解释器执行 python -m edgesec_lab demo；若使用虚拟环境，则用 .\.venv\Scripts\python.exe -m edgesec_lab demo。

不要单独直接运行 edgesec_lab/demo.py。包内 from .app import create_app 是相对导入，直接运行文件缺少包上下文会报 attempted relative import with no known parent package。应从仓库根目录使用 -m edgesec_lab。VS Code 需要打开整个项目文件夹，并选择实际安装依赖的 Python 解释器。

## 默认演示与版本标记

当前 v2 默认命令 python -m edgesec_lab demo 不需要 DeepSeek 密钥，不调用模型。程序临时启动本机服务，运行39次安全请求以及三个独立缺陷实验，生成报告后关闭服务。主安全实例预期 passed=39、audit_events=39、replay_protection=true。历史第二阶段只有17项；不能拿17项旧输出判断当前版本全部功能。

输出中的 stage=week3、security_mode=week3_secure 是安全机制阶段的兼容标记，project_version 才是当前包版本。工程代码完成不等于学习者已独立掌握全部练习。

## 报告文件与临时服务

按输出的 report_path 打开本次 report.html；output_directory 中还含 summary.json、events.jsonl、lab.sqlite3、audit.json。experiments 子目录隔离保存 missing_auth、missing_authz、missing_replay 三个缺陷实验，不混入39条主安全日志。报告可离线打开，GitHub 通常显示HTML源码，可下载后在浏览器查看。

demo 执行结束后临时服务已关闭。如果需要持续手工访问接口，应另开终端执行 python -m edgesec_lab serve；它默认只监听127.0.0.1:8000。不要把 demo 已结束误当成 serve 正在运行。

## 调试暂停与终端输出

VS Code 可选择已有 EdgeSecLab: demo 运行配置。停在黄色代码行表示断点暂停，按F5继续；完整JSON在演示结束后输出。确认终端命令、解释器和断点状态，再判断是否异常，不要仅凭暂时无输出就关闭安全检查。长时间暂停可能使已签名请求超出300秒窗口，结束调试后重新运行即可。
