# DocFlow · 技术文档检索与工单处理 Agent

[![Offline verification](https://github.com/Edenzty/DocFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/Edenzty/DocFlow/actions/workflows/ci.yml)

**将技术问题变成带证据、可追问、可审核、可恢复的工单回复。** DocFlow V2包含可调用只读工具的Agent、三路检索对比、逐条主张引文、工单版本历史和持久化后台任务。检索12篇技术文档，经人工批准后写入本地模拟工单系统。运行范围是本机单用户、单进程。

这是可运行的学习项目。默认使用离线模拟模型，模拟结果只验证工程流程；[V2结果与已知问题](docs/V2_RESULTS.md)保存真实工具调用、检索对比和失败案例，[V1历史结果](docs/RESULTS.md)保留对照。代码实现完成和学习者独立掌握分开记录。

```text
后台任务 → 初始Top5 → Agent按需检索/展开/读取上下文 → 主张＋原文引文
追问/人工编辑 → 新版本＋清空审批 → 人工审核 → 明确提交 → 本地模拟回复
                       SQLite检查点/任务状态       超时后复用同一回复
```

新版工作台提供工单、检索实验和知识库三个视图；按下方命令在本机查看。新版截图保留本地，不作为公开仓库附件。

## 五分钟体验

使用 Python 3.11。首次获取项目时，在准备存放代码的目录运行：

```powershell
git clone https://github.com/Edenzty/DocFlow.git docflow-lab
Set-Location -LiteralPath '.\docflow-lab'
```

已有本地项目时，直接进入项目根目录，再执行以下 Windows PowerShell 命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,vector]"
.\.venv\Scripts\python.exe -m docflow_lab demo --output reports/demo-01
.\.venv\Scripts\python.exe -m docflow_lab demo-v2 --output reports/demo-v2-01
.\.venv\Scripts\python.exe -m docflow_lab serve
```

打开 [本机工作台](http://127.0.0.1:8765)。默认关键词检索＋离线Agent流程演示，不使用模型API，不消耗40次请求预算。报告目录使用新名称，避免覆盖已有演示。下载依赖可能需要较长时间；只使用关键词与离线流程时，也可先安装 `.[dev]`，稍后再安装 `.[vector]`。

后续命令可以继续使用完整解释器路径。也可执行 `.\.venv\Scripts\Activate.ps1` 激活后使用 `python`；若PowerShell限制脚本运行，直接使用上面的完整路径，无需修改全局执行策略。VS Code应打开整个仓库，选择`.venv`解释器，从包入口运行，避免直接执行包内文件。

## 工作台怎样操作

1. 输入具体问题，例如“key-a读取device-b配置返回403且reason为device_forbidden，该怎么排查？”先点“仅检索”，核对章节与版本。
2. 点“检索并生成草稿”。获得后台任务编号，完成后查看Agent轨迹与逐条引文；证据来自实际检索/展开的片段池，最多15条。
3. 人工核对建议是否受到原文支持，再批准当前版本；批准后仍需单独点击提交。
4. 勾选“模拟：提交成功后响应超时”，提交后重试，观察同一回复编号，确认只保存一条回复。
5. 在工单列表载入任务；追问或人工编辑都会生成新版本、清除旧审核。后台生成中断后只有明确重试才会再调用模型。

详细操作和代码阅读顺序见[V2上手与架构指南](docs/V2_GUIDE.md)。[V1动手指南](docs/FIRST_HANDS_ON.md)保留作单次RAG基线练习。

## 向量检索与真实模型

```powershell
# 首次显式下载并构建BGE中文CPU向量索引
.\.venv\Scripts\python.exe -m docflow_lab build-index

# 比较两种检索；默认仍不调用真实生成模型
.\.venv\Scripts\python.exe -m docflow_lab evaluate --suite all --method both --output reports/eval-offline-01

# 查看整个项目的共享模型预算
.\.venv\Scripts\python.exe -m docflow_lab budget

# V2三路检索比较；不调用生成模型
.\.venv\Scripts\python.exe -m docflow_lab evaluate-retrieval --suite all --output reports/retrieval-v2-01
```

向量模型为`BAAI/bge-small-zh-v1.5`，权重约96MB，首次构建需网络与CPU时间；依赖和缓存另占空间。后续检索使用本机缓存，索引不可用时明确报错，不暗中换成关键词结果。可用`DOCFLOW_EMBEDDING_MODEL_PATH`指定兼容的本地模型目录；改变模型或语料后需重新构建索引。

`--method both`比较两种Top5，生成统一使用向量证据，每题只生成一次；`generation_method`在报告中注明。它不是给两种生成结果分别打分，也不会把每题请求翻倍。

真实DeepSeek密钥只从当前进程的`DEEPSEEK_API_KEY`读取，不加载`.env`、不写进文件。启动`.\.venv\Scripts\python.exe -m docflow_lab serve --live`后，还需在页面勾选真实模型；普通`serve`禁止付费调用。真实评测用`evaluate ... --live`显式开启，例如：

```powershell
.\.venv\Scripts\python.exe -m docflow_lab evaluate --suite development --method vector --live --output reports/eval-live-dev-01
```

**本项目真实请求总上限40次，失败与超时尝试也计入。** 交互、开发和留出评测共享`.runtime/model-budget.sqlite3`，不是每次命令重新获得40次。不要删除预算账本或复制项目来重置已使用次数。默认模型`deepseek-flash`，固定官方HTTPS端点；无密钥或调用失败不会伪装成真实成功。

## 实现与信任边界

| 能力 | 实现 |
|---|---|
| 知识库 | 6篇EdgeSecLab指南整理版＋6篇原创FAQ，保留来源、章节、版本与稳定片段编号 |
| 检索 | BM25、BGE＋Chroma、可解释RRF融合；独立Top5与同章节邻窗扩展 |
| Agent | DeepSeek原生工具调用；3次请求/6次只读工具/15条证据上限；保留单次RAG基线 |
| 草稿 | 每条主张绑定实际原文引文；只验证存在性，人工判断语义支持 |
| 人工审核 | 审批绑定工单、草稿版本与内容哈希；新版本清除旧审批 |
| 工作台 | 工单队列筛选、追问对话、版本快照、人工编辑与统计 |
| 恢复 | LangGraph＋SQLite检查点；持久化后台任务，中断后明确重试 |
| 幂等 | 本地SQLite事务与唯一约束；提交成功后超时重试复用同一回复 |
| 页面 | FastAPI＋原生HTML/CSS/JavaScript；查看证据、状态、审批与调用记录 |

30题检索回归中，BM25、BGE、RRF的章节micro Recall@5分别为96.55%、79.31%、89.66%。因此默认仍是BM25。既有留出集已被观察，本次是回归，不能当新盲测；融合没有超过最佳基线。逐题排名与失误见[V2检索报告](docs/V2_RETRIEVAL.md)。

模型没有审核或提交工具。知识库和用户文字不能替代人工审批；本地提交工具不对接真实客服、企业账号或设备。服务仅面向本机单用户，无登录权限系统，不能直接部署为公网多用户产品。引用真实存在不等于回答语义正确，需要人工核对。[架构与限制](docs/ARCHITECTURE.md)说明了哪些性质由程序保证，哪些仍依赖判断。

## 验证与学习

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

离线测试使用模型替身，不访问真实模型；未安装向量依赖的环境会跳过相应Chroma测试，不能把跳过写成已通过。冻结数据为10条开发题＋20条留出题，检索召回、引用存在性、决策匹配和人工语义支持度分开记录。详见[评测说明](docs/EVALUATION.md)、[冻结协议](data/evaluation/protocol.md)和[实际结果](docs/RESULTS.md)。

建议亲自掌握三条链路：**文档如何被检索、答案如何引用证据、审核后的任务如何可靠提交。** 每天约90分钟，三周约32小时是有协助的开发预算，不是从零独立掌握的保证。原始范围、三周节奏和验收标准见[项目计划](docs/PROJECT_PLAN.md)。

知识内容以EdgeSecLab提交`4433f8b2c68874a55d00f92b0495f7878def60c1`为固定来源；两个仓库独立，保留原项目。模型和框架用法参考[LangGraph中断](https://docs.langchain.com/oss/python/langgraph/interrupts)、[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[Chroma Python客户端](https://docs.trychroma.com/reference/python/client)、[BGE官方模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)。
