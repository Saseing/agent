# NewAgent / 喵喵助手

本地部署的 AI Agent 应用：FastAPI 后端 + Vue3 前端，具备工具调用、代码执行、
双路知识库检索与 SSE 流式输出。

> 本 README 早前描述的是 `hello_agents` 教程 demo，与实际代码不符，已按当前实现重写。

仓库：<https://github.com/Saseing/agent>

## 快速开始

### 从零跑起来（克隆 → 安装 → 配置 → 启动）

```powershell
# 1) 克隆
git clone https://github.com/Saseing/agent.git
cd agent

# 2) 建虚拟环境并装依赖（首次约 5~10 分钟，torch 较大）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3) 配置密钥
Copy-Item .env.example .env
notepad .env            # 至少填 OPENAI_API_KEY；换厂商时同时改 LLM_BASE_URL 与 LLM_MODEL

# 4) 启动
python backend.py
```

打开 <http://127.0.0.1:8001> 即可使用。

**只想先跑起来、不装向量检索那套重的？** 可以跳过 `requirements.txt` 里
「知识库：向量检索」那一段（chromadb / transformers / torch 等）—— 应用照常启动，
只是嵌入后端会自动降级为离线哈希嵌入，检索质量下降。知识库用不上就无所谓。

> ⚠️ 别用 PATH 上的 `python`。Windows 下它常是应用商店占位符，缺 fastapi/langchain
> 等依赖；请用虚拟环境里的解释器（`.\.venv\Scripts\python.exe backend.py`）。
>
> 本项目的开发环境把 venv 放在**项目同级目录**（`D:\python_text\.venv`），
> 所以开发时用的是 `D:\python_text\.venv\Scripts\python.exe backend.py`。
> 你若按上面步骤在项目内建 `.venv`，用哪个都行。

### 环境变量（`.env`）

完整模板见 [`.env.example`](.env.example)（复制成 `.env` 再填值，`.env` 已被 git 忽略）。
最常用的几项：

```env
OPENAI_API_KEY=...                 # 当前 LLM_BASE_URL 对应服务的密钥（变量名沿用 OpenAI 的叫法）
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=1
LLM_BASE_URL=https://api.deepseek.com
TAVILY_API_KEY=...                 # 联网搜索（可选）

# 可选
HOST=127.0.0.1                     # 默认仅回环；设为 0.0.0.0 才会对局域网开放
PORT=8001
CORS_ORIGINS=http://192.168.1.10:8001   # 需要跨机访问时的显式来源白名单
AUTO_EXECUTE_CODE_BLOCKS=0         # 危险开关，见下文「安全」
ALLOW_REMOTE_SETTINGS=0            # 是否允许非本机来源改 API 密钥，见下文「运行时切换 API」
```

### 运行时切换 API（密钥 / 接口地址，免重启）

「设置 → 模型」页可直接改 **接口地址（Base URL）** 与 **API 密钥**，保存即生效，**不用重启后端**。
适合这些场景：密钥欠费/失效要换、换账号、换中转或兼容端点（模型名不变时只改地址即可）。

流程是**先验证、后落盘**（`POST /settings/api`）：

```
提交 → ① 格式校验 → ② 用新配置发一次极小的真实请求探测 →
       ③ 通过才写进程环境 + 重建客户端（立即生效）→ ④ 写回 .env 持久化
```

- **验证不通过就整单拒绝**，原配置继续可用 —— 不会留下"存进去了但用不了"的中间状态。
  探测用 `max_retries=0`，地址写错时立刻失败，不会让人干等三次重试。
- **生效需要重建两处客户端**：`LCtext2.llm` / `LCtext2.llm_with_tools`（LCtext2 内部兜底链）
  和 `backend.llm` / `backend.llm_with_tools`（所有 `/chat` 链路）。**工具清单必须一起重新绑定**
  （`LCtext2.LC_TOOLS`、`all_tools`），否则会变成"模型换了但身上没工具"，Agent 循环静默失效。
  Planner / Judge 每次请求都读 `os.environ`，更新环境变量后自动跟上。
- **密钥永不完整出后端**：界面只回显首尾（如 `sk-1e…abf0`），输入框留空表示不修改。
- **只允许本机调用**：该接口能改密钥，因此默认只放行回环地址（`127.0.0.1` / `::1`）。
  确实要让局域网改，得显式设 `ALLOW_REMOTE_SETTINGS=1` 并自担风险。
