# 项目文件分类与存放约定

这份文档回答两个问题：**每个文件是干什么的**、**新东西该放哪里**。
（README 里的「目录说明」是速查表，这里是完整版。）

分类的第一条原则是：**源码、配置、运行时数据、本机私有资料，四者不混放**。

---

## 一、源码（进仓库）

### 1. 服务与 Agent 核心

| 文件 | 行数 | 职责 |
|---|---|---|
| `backend.py` | ~2770 | FastAPI 应用：全部 HTTP 接口、SSE 流式对话、工具在服务端的绑定（`all_tools`）、静态资源与页面/图片服务、设置接口 |
| `LCtext2.py` | ~363 | Agent 循环本体（`agent_loop`）：多轮工具调用、todo 提醒、工具结果压缩；本地工具的定义与 `TOOL_MAP` |
| `multi_agent.py` | ~243 | Planner / Executor / Judge 闭环：计划生成、评审打分、重试控制 |
| `workspaces.py` | ~533 | 工作区登记与切换、本机目录浏览、新建文件夹 |
| `utils.py` | ~765 | 公共工具：路径与安全校验、文本/PDF 提取、历史压缩、速率限制器等 |

### 2. 工具（Agent 能调用的能力）

| 文件 | 职责 |
|---|---|
| `page_tool.py` | 前端页面制作：`create_page` / `edit_page` / `list_pages` |
| `file_tool.py` | 文件读写：`write_file` / `edit_file` |
| `shell_tool.py` | 命令行执行 `run_shell`（bash 语法自动降级为 PowerShell） |
| `code_executor_tool.py` | Python 代码执行工具（子进程管理端、超时与资源限制） |
| `_exec_worker.py` | 代码执行的**子进程脚本**，由 `code_executor_tool.py` 按路径拉起。**勿改名、勿移动**，否则 `execute_code` 直接失效 |

### 3. 知识库

| 文件 | 职责 |
|---|---|
| `knowledge_base.py` | RAG 管道：文档切分、向量/BM25 双路检索、RRF 融合、重排；嵌入后端三级降级（transformers → sentence-transformers → 离线哈希） |

### 4. 前端（`frontend/`，由后端挂载）

| 路径 | 职责 |
|---|---|
| `frontend/index.html` | 单页结构与 Vue 模板 |
| `frontend/script.js` | 全部前端逻辑（Vue 应用、SSE 消费、设置面板、页面预览与元素选取） |
| `frontend/styles.css` | 样式与配色主题（浅色/深色变量集中在文件开头） |
| `frontend/picker.js` | 预览页里的元素选取器，仅在「编辑页面」模式下由后端注入到被预览的页面里 |
| `frontend/vendor/` | 本地化的第三方库与字体（Vue、marked、DOMPurify、KaTeX、highlight.js、FontAwesome）。**面向离线/内网，禁止改成 CDN 外链** |

---

## 二、配置与文档（进仓库）

| 文件 | 说明 |
|---|---|
| `README.md` | 项目说明：快速开始、架构、各功能设计与踩坑记录 |
| `docs/PROJECT_LAYOUT.md` | 本文件——文件分类与存放约定 |
| `requirements.txt` | 依赖清单，按用途分组并注明"缺了会降级"还是"缺了会报错" |
| `kb_requirements.txt` | 知识库的文档解析依赖，被 `requirements.txt` 用 `-r` 引用 |
| `.env.example` | 环境变量模板（复制成 `.env` 使用） |
| `.gitignore` | 忽略规则：密钥、运行时数据、本机私有资料 |

> **`.env` 永远不进仓库。** 它含真实密钥，已在 `.gitignore` 首行忽略。
> 如果哪天它被提交过，立即去控制台轮换密钥。

---

## 三、运行时数据（不进仓库，但**不能乱动**）

这些目录由程序在运行中读写，**位置由 `utils.py` 相对项目根解析**。
把它们挪走 = 应用认不到已有数据（旧上传、旧索引、工作区列表都会"消失"）。

| 目录 | 内容 | 删了会怎样 |
|---|---|---|
| `uploads/` | 用户上传的文件与图片 | 旧消息里的附件失效 |
| `pang/` | 生成的图表（`/images/<名字>` 就是从这里取的） | 旧消息里的图片变裂图 |
| `knowledge_base/` | 向量库与索引缓存 | 需要重新索引文档（耗时） |
| `pages/` | 当工作区就是应用目录时，Agent 生成的页面落在这里 | 产物丢失（可在界面里重新生成） |
| `.tasks/` | 持久化任务（`task_create` 等） | 任务列表丢失 |
| `.workspaces/registry.json` | 工作区登记表，**含本机绝对路径** | 工作区列表需要重新添加 |

上面前五个目录在应用启动时都会 `mkdir(exist_ok=True)` 自动重建，所以**清空内容**是安全的，
**移动目录本身**才是危险的。

---

## 四、本机私有资料（`_local/`，不进仓库）

统一入口，避免这些"只属于这台机器"的东西散落在项目根：

| 路径 | 内容 | 为什么留着 |
|---|---|---|
| `_local/reference/` | 参考文档（如第三方手册 PDF） | 可能涉及版权，不宜公开；本机查阅方便 |
| `_local/legacy/` | 早期 `hello_agents` 示例与废弃代码（原 `tests/`、`examples/`、`deprecated/`） | 与当前实现无关，但可能还有参考价值 |
| `_local/old-charts/` | 历史生成的图表 | 旧会话里的图片附件仍指向这些文件名，删掉会变裂图 |

`_local/` 整目录已被 `.gitignore` 忽略，可以放心往里塞任何本机东西。

---

## 五、新增文件该放哪（决策表）

| 你要加的东西 | 放哪里 |
|---|---|
| 一个新的 Agent 工具 | 项目根，命名 `<用途>_tool.py`，然后在 `LCtext2.py` 的 `TOOL_MAP` 与 `backend.py` 的 `all_tools` 里各注册一次（**两处都要**，否则"模型能调用、后端找不到工具"） |
| 一个新的 HTTP 接口 | `backend.py`（当前所有接口都在这里；若将来拆分，按 `/chat`、`/workspaces`、`/pages` 分文件） |
| 前端改动 | `frontend/`（静态文件，改完刷新页面即生效；后端已给资源加了版本号，不会串缓存） |
| 一份说明文档 | `docs/` |
| 一个启动/运维脚本 | `scripts/`（当前还没有，需要时新建） |
| 测试 | 新建 `tests/`（记得把 `.gitignore` 里的 `_local/` 下方注释所述的那行删掉——目前 `_local/legacy/tests/` 是旧的、无关的脚本） |
| 只在本机用的资料 | `_local/`（参考文档放 `reference/`） |
| 临时验证脚本 | 系统临时目录，不要留在项目里（`git status` 保持干净） |

---

## 六、不要做的事（都踩过坑）

- **不要移动 `_exec_worker.py`**：`code_executor_tool.py` 按路径拉起它。
- **不要用 PowerShell 的 `Get-Content`/`Set-Content` 处理 UTF-8 文件**：Windows PowerShell 5.1
  会按 GBK 解码，中文注释变乱码、还可能把忽略规则吃掉（本项目真发生过：`pages/` 那行被并进注释，
  导致 4 个产物被误加进 git 索引）。
- **不要把 `.env` 提交**，也不要把真实密钥写进 README 或 `.env.example`。
- **不要改 `requirements.txt` 里"知识库：向量检索"那组的分组注释**：它标明了缺这些包时会降级而不是启动失败，
  这是为"只想快速跑起来"的人准备的。
- **不要动运行时数据目录的位置**（见第三节）。