- **写 .env 必须 `newline=""`**：Python 文本模式默认读时把 CRLF 归一成 LF、写时又把 LF
  翻译成 `os.linesep`，Windows 上会把整个 `.env` 从 LF 悄悄换成 CRLF（内容看着没变、字节全变）。
  已用字节比对验证：加了 `newline=""` 后，切换再切回来 `.env` 逐字节一致。

```powershell
# 命令行也能直接改（与界面等价）
curl.exe -X POST http://127.0.0.1:8001/settings/api -H "Content-Type: application/json" `
  -d "{\"api_key\":\"sk-新密钥\",\"base_url\":\"https://api.deepseek.com\",\"verify\":true}"
# 只看当前配置（密钥自动打码）
curl.exe http://127.0.0.1:8001/settings/api
```

> 模型名与温度仍是**只读**的，由 `.env` 的 `LLM_MODEL` / `LLM_TEMPERATURE` 决定（改这两项仍需重启）。

## 架构

```
前端 frontend/ (Vue3 SPA, index.html + script.js + styles.css)
        │  POST /chat/stream  (SSE)
        ▼
backend.py (FastAPI)  ── /chat  /chat/stream  /workspaces/*  /workspace/tree  /workspace/file  /kb/*  /files/*  /images/*  /pages/*
        │
        ├── workspaces.py         工作区登记、切换、本机目录浏览
        ├── LCtext2.py            agent_loop：工具调用循环、todo / task 管理
        ├── knowledge_base.py     RAG：分块 + BM25 + 向量 + RRF 融合 + 重排
        │                         提供 search_doc / search_doc_fast / search_doc_quality
        ├── code_executor_tool.py execute_code → 子进程 _exec_worker.py (IPython)
        │                          write_file / edit_file
        ├── page_tool.py          create_page / edit_page / list_pages → 工作区 pages/
        ├── shell_tool.py         run_shell → bash（无则降级 PowerShell）
        └── utils.py              路径白名单、文本提取、上下文压缩、限流器
```

### 工作区（Workspace）

**一个工作区 = 你在本机选定的一个文件夹。** Agent 的文件读写、命令行执行
都以它为根；每个工作区的会话独立保存。侧边栏按工作区分组展示所有会话。

**工作区不是固定列表**——你在界面上选哪个本地文件夹，就动态新建一个工作区。

#### 侧边栏行为（对齐 DSH）

| 行为 | 说明 |
|---|---|
| 分组 | 工作区为一级，会话挂在各自工作区下 |
| 折叠显示 | 每个工作区默认只显示 **5 条**会话；点「显示其余 N 个会话」每次再多放 5 条，全部展开后可「收起」 |
| 排序 | 「最近更新」（默认）或「手动排序」，在右上角 ⚙ 视图选项里切换 |
| 固定 | 固定的会话排在本工作区最前 |
| 归档 | 归档的会话**不混在正常列表**里，需在视图选项里打开「显示已归档」才显示 |
| 搜索 | 点 🔍 跨工作区按标题过滤，命中时自动展开、不做折叠 |
| 空态 | 工作区没会话时提示「暂无对话」；搜索无结果时提示「没有匹配的对话」 |
| 缺失标记 | 工作区目录已被删除时，该行显示红色「缺失」 |

#### 会话与工作区的绑定（重要）

工作区根是**按请求上下文隔离**的（`utils._ws_root`，一个 `ContextVar`）。

这里踩过一个值得记录的坑：最初工作区根是模块级全局变量，配一把可重入锁
看似够了，但实测并发下仍然串味：

```
[ws-scope] enter 深度=1 根=D:\dsh
[ws-scope] enter 深度=2 根=D:\java-demo     ← 第二个请求套在第一个里面
```

原因是两个 SSE 生成器跑在**同一个事件循环线程**上，A 在 `await`（等 LLM）时
B 插入执行 —— 锁对"同一线程内的协作式嵌套"完全无效。A 恢复后用工具时，
全局根已经是 B 的了，表现为**读不到 / 读错文件**。

改用 `ContextVar` 后彻底解决：每个请求任务各持一份，且能自动穿透
`asyncio.to_thread`（工具在线程池里执行也读得到正确的根）。

#### 工作区与 DSH 的关系（已解耦）

本应用运行在 **DSH** 里，DSH 自己也维护一份工作区列表
（`<DSH_HOME>/storages/workspace.json`）。**两者现在互不影响**：

- 本应用的工作区**只**来自 `.workspaces/registry.json`；在应用里新建 / 切换 /
  删除工作区都**不会**写回 DSH；
- **不再**从 DSH 导入：启动时不读取 DSH 的登记文件，侧边栏也没有「同步」按钮，
  `POST /workspaces/sync-dsh` 接口已移除。在 DSH 界面里点「选择工作区目录」
  只影响 DSH 自身，不会改变本应用的当前工作区。

> 历史：早前版本做过**单向导入**（启动时自动同步 + 侧边栏 ↻ 按钮），
> 用于解决「在 DSH 里选了目录、Agent 却读不到」。已按要求移除 ——
> DSH 里的工作区变动不再影响本应用。
> 此前导入进来的工作区仍是普通工作区，需要清理时在侧边栏用 🗑 移除即可
> （只删登记，磁盘上的文件夹原样保留）。

#### 新建工作区

侧边栏右上角 📁+ 打开**应用内的目录浏览器**（可切换盘符、逐层进入、就地新建文件夹）。

> 为什么需要应用内浏览器：网页**无法访问你的本地文件系统**。
> 浏览器文件选择器给的是文件句柄，不是后端能用的路径。
> 因此 `/workspaces/browse` 提供了服务端的目录浏览能力。

切换工作区时，以下内容整体切换：

| 项目 | 行为 |
|---|---|
| Agent 的路径根（`utils.BASE_DIR`） | 切到新文件夹；`read_file` / `write_file` / `run_shell` / `execute_code` 全部跟随 |
| 左侧文件树（`/workspace/tree`） | 以新根重新懒加载 |
| 对话记录 | 按工作区分别存 `localStorage`：`chatSessions::<workspaceId>` |
| 代码执行环境 | 子进程被终止，下次执行在新工作区重建（子进程的 cwd 是启动时确定的） |

**固定不变的**（始终在应用目录下）：`.env` 配置、知识库索引、`uploads/`
（聊天上传缓存）、`pang/`（生成的图表）、`.workspaces/`（工作区登记）。
这样工作区切换不会让上传附件与生成的图表"找不到"。

#### 安全边界

- **不删除你的文件**：移除工作区只删登记（可选清理知识库索引），
  磁盘上的文件夹原样保留——界面里也明确提示这一点。
- **拒绝系统目录**：`C:\Windows`、`C:\Program Files`、`/System`、`/usr` 等
  不允许作为工作区（含大小写变体）。
- **静态服务不放宽**：`/files` 与 `/images` 仍然只服务 `uploads/` 与 `pang/`，
  **不包含任何工作区**。否则把工作区指向含密钥的目录就等于开了下载口。
- **注意**：选定的工作区**可以被 Agent 读写**（这正是它的用途）。
  不要把工作区指向含有敏感凭据的目录。

### 前端页面制作

在对话里说「帮我做一个 XX 页面」，Agent 会用 `create_page` 直接产出页面，
写进**当前工作区**的 `pages/` 子目录；点顶部导航栏右侧的**打开侧边栏**按钮
展开第三栏，再点其中的**「页面预览」**即可实时查看（三栏并排，不遮挡聊天）。

| 工具 | 作用 |
|---|---|
| `create_page(name, content)` | 创建/覆盖页面。不写后缀自动补 `.html`；也可写配套资源（`style.css` / `app.js` / `logo.svg`）或子目录（`demo/index.html`） |
| `edit_page(name, old_text, new_text)` | 精确修改页面。改页面优先用它，别整页重写 |
| `list_pages()` | 列出当前工作区已生成的页面与配套资源 |

设计取舍：

- **页面跟着工作区走**：产物固定在 `<工作区根>/pages/`。切工作区就换一整套
  页面，与「一个工作区 = 用户选定的一个文件夹」的既有模型一致。
- **零构建**：一个页面 = 自包含单文件 HTML（CSS/JS 内联，或引用同目录的
  `.css` / `.js`）。不需要 Node，写完即可预览 —— 与本项目「依赖全部本地化、
  内网可用」的取舍一致。真要做 Vite/React 工程，仍走 `run_shell`。
- **禁止外链**：系统提示词明确要求不引用 CDN。本应用面向离线/内网环境，
  外链加载失败会让页面样式直接崩掉；图标用内联 SVG 或 emoji，
  图表用原生 Canvas/SVG/CSS。
- **附件的访问地址按文件实际位置给**（回复里的附件行、以及正文里被替换成链接的路径）：
  工作区 `pages/` 下的 → `/pages/file/...`（页面预览地址）；`pang/`、`uploads/` 下的
  图片 → `/images/...`、其它文件 → `/files/...`；工作区里**其它位置**的文件没有静态出口，
  就**不给链接**（渲染成不可点的行），宁可不给也不给一个点开 404 的死链 ——
  早前一律给 `/files/{文件名}`，而 Agent 最常产出的页面正好在工作区 `pages/` 里，
  于是附件点开全是 404（实测 `/files/blog.html`=404、`/pages/file/blog.html`=200）。

### 三栏布局与右侧栏（页面预览 / 工作区文件）

顶部导航栏**左右两侧各有一个「打开/收起侧边栏」按钮**（图标互为镜像），
按钮会随对应栏的开合状态高亮，标题在「打开侧边栏 / 收起侧边栏」之间切换：

| 位置 | 控制 | 打开后 |
|---|---|---|
| 顶部左侧 | 左侧边栏（工作区 / 会话 / 文件树） | 宽屏是收窄到 0 再展开；窄屏是抽屉 |
| 顶部右侧 | 第三栏 | 整页变成 **左栏 + 中间对话 + 右栏** 三块 |

**右栏占满整个窗口高度**：它是 `.app-layout` 的直接子元素（与中间栏平级），
不是嵌在中间栏内部 —— 因此不会被中间栏的顶部导航条压在下面；反过来，
那条导航条也只占中间栏的宽度，右边缘停在右栏的左边界。
切到知识库视图时右栏自动隐藏（`v-if="rightPanelOpen && currentView==='chat'"`）。

右侧栏顶部**居中**并排放着两个内容入口，点哪个就显示哪个：

- **页面预览** —— 页面文件列表 + iframe 实时预览（附页面数角标、「新窗口」打开）
- **查看工作区文件** —— 当前工作区目录树（懒加载、展开/折叠）+ 点文件看文本内容

右栏宽度可拖动其左边缘调整，`Esc` 关闭，宽度记在 `localStorage`；
窄屏（≤900px）右栏自动改为贴右浮层（手机上放不下三栏）。

「页面预览」工具条最左侧还有一个**收展按钮**：收起页面列表后，预览会占满整栏高度，
适合看长页面；收起状态同样记在 `localStorage`，刷新后保持。

| 入口 | 数据来源 |
|---|---|
| 页面预览 | `GET /pages`、`GET /pages/file/{path}` |
| 查看工作区文件 | `GET /workspace/tree`、`GET /workspace/file?path=` |

#### 元素级修改：在预览里点选元素

「页面预览」工具条上有一个**箭头图标（编辑页面）**：

1. 点它进入**元素选取模式**（预览页重载一次，后端给页面注入 `picker.js`）；
2. 在预览里移动鼠标：元素被蓝色高亮框圈住，并显示与 DevTools 同款的元素标识与尺寸
   （如 `div.card-title  242 × 62`）；
3. 点击元素：输入框上方出现一个**元素标签**（不影响输入框里的文字）。

   ```
   元素标签 2  [清空]   div#pickme.t-card.t-primary  242 × 62  _t_autopick.html  ×
                        div#pickme2.t-badge          122 × 30  _t_autopick.html  ×
   ```

4. **可以连选多个元素**（每次点选新增一个标签），每个标签右侧的 **×** 单独删除，
   多个标签时左侧有**清空**；
5. 在输入框里写**一段**修改要求（多个标签共用这段要求），发送即可。

发送时每个标签会转成一段自包含的文本块附在消息里，agent 按块定位：

```
【页面编辑】_t_autopick.html
div#pickme2.t-badge  122 × 30
修改要求：把它改成圆角并换成蓝色
```

agent 收到后会用 `read_file` 按元素标识定位、用 `edit_page` 做最小替换；
多段就是多个元素，逐个处理；改完预览会自动刷新。你发出去的那条消息气泡里
也会回显本次引用了哪些元素标签。

行为约定：

- 选取模式下页面自身不可交互（点击/按下被拦截，与 DevTools 一致）；`Esc` 或再点按钮退出。
- 标签在输入框上方，**不占用输入框文字** —— 输入框始终只写你的要求。
- 重复点选同一个元素不会产生重复标签（会提示「该元素已在标签里」）。
- 尺寸是浏览器渲染后的**边框盒**尺寸（与 DevTools 显示的一致），不是源码里的
  `width` / `height`，两者可能不同。
- 标签信息会随会话一起保存，刷新页面后气泡里仍能看到当时引用了哪些元素。

#### 元素选取是怎么实现的（以及两个踩过的坑）

- 预览 iframe 带 sandbox 且**不含 `allow-same-origin`**（安全边界，不能拆），
  父页面读不到它的 DOM。因此选取逻辑必须**注入到页面内部**：后端在
  `?edit=1` 时把 `<script src="/picker.js">` **追加到文档末尾**，由页面内的脚本
  自己监听悬停/点击，再用 `postMessage` 回传选中元素。
- **注入点为什么是「文档末尾」而不是「`</body>` 之前」**：`</body>` 这个字面量在
  页面里可能出现多次（HTML 注释、JS 字符串、模板文本）。早先用
  `rfind("</body>")` 定位，结果把 `<script>` 插进了注释内部 —— 标签既不进 DOM
  也不执行，而且**完全不报错**（实测时靠页面内探针报告 `tagPresent=false` 才定位到）。
  追加到末尾交给 HTML 解析器处理，不受这些假标签影响。
- **父页面如何鉴权**：沙箱文档的 origin 是 `null`（不透明源），不能用 origin 判断
  来源；只接受 `event.source === iframe.contentWindow` 的消息，其他地方发来的
  同类型消息一律丢弃（有测试覆盖：本窗口自己伪造的 pick 不会进输入框）。

**为什么「查看工作区文件」要新增一个读工作区的接口**：`/files` 与 `/images` 是
**对外提供下载**，所以绝不能包含工作区（指向含密钥的目录就等于开了下载口）；
而「查看工作区文件」的用途恰恰是读工作区，因此单独做了 `GET /workspace/file`，
并把它收得比下载接口更紧：

- 只读，只服务**当前**工作区，路径 `resolve()` 后仍须落在工作区根内
  （实测 `C:/Windows/win.ini` 这种盘符重置会被这一层拦下并返回 403）；
- 仅文本：检出 NUL 字节即判为二进制，返回 415；
- 单次上限 256KB，超出截断并标记 `truncated`；
- 拒绝 `.env`、密钥 / 证书类文件名与后缀，以及 `.pyc` / `.log` 等运行产物；
- 前端只用 `{{ }}` 插值渲染（不经过 `v-html`），文件内容里的 HTML 不会被当标签执行。

#### 预览的安全边界

页面是**模型生成的代码**，与 `execute_code` 同级别的信任假设，因此：

- **只服务 `pages/` 子目录**，不放宽整个工作区 —— 否则把工作区指向含 `.env`
  的项目就等于开了下载口（与 `/files`、`/images` 同一个教训）。
- 请求路径经 `resolve()` 后仍要求落在 `pages/` 之内，防 `..` 与符号链接 /
  junction 逃逸；对外提供的后缀用**白名单**，不在表内的类型一律 404。
- 预览 iframe 用 `sandbox="allow-scripts allow-forms allow-modals
  allow-popups allow-downloads"`，**刻意不加 `allow-same-origin`**：
  页面脚本因此拿不到本应用的 `localStorage`（会话记录），也发不出带凭据的
  同源请求。代价是页面内用 `fetch` 取相对资源会被 CORS 拦掉（纯静态页面、
  `<script src>` / `<link>` / `<img>` 这类子资源加载不受影响）。
- **沙箱会挡掉本地存储（实测结论）**：无 `allow-same-origin` 时，页面访问
  `localStorage` / `sessionStorage` 会直接抛错 ——

  ```
  SecurityError: Failed to read the 'localStorage' property from 'Window':
  The document is sandboxed and lacks the 'allow-same-origin' flag.
  ```

  对照组（额外加 `allow-same-origin`）则读写正常。因此系统提示词要求生成的
  页面用 `try/catch` 探测存储并降级到内存变量，别让一次存储访问把整段脚本
  带崩；需要真正持久化时用「新窗口」打开（独立标签页不受沙箱限制）。
  这个取舍是刻意的：页面是**模型生成的代码**，宁可牺牲一个 API，
  也不让它碰到本应用的登录态与会话数据。
- 响应带 `Cache-Control: no-store`：Agent 改完页面，预览立刻是最新的。
- 写入侧拒绝 `..`、绝对路径、盘符与 `.py` / `.exe` / `.ps1` / `.env`
  等后缀 —— 这是「减速带」而非安全边界（模型本来就能用 `write_file` 与
  `run_shell` 做更多事）。

### 知识库的两层结构

| 层 | 存储 | 用途 |
|---|---|---|
| 文档层 | `knowledge_base/kb_store.json` | 保存正文，供 `read_file` 读取 |
| 索引层 | `knowledge_base/chroma/` | 向量 + BM25 索引，供 `search_doc*` 工具检索 |

启动时会自动对账：`kb_store.json` 中有但索引层缺失的文档会被补索引
（`_sync_kb_index`），避免出现「工具说没有文档、提示词却塞着文档」的错位。

### RAG 检索管线

```
提问
 ├─ 查询理解（仅 quality 模式）：改写 / 多查询 / HyDE
 ├─ 多路召回
 │   ├─ 向量检索 —— 索引【父块摘要】
 │   └─ BM25    —— 索引【子块全文】
 ├─ RAG-Fusion（RRF 倒数排名融合，按 chroma record_id 聚合）
 ├─ 语义重排（用父块摘要编码，与索引文本一致）
 └─ 上下文压缩 → 喂给模型【子块全文】
```

**父子块（small-to-big）**：分块时同时产出

- *父块* = `[第N页] + 前两句 + 关键词`，紧凑、语义聚焦 → 用于建索引与检索
- *子块* = 完整正文 → 命中父块后回带全文，保证生成阶段上下文完整

向量检索用摘要（避免长正文"语义稀释"），BM25 用全文（关键词召回更全），
精排用摘要（与索引文本一致，相似度才可比）。子块全文随 metadata 一起存，
命中后无需二次查库。

### 注入提示词的相关性门控

`search_knowledge_base_from()` 决定哪些片段值得塞进系统提示词。
原实现用「关键词是否出现」计分，中文 bigram/trigram 几乎必然命中，
导致任何中文提问（含「你好」）都会注入数千字无关文档。

现为 **BM25 召回 + 「语义相似度 AND 词面重合」双门控**，阈值经实测标定：

| 判别方式 | 结果 |
|---|---|
| 只看 BM25 绝对分 | 闲聊也拿高分，无效 |
| 只看命中 token 比例 | 中文 n-gram 让「今天天气怎么样」达到 0.62，误放 |
| 只看语义相似度 | 「1+1等于几」0.4879 > 真实提问「迟到早退怎么处罚」0.4165，分布重叠 |
| **语义 ≥ 0.45 且 词面重合 ≥ 0.10** | 相关通过 8/9，闲聊误放 0/7 |

标定用例与阈值写在 `backend.py` 的 `KB_*` 常量处，改动前请先跑一组回归。

## 安全说明

这个服务会执行模型生成的代码，请把它当作**可信内网工具**对待。

- **代码执行隔离**：模型代码在独立子进程 `_exec_worker.py` 中运行。
  子进程只继承白名单环境变量，`OPENAI_API_KEY` / `TAVILY_API_KEY` 不会进入子进程；
  超时可真正 kill 子进程（此前用线程实现，死循环杀不掉）。
  AST 静态检查只是「减速带」，不是安全边界。
- **路径白名单**：`utils.is_path_allowed` / `is_path_writable` 使用
  `Path.is_relative_to` 做路径组件级判断，防止 `.../NewAgent_backup` 这类
  同前缀目录绕过。注意：该白名单约束的是 `read_file` / `write_file` / `edit_file`，
  **不约束 `run_shell` 与 `execute_code`**。
- **`run_shell` 没有静态检查兜底（重要）**：shell 命令是纯字符串，
  AST 过滤器解析的是 Python 语法树，**完全看不懂命令行**——
  `rm -rf`、`curl | sh` 这类都不在其视野内。
  该工具的安全边界只有三条：环境变量白名单、60 秒超时、输出截断。
  **它不会阻止命令读写当前用户有权限访问的文件、发起网络请求或下载执行远程脚本。**
  因此只适合可信的本地/内网场景，任何情况下都不要把它暴露给不受信任的用户。
  不需要这个能力时，从 `LCtext2.TOOL_MAP` 和 `backend.all_tools` 里移除 `run_shell` 即可。
- **静态文件服务**：`/files` 与 `/images` 只在 `uploads/`、`pang/` 内提供文件，
  并拒绝 `.env` 等敏感文件名 —— 此前会回退到项目根目录，
  `GET /files/.env` 可直接下载 API 密钥。
- **网络暴露**：默认绑定 `127.0.0.1`。需要局域网访问时显式设置 `HOST=0.0.0.0`
  并配置 `CORS_ORIGINS`，同时轮换密钥。
- `AUTO_EXECUTE_CODE_BLOCKS=1` 会让后端正则抓取模型回复中的 ```python``` 代码块
  并直接执行。这可能执行模型仅用于「讲解」的代码，默认关闭。

**如果 `.env` 曾经随代码分发或提交过版本库，请立即在控制台轮换密钥。**

## 已知限制

- 会话保存在内存中（1 小时 TTL），重启即丢失。
- 语义嵌入模型 `shibing624/text2vec-base-chinese`（约 780MB）需先下载到本地
  HF 缓存，否则退化为离线哈希嵌入（无语义能力，检索质量明显下降）。
  国内网络请走镜像：
  ```powershell
  $env:HF_ENDPOINT="https://hf-mirror.com"
  python -c "from huggingface_hub import snapshot_download; snapshot_download('shibing624/text2vec-base-chinese')"
  ```
  `knowledge_base.py` 已默认设置 `HF_ENDPOINT` 为 hf-mirror；模型**只在本地缓存
  中读取**，不会联网校验，因此启动很快（嵌入加载约 4 秒）。
- **不要**在模块顶层 `import sentence_transformers`。本机环境下该 import 会触发
  原生层访问违例（进程以 `0xC0000005` 直接退出，`try/except` 拦不住）。
  代码改用 `transformers` + 均值池化直接加载同一份权重
  （见 `TransformersEmbedding`）；如需回退到 sentence-transformers，
  必须先经 `utils.sentence_transformers_importable()` 子进程探测。
- 向量维度变化会使旧索引失效：切换嵌入后端后需清空 `knowledge_base/chroma/`
  重新索引（`_sync_kb_index` 会补，但维度不匹配必须先删目录）。
- `search_doc_quality` 的查询改写/多查询/HyDE 会额外调用 LLM，延迟较高。
- 首轮索引一本 570 页 PDF 约需 70 秒（CPU 编码），之后启动直接复用。
- 前端公式渲染不区分代码块：代码里的 `$` 也会被当作公式摘出（已知取舍）。

## 前端依赖

所有前端库已**本地化到 `frontend/vendor/`**（约 2MB，含 KaTeX 与 Font Awesome
字体），页面不再引用任何 CDN。这样做的原因：

1. 离线/内网可用 —— 此前 CDN 不可达时 `marked.setOptions` 会在 `createApp`
   之前抛错，直接白屏；
2. 锁定版本，避免上游变更导致接口失效（如 `marked` v8 移除了 `highlight` 回调）；
3. 不引入第三方域名，缩小供应链面。

| 库 | 版本 | 用途 |
|---|---|---|
| Vue | 3.4.38 | 前端框架 |
| marked | 12.0.2 | Markdown 解析 |
| DOMPurify | 3.2.6 | **HTML 净化（所有 v-html 出口）** |
| KaTeX | 0.16.11 | 数学公式（`trust: false`） |
| highlight.js | 11.9.0 | 代码高亮 |

代码高亮由 `renderMarkdown` 之后的 `highlightCodeBlocks()` 手动完成
（marked 的 `highlight` 回调已在 v8 移除）。

### 静态资源的缓存与版本号（曾经导致整页白屏）

`index.html` 与 `script.js` 是**配套**的：模板里用到的字段必须与 JS 里的 `data` 一致。
浏览器只要沿用了**旧的 `script.js`**、而 `index.html` 是新的，渲染阶段就会抛
`TypeError: Cannot read properties of undefined`，Vue 随即**卸载整棵树** —— 用户看到的是
**全白页面，且屏幕上没有任何提示**（已实测复现：打开「设置 → 模型」即整页白屏）。

三层防护，从根上解决：

1. **资源带版本号**：`GET /` 返回 HTML 时，把本地 js/css 引用改写成 `script.js?v=<文件 mtime>`
   （见 `_versioned_index_html`）。文件一改 URL 就变，缓存**不可能**再串版本。
2. **入口不缓存、资源回源校验**：HTML 带 `Cache-Control: no-store`；
   `script.js` / `styles.css` / `/vendor/*` 由中间件统一加 `Cache-Control: no-cache, must-revalidate`。
   （StaticFiles 默认只给 ETag/Last-Modified、**没有** Cache-Control，浏览器会按
   "启发式新鲜度"直接用缓存 —— 那正是白屏事故的入口。命中缓存只是一个 304，开销可忽略。）
3. **前端兜底**：`app.config.errorHandler` 接住组件错误 —— 先尝试**自动重新加载一次**
   （版本串号这类问题重载即自愈，20 秒内只自动重试一次，避免真 bug 时无限刷新）；
   仍失败则显示红色错误横幅 + 「强制刷新」按钮，并打印 `[UI] 组件错误` 到控制台。

> 注意：Vue 在**根组件**渲染抛错时仍会卸载整棵树，`errorHandler` 无法阻止卸载 ——
> 它保证的是"**不再静默白屏**、并给出可操作的重载入口"；根治靠的是上面第 1、2 条。

### 渲染安全

模型回复里包含联网搜索结果、知识库正文与文件内容，全部视为**不可信输入**。
渲染链路的约束：

- 所有进入 `v-html` 的 HTML 必须先过 `sanitizeHtml()`（DOMPurify）；
- DOMPurify 不可用时降级为纯文本转义，宁可丢格式也不执行脚本；
- KaTeX 以 `trust: false` 渲染，禁止 `\href` / `\url` / `\includegraphics`；
- 公式回退分支对 tex 做 HTML 转义；
- `safeUrl()` 拦截 `javascript:` / `vbscript:` / `file:` / `data:text/html`。

## 目录说明

```
agent/
├── backend.py            FastAPI 服务与全部接口
├── LCtext2.py            Agent 循环与本地工具定义
├── multi_agent.py        Planner / Executor / Judge 闭环
├── knowledge_base.py     RAG 管道与检索工具
├── utils.py              公共工具（路径安全、文本/PDF 提取、上下文压缩…）
├── workspaces.py         工作区登记与切换
├── page_tool.py          页面制作工具（create_page / edit_page / list_pages）
├── file_tool.py          文件读写工具（write_file / edit_file）
├── shell_tool.py         命令行工具（run_shell）
├── code_executor_tool.py 代码执行工具（子进程管理端）
├── _exec_worker.py       代码执行子进程（勿改名/勿移动）
├── frontend/             前端静态资源（本地 vendor，离线可用）
│   ├── index.html  script.js  styles.css  picker.js
│   └── vendor/           Vue / marked / DOMPurify / KaTeX / highlight.js / 字体
├── docs/
│   └── PROJECT_LAYOUT.md 文件分类与存放约定（新增文件该放哪看这里）
├── requirements.txt      依赖清单
├── kb_requirements.txt   知识库文档解析依赖（被 requirements.txt 引用）
├── .env.example          环境变量模板
├── .gitignore
├── README.md
│
├── uploads/  pang/  knowledge_base/  pages/  .tasks/  .workspaces/   ← 运行时数据（已忽略）
└── _local/                                                            ← 本机私有资料（已忽略）
```

| 路径 | 说明 |
|---|---|
| `backend.py` | FastAPI 服务与全部 HTTP 接口 |
| `LCtext2.py` | Agent 循环与本地工具定义 |
| `knowledge_base.py` | RAG 管道与检索工具 |
| `code_executor_tool.py` | 代码执行工具（子进程管理端） |
| `_exec_worker.py` | 代码执行子进程（勿删） |
| `shell_tool.py` | 命令行执行工具 `run_shell`（bash / PowerShell） |
| `page_tool.py` | 页面制作工具 `create_page` / `edit_page` / `list_pages` |
| `workspaces.py` | 工作区登记/切换、本机目录浏览与新建文件夹 |
| `utils.py` | 公共工具 |
| `docs/PROJECT_LAYOUT.md` | **文件分类与存放约定**：每个文件干什么、新文件该放哪、哪些目录不能挪 |
| `frontend/` | 前端静态资源，由后端挂载 |
| `frontend/picker.js` | 预览页里的元素选取器（仅在「编辑页面」模式下由后端注入） |
| `frontend/vendor/` | 本地化的第三方前端库与字体 |
| `requirements.txt` / `kb_requirements.txt` | 依赖清单（后者被前者引用） |
| `.env.example` | 环境变量模板（复制成 `.env` 使用） |
| `uploads/` / `pang/` / `knowledge_base/` / `pages/` / `.tasks/` | 运行时数据，**启动时会自动重建，但不要移动目录本身**（移动会导致应用认不到已有数据） |
| `.workspaces/registry.json` | 工作区登记表（刻意放在应用目录，不放工作区里；含本机路径，已忽略） |
| `_local/` | 本机私有资料：`reference/` 参考文档、`legacy/` 早期示例与废弃代码、`old-charts/` 历史图表。已忽略，不随仓库发布 |

完整的分类原则、每类文件的职责、以及「新增文件放哪」的决策表见
**[docs/PROJECT_LAYOUT.md](docs/PROJECT_LAYOUT.md)**。
