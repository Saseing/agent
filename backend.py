"""
FastAPI 后端服务 - 封装 LCtext2.py 的 Agent 逻辑
启动命令: python backend.py
访问地址: http://localhost:8001
"""

import uuid
import os
import re
import sys
import time
import json
import base64
import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from langchain_tavily import TavilySearch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("NewAgent.backend")

# 确保当前目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent))

# 加载 .env（必须在导入 LCtext2 之前，确保环境变量就绪）
import dotenv
dotenv.load_dotenv(Path(__file__).parent / ".env")

# 导入公共工具
from utils import (
    BASE_DIR, APP_DIR, UPLOADS_DIR, CHART_OUTPUT_DIR, KB_DIR, TASKS_DIR,
    SERVABLE_DIRS, SERVABLE_DENY_SUFFIXES, SERVABLE_DENY_NAMES,
    get_base_dir as utils_get_base_dir,
    set_base_dir as utils_set_base_dir,
    workspace_scope,
    extract_text as _extract_text,
    is_valid_content,
    trim_session_history,
    compress_tool_result,
    build_conversation_memory,
    extract_tool_context,
    extract_file_content,
    FILE_PATH_PATTERN,
    MAX_UPLOAD_SIZE_BYTES,
    DEFAULT_RATE_LIMIT_MAX,
    DEFAULT_RATE_LIMIT_WINDOW,
    RateLimiter,
    maybe_compress_agent_context,
)

from multi_agent import (
    PlannerAgent, JudgeAgent,
    MAX_JUDGE_RETRIES, JUDGE_PASS_SCORE, ENABLE_MULTI_AGENT,
    worth_multi_agent, build_llm_config_from_env,
)

# 从 LCtext2 导入核心组件（此时 get_answer() 已被注释，不会触发交互循环）
from LCtext2 import (
    llm,
    agent_loop,
    read_file, execute_code, reset_code_environment, write_file, edit_file, run_shell,
    todo, task_create, task_list, task_get, task_update,
    TOOL_MAP,
    _extract_base_system,
    _rebuild_chain,
)
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from code_executor_tool import get_executor
from page_tool import (
    create_page, edit_page, list_pages,
    PAGES_DIR_NAME, ALLOWED_PAGE_SUFFIXES,
)

# ── FastAPI 应用 ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：启动时恢复工作区并补齐知识库索引，退出时回收子进程。"""
    # 恢复上次的工作区 —— 知识库索引路径与工作区相关，必须在索引之前。
    #
    # 【刻意不与 DSH 关联】工作区只认本应用的 .workspaces/registry.json。
    # 此前启动时会从 <DSH_HOME>/storages/workspace.json 导入工作区，
    # 并在 DSH 侧多一个「同步」入口 —— 已按要求移除：在 DSH 里新建/切换
    # 工作区不再影响本应用。
    try:
        import workspaces as _ws_startup

        restored = _ws_startup.restore_active_on_startup()
        if restored:
            logger.info("已恢复工作区: %s (%s)", restored["name"], restored["path"])
        else:
            logger.info("暂无已登记工作区，使用应用目录作为工作区: %s", utils_get_base_dir())
    except Exception as e:
        logger.error("恢复工作区失败: %s", e)

    try:
        result = _sync_kb_index()
        if result.get("added"):
            logger.info("启动索引自愈完成，补齐 %d 篇文档: %s", len(result["added"]), result["added"])
        elif result.get("errors"):
            logger.warning("启动索引自愈存在错误: %s", result["errors"])
    except Exception as e:
        logger.error("启动索引自愈失败: %s", e)

    # 会话清理后台任务（原先的 threading.Timer 只触发一次，等于从不清理）
    cleanup_task = asyncio.create_task(_session_cleanup_loop())

    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            from code_executor_tool import _shutdown_executor
            _shutdown_executor()
        except Exception:
            pass


app = FastAPI(title="喵喵助手 API", lifespan=_lifespan)

# 【安全】CORS 收窄：默认只允许本机来源。
# 需要从局域网访问时，在 .env 中设置 CORS_ORIGINS（逗号分隔）显式放行，
# 例如 CORS_ORIGINS=http://192.168.1.10:8001
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_env:
    allow_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    allow_origins = [
        "http://localhost:8001", "http://127.0.0.1:8001",
        "http://localhost:3000", "http://127.0.0.1:3000",
    ]

# 注意：allow_credentials=True 与 allow_origins=["*"] 组合是被浏览器禁止的，
# 早期版本的 ["*"] 实际既不安全也不生效，这里改为显式来源列表。
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ── 会话存储（内存） ───────────────────────────────────────────────────────────
# sessions[session_id] = {"messages": [...], "last_access": timestamp}
SESSION_TTL_SECONDS = 3600  # 1 小时会话超时
sessions: dict[str, dict] = {}

import threading
_sessions_lock = threading.Lock()

def _cleanup_expired_sessions():
    """清理过期会话，防止内存泄漏。"""
    now = time.time()
    expired = []
    with _sessions_lock:
        for sid, data in sessions.items():
            if now - data["last_access"] > SESSION_TTL_SECONDS:
                expired.append(sid)
        for sid in expired:
            del sessions[sid]
    if expired:
        logger.info("清理过期会话: %d 个 (%s)", len(expired), expired)
    return len(expired)


async def _session_cleanup_loop(interval_seconds: int = 300):
    """
    周期性清理过期会话。

    修复：早期版本用 threading.Timer(300, ...) 启动，但 Timer 只触发一次，
    等于启动 5 分钟后清理一次、之后再也不会清理，过期会话会永久驻留内存。
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            _cleanup_expired_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("会话清理任务异常: %s", e)

# ── API 速率限制器（基于 IP 的滑动窗口） ───────────────────────────────────────
_rate_limiter = RateLimiter(max_requests=DEFAULT_RATE_LIMIT_MAX, window_seconds=DEFAULT_RATE_LIMIT_WINDOW)


async def rate_limit_dependency(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.check(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请稍后再试。每 {_rate_limiter.window_seconds} 秒最多允许 {_rate_limiter.max_requests} 次请求。"
        )

# ── 上传文件存储目录 ────────────────────────────────────────────────────────────
UPLOADS_DIR.mkdir(exist_ok=True)

# ── 图表保存目录 ────────────────────────────────────────────────────────────────
CHART_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 知识库存储 ─────────────────────────────────────────────────────────────────
KB_DIR.mkdir(exist_ok=True)
KB_DB_PATH = KB_DIR / "kb_store.json"

# 知识库 JSON 缓存：kb_store.json 含长文档正文（当前约 400KB），
# 每个请求都读盘 + json.loads 是纯浪费。按 mtime 判断失效。
_kb_cache: dict = {"mtime": None, "data": None}


def _load_kb():
    if not KB_DB_PATH.exists():
        return {"documents": []}
    try:
        mtime = KB_DB_PATH.stat().st_mtime_ns
    except OSError:
        mtime = None

    if _kb_cache["data"] is not None and mtime is not None and _kb_cache["mtime"] == mtime:
        return _kb_cache["data"]

    try:
        with open(KB_DB_PATH, 'r', encoding='utf-8') as f:
            kb = json.load(f)
        _kb_cache["mtime"] = mtime
        _kb_cache["data"] = kb
        return kb
    except Exception as e:
        logger.error("知识库加载失败: %s", e)
        # 解析失败时退回上次成功的缓存，避免把知识库整体"清空"
        if _kb_cache["data"] is not None:
            return _kb_cache["data"]
    return {"documents": []}

def _save_kb(kb):
    tmp_path = KB_DB_PATH.with_suffix('.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        tmp_path.replace(KB_DB_PATH)
        # 主动刷新缓存，避免下一次读请求因 mtime 精度问题拿到旧数据
        try:
            _kb_cache["mtime"] = KB_DB_PATH.stat().st_mtime_ns
            _kb_cache["data"] = kb
        except OSError:
            _kb_cache["mtime"] = None
            _kb_cache["data"] = None
        logger.debug("知识库已保存, 文档数=%d", len(kb.get("documents", [])))
    except Exception as e:
        logger.error("知识库保存失败: %s", e)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

# 注：_extract_text 已从 utils 导入（extract_text as _extract_text）

# ── 请求体模型 ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    # 工作区作用域（相对当前工作区根的路径，如 "frontend/script.js" 或 "uploads"）
    # 留空表示整个工作区。用于告诉模型这一轮在哪个文件/目录下工作
    scope: Optional[str] = None
    # 该会话所属的工作区 id。每个会话记住自己的工作区，
    # 避免并发请求切换全局根目录时互相踩（见 utils.workspace_scope）
    workspace_id: Optional[str] = None

    def validate_message(self):
        if not self.message or not self.message.strip():
            raise ValueError("消息内容不能为空")
        if len(self.message) > 100000:
            raise ValueError(f"消息内容过长（{len(self.message)} 字符），最大允许 100000 字符")

# ── 系统提示词 ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_BASE = (
    "你是一个智能AI助手。当用户提到阅读、分析文件或询问文件内容时，请使用 read_file 工具读取文件。"
    "当用户要求制作图表、可视化数据、数据分析时，请使用 execute_code 工具执行 Python 代码。"
    "【绝对禁止规则 - 最高优先级】："
    "- 当你想要执行任何 Python 代码时，你必须使用 execute_code 工具，绝对不能在回复文本中输出代码块！"
    "- 不要用 ```python ... ``` 代码块的形式展示代码——直接把代码作为 execute_code 工具的参数传入"
    "- 违反此规则将导致代码无法执行、图表无法生成，这是最严重的错误"
    "- 正确做法：调用 execute_code(code='...完整代码...') 而不是在文本中写代码"
    "你可以编写任意 matplotlib 代码来生成图表（折线图、柱状图、饼图、散点图等），例如："
    "plt.figure(figsize=(10, 6)); plt.plot(x, y); plt.title('标题'); plt.grid(True)"
    "图片会自动保存，无需手动调用 plt.savefig 或 plt.close。"
    "已预导入的库: pandas, numpy, matplotlib.pyplot, os, json, pathlib, time"
    "变量在多次调用之间保持，类似 Jupyter Notebook 环境。"
    "代码执行有 30 秒超时限制，请勿编写死循环。"
    "read_file 工具只能访问工作目录内的文件。"
    "当用户要求创建新文件时，请使用 write_file 工具。"
    "当用户要求修改已有文件中的特定内容时，请使用 edit_file 工具。"
    "【页面制作 - 重要】当用户要求做网页/前端页面/H5/落地页/仪表盘/网页小工具时，"
    "请使用 create_page 工具把页面写进当前工作区的 pages/ 目录，用户在左侧边栏「页面」面板里能实时预览。"
    "改已有页面用 edit_page（只替换要改的那段，不要整页重写），查看做过哪些页面用 list_pages。"
    "【页面制作规范】页面必须是自包含的单文件 HTML：以 <!DOCTYPE html> 开头，"
    'head 里必须有 <meta charset="utf-8"> 与 viewport，CSS 写在 <style> 里、JS 写在 <script> 里。'
    "绝对不要引用 CDN 或任何外网资源 —— 本应用面向离线/内网环境，外链加载失败会让页面样式直接崩掉；"
    "图标用内联 SVG 或 emoji，图表用原生 Canvas/SVG/CSS 实现，字体用系统字体栈"
    "（如 -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif）。"
    "用户没指定风格时，默认做成简洁现代的浅色卡片式布局，并保证窄屏下不横向溢出。"
    "页面完成后，把预览地址原样写在回复里，并提示用户在顶部导航右侧「打开侧边栏」按钮展开的预览面板中查看。"
    "【地址格式】页面的地址是 /pages/file/xxx.html。回复里**只给这个预览地址**，"
    "并且**绝对不要出现任何 /files/ 开头的链接或「点击下载」链接** —— "
    "/files 只服务上传文件与图表目录（uploads/、pang/），对页面一律返回 404；"
    "页面在应用内预览即可，本来也不需要下载链接。"
    "【沙箱限制 - 必须遵守】预览面板用带 sandbox 的 iframe 承载页面（出于安全刻意不带 "
    "allow-same-origin），因此页面里直接访问 localStorage / sessionStorage / document.cookie "
    "会抛 SecurityError 并中断脚本 —— 实测确认过。所以页面必须用 try/catch 兜底："
    "先探测 `var store = (function(){ try { localStorage.setItem('__t','1'); "
    "localStorage.removeItem('__t'); return localStorage; } catch (e) { return null; } })();`，"
    "store 为 null 时降级到内存变量（页面关掉就丢，但功能可用），并在界面上说明「点『新窗口』"
    "打开可持久保存」。仓储/存储相关的 API 一律照此处理，绝不让它把整段脚本带崩。"
    "【元素级修改 - 用户点选元素发来的指令】预览面板里有「编辑页面」按钮，用户点选预览页面里的元素后"
    "会生成「元素标签」；用户写好自己的要求并发送时，每个标签会转成一段定位块，形如：\n"
    "【页面编辑】pages/todo.html\n"
    "div.card-title  220 × 24\n"
    "修改要求：把标题改成蓝色\n"
    "其中第一行是标记与**页面路径**，第二行是**元素标识**（与浏览器 DevTools 悬停提示一致：标签名#id.类名）"
    "和该元素在页面里的**像素尺寸**，第三行「修改要求：」后面才是用户的具体诉求。"
    "**一条消息里可能出现多段这样的块**（用户一次点了多个元素），每段的「修改要求」内容相同 —— "
    "这说明要改的是多个元素，请对每一段分别处理，不要只改第一个、也不要漏掉后面的。"
    "收到这种消息时按以下顺序处理："
    "① 用 read_file 读该页面，按元素标识（标签名 / 类名 / id）在源码里定位到对应元素；"
    "② 同名元素有多个时，结合用户的描述、元素尺寸与页面结构判断是哪一个，确实拿不准就在回复里说明你改的是哪一个；"
    "③ 用 edit_page 做**最小替换**：只改与该元素直接相关的结构或样式，绝不整页重写、不顺手改用户没提到的部分；"
    "④ 如果该元素的样式其实定义在同目录的 .css 文件里，就改那个文件；"
    "⑤ 改完提示用户预览面板会自动刷新，可直接查看效果；多个元素时逐一说明改了哪些。"
    "注意：块里的尺寸是浏览器渲染后的实际尺寸，源码里的 width/height 可能不同，不要把它们混为一谈。"
    "当用户需要运行命令行指令（运行脚本、git、pip、npm、列出/查找/统计文件、查看系统信息）时，请使用 run_shell 工具。"
    "run_shell 支持 bash（未安装 bash 时自动降级为 PowerShell），单条命令超时 60 秒；"
    "在只有 PowerShell 的 Windows 环境下，常见 bash 命令（ls -la / cat / rm -rf / grep / pwd 等）"
    "会被自动翻译成 PowerShell 等价语法，但复杂 bash 管道/高级特性不保证兼容，"
    "此时请直接写 PowerShell 语法（Get-ChildItem / Get-Content / Remove-Item / Select-String 等）。"
    "不要执行需要交互输入的命令，必要时加 -y 等非交互参数。"
    "对于多步骤任务，请使用 todo 工具规划任务列表并跟踪进度。"
    "开始执行前将任务标为 in_progress，完成后标为 completed。"
    "对于需要跨会话持久化的任务，请使用 task_create/task_list/task_get/task_update 工具。"
    "任务支持依赖关系（blockedBy），完成前置任务后会自动解除后续任务的阻塞。"
    "【重要】当你使用 execute_code 或 write_file 工具后，必须在回复中原样包含工具返回的完整文件路径（如 D:\\xxx\\chart.png），"
    "不要用反引号包裹路径，直接写明路径以便用户可以查看和下载生成的文件或图表。"
    "【代码执行规范 - 极其重要】每次调用 execute_code 时，代码必须是完全自包含的（self-contained）："
    "1. 如果需要读取数据文件，必须在代码中立即使用 pd.read_csv() 或类似函数读取，不要假设之前的代码已经加载过数据"
    "2. 在引用任何变量前，先检查该变量是否已定义；如果不确定，重新定义或重新加载数据"
    "3. 读取文件时可以使用纯文件名（如 '贵州茅台利润表.csv'），系统会自动解析到上传目录"
    "4. 如果第一次执行因变量未定义失败，第二次必须将所有必要的数据加载和变量定义包含在同一次执行中"
    "【文件上传目录】用户上传的文件保存在 uploads/ 目录下，可以通过文件名直接引用。"
    "【工作区规则 - 重要】当前工作区根目录下的文件就是用户正在处理的文件。"
    "当用户询问工作区里有什么、某个文件的内容、项目叫什么名字、代码怎么实现等问题时，"
    "你必须先实际调用工具（run_shell 列目录 / read_file 读文件）去查看，"
    "绝不能在没有查看的情况下说「文件不存在」或「没有找到」；"
    "也不要用知识库里的无关文档来回答关于当前工作区的问题。"
)

SYSTEM_PROMPT_TOOLS = (
    "你现在拥有一个 `tavily_search` 工具，可以用于搜索最新的网络信息。"
    "【强制规则】当用户询问以下内容时，必须调用 tavily_search 工具，绝对禁止自己回答："
    "- 当前日期、时间、星期几"
    "- 天气、新闻、实时信息"
    "- 你不知道的或可能过时的内容"
    "只有当你确定用户问的是纯知识性、不需要实时数据的问题时，才可以不使用工具。"
    "你现在拥有三个知识库检索工具：search_doc（平衡模式，默认）、search_doc_fast（快速模式）、search_doc_quality（高质量模式）。"
    "【知识库检索策略】："
    "- search_doc：平衡模式（向量+BM25+RAG-Fusion+重排序），适用于大多数查询，优先使用"
    "- search_doc_fast：快速模式（仅向量），适用于简单明确的关键词查询"
    "- search_doc_quality：高质量模式（查询改写+多查询+HyDE+完整管道），适用于复杂、模糊或需要高精度的查询"
    "【知识库使用规则】："
    "- 当用户问的问题涉及已上传的文档、资料或文件时，必须调用知识库检索工具"
    "- 优先使用 search_doc；对于简短明确的查询可使用 search_doc_fast；对于复杂模糊的问题使用 search_doc_quality"
    "- 当用户的问题与任何文档无关（如闲聊、编程、数学计算等），不要调用知识库工具，直接回答"
    "- 如果检索结果不足，可以使用 read_file 工具读取知识库文件的完整路径"
)

SYSTEM_PROMPT_CONTEXT = (
    "【上下文理解】：你必须严格根据对话历史理解用户的问题。当用户使用指代（如'你'、'之前'、'刚才'、'上面'、'那个结果'、'你说的'等）时，"
    "你必须从对话历史中找到对应的信息进行回答，绝对不能要求用户澄清或重述。"
    "【停止条件 - 最高优先级】："
    "你最多只能调用 8 轮工具。在第 6 轮之后，你必须停止调用工具，直接用已有信息给出最终回答。"
    "如果连续两次调用同一个工具且参数几乎相同，说明你陷入了循环——立即停下来，直接回答。"
    "当你认为已经获得足够信息时，**立刻输出文本回答**，不要为了'确认一下'再多调一个工具。"
    "宁可在信息略少的情况下直接回答并说明假设，也不要无限探索。"
    "你总共只有 8 轮工具调用配额，用完后必须直接回答。"
)

SYSTEM_PROMPT = SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_TOOLS + SYSTEM_PROMPT_CONTEXT

# ── 路由 ───────────────────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"

# 兼容旧文件名（历史版本使用 frontend_chat2_modified.html）
if not FRONTEND_INDEX.exists():
    _legacy_index = FRONTEND_DIR / "frontend_chat2_modified.html"
    if _legacy_index.exists():
        FRONTEND_INDEX = _legacy_index


# 匹配 index.html 里对本地 js/css 的引用（相对路径或 / 开头；含 : 的外链不匹配）
_FRONTEND_REF_RE = re.compile(r'(?P<attr>src|href)=(["\'])(?P<url>[^"\':?#]+\.(?:js|css))\2')


def _versioned_index_html() -> str:
    """给 index.html 里的本地 js/css 引用加上 ?v=<文件 mtime>。

    【为什么必须这么做】index.html 与 script.js 是**配套**的。浏览器只要沿用了
    旧的 script.js、而 HTML 是新的，模板里引用的新字段在旧代码里并不存在，
    渲染阶段就抛 TypeError → Vue 卸载整棵树 → **整页白屏**（已实测复现：
    "Cannot read properties of undefined (reading 'base_url')"）。
    把文件 mtime 拼进 URL，文件一改 URL 就变，缓存再也不可能串版本。
    只有确实存在于 frontend/ 下的文件才加版本号，外链与不存在的路径原样返回。
    """
    html = FRONTEND_INDEX.read_text(encoding="utf-8")

    def _repl(m):
        rel = m.group("url").lstrip("./")
        try:
            version = int((FRONTEND_DIR / rel).stat().st_mtime)
        except OSError:
            return m.group(0)
        q = m.group(2)
        return f'{m.group("attr")}={q}{m.group("url")}?v={version}{q}'

    return _FRONTEND_REF_RE.sub(_repl, html)


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """返回前端页面。

    修复：早期版本读取项目根目录下的 frontend_chat2_modified.html，
    但该文件实际位于 frontend/ 子目录，导致 GET / 抛 FileNotFoundError（HTTP 500）。

    另外两处与"白屏"直接相关：
      1. HTML 里注入资源版本号（见 _versioned_index_html）；
      2. HTML 本身 no-store —— 入口永远是最新的，它指向的资源版本号也就是最新的。
    """
    if not FRONTEND_INDEX.exists():
        logger.error("前端页面缺失: %s", FRONTEND_INDEX)
        raise HTTPException(
            status_code=500,
            detail=f"前端页面缺失: {FRONTEND_INDEX}。请确认 frontend/index.html 存在。",
        )
    return HTMLResponse(
        content=_versioned_index_html(),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


# 需要"每次回源校验"的前端资源。StaticFiles 默认只给 ETag/Last-Modified，
# 不带 Cache-Control，浏览器会按启发式新鲜度直接用缓存 —— 那正是白屏事故的入口。
_FRONTEND_ASSET_PATHS = ("/script.js", "/styles.css", "/picker.js")


@app.middleware("http")
async def frontend_revalidate(request: Request, call_next):
    """前端资源一律要求回源校验：命中缓存时是一个 304，开销可忽略，但版本绝不会串。"""
    response = await call_next(request)
    path = request.url.path
    if path in _FRONTEND_ASSET_PATHS or path.startswith("/vendor/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def extract_and_save_files(message: str) -> tuple:
    """
    从消息中提取上传的文件/图片内容，保存到磁盘，并替换为文件路径引用。
    文本格式: [文件: filename]\\n<文件内容>
    图片格式: [图片: filename]\\n<data URL / base64>
    返回: (处理后的消息, 上传文件列表 [{filename, type, url, path}])
    """
    MAX_UPLOAD_SIZE = MAX_UPLOAD_SIZE_BYTES
    uploaded_files = []

    # 先处理文本文件
    text_pattern = r'\[文件: ([^\]]+)\]\n(.*?)(?=\n\n\[文件: |\n\n\[图片: |\Z)'
    
    def replace_text_file(match):
        filename = match.group(1).strip()
        content = match.group(2).strip()
        if len(content.encode('utf-8')) > MAX_UPLOAD_SIZE:
            return f"[文件过大，已跳过: {filename} (>10MB)]"
        file_path = UPLOADS_DIR / filename
        file_path.write_text(content, encoding='utf-8')
        logger.info("保存上传文件: %s -> %s", filename, file_path)
        uploaded_files.append({
            "filename": filename,
            "type": "file",
            "url": f"/files/{filename}",
            "path": str(file_path),
        })
        return f"[已保存文件: {filename}] 路径: {file_path}"
    
    new_message = re.sub(text_pattern, replace_text_file, message, flags=re.DOTALL)

    # 再处理图片文件
    img_pattern = r'\[图片: ([^\]]+)\]\n(.*?)(?=\n\n\[文件: |\n\n\[图片: |\Z)'

    def replace_image(match):
        filename = match.group(1).strip()
        img_data = match.group(2).strip()

        if img_data.startswith('data:'):
            _, b64_data = img_data.split(',', 1)
        else:
            b64_data = img_data

        try:
            img_bytes = base64.b64decode(b64_data)
        except Exception:
            return f"[图片解析失败: {filename}]"

        if len(img_bytes) > MAX_UPLOAD_SIZE:
            return f"[图片过大，已跳过: {filename} (>10MB)]"

        if not Path(filename).suffix:
            filename += '.png'

        file_path = UPLOADS_DIR / filename
        file_path.write_bytes(img_bytes)
        logger.info("保存上传图片: %s -> %s", filename, file_path)
        uploaded_files.append({
            "filename": filename,
            "type": "image",
            "url": f"/images/{filename}",
            "path": str(file_path),
        })
        return f"[已保存图片: {filename}] 路径: {file_path}"

    new_message = re.sub(img_pattern, replace_image, new_message, flags=re.DOTALL)
    return new_message, uploaded_files

# ── 创建 Tavily 搜索工具并绑定到 LLM ──────────────────────────────────────────
# ⚠️ 关键：Tavily 工具必须在 LCtext2 导入后显式创建并绑定，否则 LLM 无法调用它
tavily_search_tool = TavilySearch(
    max_results=5,
    include_answer=True,
    include_raw_content=True,
)

# 将 Tavily 工具加入 TOOL_MAP，使 agent_loop 能找到并执行它
TOOL_MAP[tavily_search_tool.name] = tavily_search_tool

# ── 导入 ChromaDB 向量知识库 ──────────────────────────────────────────────────
from knowledge_base import get_kb, search_doc, search_doc_fast, search_doc_quality, HAS_CHROMA
_search_doc_tool = search_doc if HAS_CHROMA else None
_search_doc_fast_tool = search_doc_fast if HAS_CHROMA else None
_search_doc_quality_tool = search_doc_quality if HAS_CHROMA else None

# 绑定所有工具（原有工具 + 页面制作 + Tavily 搜索 + 三种知识库检索）
all_tools = [
    read_file, execute_code, reset_code_environment, write_file, edit_file, run_shell,
    create_page, edit_page, list_pages,
    todo, task_create, task_list, task_get, task_update,
    tavily_search_tool,
]
# 页面制作工具同时要进 TOOL_MAP：agent_loop 与 SSE 流式循环都靠它执行工具，
# 只在 bind_tools 里注册会导致「模型能调用、后端找不到工具」。
for _page_tool in (create_page, edit_page, list_pages):
    TOOL_MAP[_page_tool.name] = _page_tool
if _search_doc_tool:
    all_tools.append(_search_doc_tool)
    TOOL_MAP[_search_doc_tool.name] = _search_doc_tool
if _search_doc_fast_tool:
    all_tools.append(_search_doc_fast_tool)
    TOOL_MAP[_search_doc_fast_tool.name] = _search_doc_fast_tool
if _search_doc_quality_tool:
    all_tools.append(_search_doc_quality_tool)
    TOOL_MAP[_search_doc_quality_tool.name] = _search_doc_quality_tool

llm_with_tools = llm.bind_tools(all_tools)


# ── 运行时切换 API（密钥 / 接口地址） ───────────────────────────────────────────
# 背景：模型客户端是**模块级常量**，进程启动时读一次 .env，所以早前换 key 必须重启。
# 这里实现「验证 → 生效 → 持久化」三步，让设置界面能直接换 key 而不必重启。
#
# 安全边界：只接受来自回环地址的请求（后端默认也只监听 127.0.0.1）。
# 这个接口能改密钥，等于一个敏感入口；若确实要开放给局域网改，
# 必须显式设置 ALLOW_REMOTE_SETTINGS=1 并自行承担风险。

import LCtext2 as _lctext2  # noqa: E402  （backend 只 from 导入过成员名，这里要模块对象本身）

ENV_FILE_PATH = Path(__file__).parent / ".env"
ALLOW_REMOTE_SETTINGS = os.getenv("ALLOW_REMOTE_SETTINGS", "").strip().lower() in ("1", "true", "yes")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

# 密钥/地址的白名单式校验：既防误填，也防把换行等字符注入 .env
_API_KEY_RE = re.compile(r"^[A-Za-z0-9\-_.~+/=:]{6,200}$")
_BASE_URL_RE = re.compile(r"^https?://[^\s'\"]{4,300}$")


def mask_api_key(key: str) -> str:
    """只回显密钥首尾，完整密钥永远不出后端。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return "****"
    return f"{key[:5]}…{key[-4:]}"


def _write_env_values(values: dict) -> None:
    """把若干键写回 .env：就地替换，保留注释、顺序与换行风格。

    【踩过的坑】必须 newline="" 打开：Python 文本模式默认会在读时把 CRLF 归一成 LF、
    在写时又把 LF 翻译成 os.linesep。Windows 上这会把整个 .env 从 LF 悄悄改成 CRLF ——
    文件内容看着没错，字节却全变了（已用字节比对验证过，改用 newline="" 后逐字节一致）。
    """
    text = ""
    if ENV_FILE_PATH.exists():
        with open(ENV_FILE_PATH, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl) if text else [""]
    for key, val in values.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for idx, line in enumerate(lines):
            if pattern.match(line):
                lines[idx] = f"{key}={val}"
                break
        else:
            lines.append(f"{key}={val}")
    with open(ENV_FILE_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines))


def rebuild_llm_clients():
    """按当前环境变量重建所有持有模型客户端的地方（主对话 + 工具绑定）。

    两处都要重建，否则会出现"模型换了但没工具"或"工具还在但模型没换"：
      · LCtext2.llm / LCtext2.llm_with_tools —— LCtext2 内部兜底链用的是这两个模块级名字
      · backend.llm / backend.llm_with_tools —— 所有 /chat 链路走的是 backend 的这份绑定
    knowledge_base 里是 `from LCtext2 import llm`（每次调用时取），替换模块属性后自动跟上。
    Planner / Judge 每次请求都读 os.environ，所以更新环境变量后立即生效。
    """
    new_llm = _lctext2.build_llm()
    _lctext2.llm = new_llm
    _lctext2.llm_with_tools = new_llm.bind_tools(_lctext2.LC_TOOLS)
    global llm, llm_with_tools
    llm = new_llm
    llm_with_tools = new_llm.bind_tools(all_tools)
    return new_llm


def probe_credentials(api_key: str, base_url: str, model: str) -> dict:
    """用给定密钥做一次极小的真实请求，判断配置是否可用（阻塞，调用方放线程里跑）。

    刻意设 max_retries=0：配置写错时要立刻失败，而不是让用户等三次重试。
    """
    started = time.perf_counter()
    try:
        probe = _lctext2.build_llm(api_key=api_key, base_url=base_url, model=model,
                                   temperature=0, timeout=20, max_retries=0)
        resp = probe.invoke("ping")
        return {"ok": True, "ms": int((time.perf_counter() - started) * 1000),
                "reply": str(getattr(resp, "content", ""))[:60]}
    except Exception as e:
        return {"ok": False, "ms": int((time.perf_counter() - started) * 1000),
                "error": f"{type(e).__name__}: {str(e)[:300]}"}


def _settings_client_allowed(request: Request) -> bool:
    if ALLOW_REMOTE_SETTINGS:
        return True
    host = request.client.host if request.client else ""
    return host in _LOOPBACK_HOSTS


def _api_settings_view(request: Request) -> dict:
    key = os.getenv("OPENAI_API_KEY") or ""
    return {
        "base_url": os.getenv("LLM_BASE_URL") or "",
        "fallback_base_url": os.getenv("DASHSCOPE_BASE_URL") or "",
        "model": os.getenv("LLM_MODEL") or "",
        "temperature": os.getenv("LLM_TEMPERATURE", ""),
        "api_key_masked": mask_api_key(key),
        "api_key_set": bool(key.strip()),
        "env_file": str(ENV_FILE_PATH),
        "writable": _settings_client_allowed(request),
        "allow_remote": ALLOW_REMOTE_SETTINGS,
    }


class ApiSettingsRequest(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    verify: bool = True


@app.get("/settings/api")
async def get_api_settings(request: Request):
    """当前生效的接口配置（密钥只回显首尾，供界面显示"当前是哪一个"）。"""
    return _api_settings_view(request)


@app.post("/settings/api")
async def update_api_settings(payload: ApiSettingsRequest, request: Request):
    """换 API：验证 → 立即生效（免重启）→ 写入 .env 持久化。

    顺序刻意是"先验证后落盘"：密钥或地址不通就整个拒绝，原配置继续可用，
    不会留下"存进去了但用不了"的中间状态。留空表示不修改该项。
    """
    if not _settings_client_allowed(request):
        raise HTTPException(403, "只允许从本机修改接口配置。确需远程修改，请在后端设置 ALLOW_REMOTE_SETTINGS=1")

    current_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    current_base = (os.getenv("LLM_BASE_URL") or "").strip()
    current_model = (os.getenv("LLM_MODEL") or "").strip()

    new_key = (payload.api_key or "").strip() or current_key
    new_base = ((payload.base_url or "").strip().rstrip("/")) or current_base

    if not new_key:
        raise HTTPException(400, "API 密钥为空：请填写密钥，或先在后端 .env 中配置 OPENAI_API_KEY")
    if not _API_KEY_RE.match(new_key):
        raise HTTPException(400, "API 密钥格式不合法（只允许字母、数字与 - _ . ~ + / = :，不能含空格或换行）")
    if new_base and not _BASE_URL_RE.match(new_base):
        raise HTTPException(400, "接口地址必须以 http:// 或 https:// 开头，且不能含空格")

    changed = {}
    if new_key != current_key:
        changed["OPENAI_API_KEY"] = new_key
    if new_base != current_base:
        changed["LLM_BASE_URL"] = new_base

    verify_result = None
    if payload.verify:
        verify_result = await asyncio.to_thread(probe_credentials, new_key, new_base, current_model)
        if not verify_result.get("ok"):
            raise HTTPException(400, f"新配置连通性测试失败，已保持原配置不变 —— {verify_result.get('error')}")

    if not changed:
        return {"ok": True, "changed": [], "message": "配置没有变化", "verify": verify_result,
                **_api_settings_view(request)}

    # 生效：先写进程环境（Planner/Judge 每请求读 env，立即跟上），再重建主客户端
    os.environ["OPENAI_API_KEY"] = new_key
    if new_base:
        os.environ["LLM_BASE_URL"] = new_base
    rebuild_llm_clients()

    # 持久化：失败不影响本次生效，但要如实告知（否则重启后"莫名其妙变回去了"）
    persist_error = None
    try:
        _write_env_values(changed)
    except Exception as e:
        persist_error = f"{type(e).__name__}: {e}"
        logger.error("写入 .env 失败: %s", e)

    logger.info("接口配置已切换: base_url=%s key=%s 变更=%s", new_base, mask_api_key(new_key), sorted(changed))
    return {"ok": True, "changed": sorted(changed), "persist_error": persist_error,
            "verify": verify_result, **_api_settings_view(request)}


# ── 公共辅助函数（消除 /chat 和 /chat/stream 的重复代码） ────────────────────────

def _format_dialogue_history(messages: list) -> str:
    """将消息历史格式化为自然语言对话转录，注入系统提示词提供上下文。"""
    if len(messages) < 2:
        return ""
    transcript_lines = ["\n\n【完整对话历史（请以此为上下文依据）】"]
    round_idx = 1
    prev_msg_type = None
    for m in messages:
        cls_name = m.__class__.__name__
        if cls_name == 'HumanMessage':
            if prev_msg_type is not None:
                round_idx += 1
            content = str(m.content)[:500]
            transcript_lines.append(f"第{round_idx}轮 - 用户: {content}")
            prev_msg_type = 'human'
        elif cls_name == 'AIMessage':
            content = str(m.content)[:500]
            transcript_lines.append(f"第{round_idx}轮 - 助手: {content}")
            prev_msg_type = 'ai'
        elif cls_name == 'ToolMessage':
            content = str(m.content)[:200]
            transcript_lines.append(f"第{round_idx}轮 - [工具返回]: {content}")
    transcript_lines.append("（请基于以上完整对话历史来回答用户的问题，用户的追问通常与之前的对话直接相关）")
    return "\n".join(transcript_lines)


# 不展示 / 不计入提示词的目录名（缓存、虚拟环境、版本库内部）
# 定义在 _workspace_snapshot 之前，避免"先用后定义"的隐患。
WORKSPACE_SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
}


def _workspace_snapshot(root: Path, limit: int = 40) -> str:
    """
    生成工作区根目录的条目清单，直接写进提示词。

    为什么要把清单放进提示词：模型看不到文件系统，光告诉它"根目录是 X"
    它仍会凭知识库或猜测作答（实测会答出别的项目的内容）。
    给它一份真实清单，它才知道该去读哪个文件。
    """
    try:
        dirs, files = [], []
        with os.scandir(root) as it:
            for e in it:
                if e.name.startswith(".") or e.name in WORKSPACE_SKIP_DIRS:
                    continue
                if e.is_dir(follow_symlinks=False):
                    dirs.append(e.name + "/")
                elif e.is_file(follow_symlinks=False):
                    files.append(e.name)
        dirs.sort()
        files.sort()
        entries = dirs + files
        if not entries:
            return "（该工作区目前是空的）"
        shown = entries[:limit]
        text = "、".join(shown)
        if len(entries) > limit:
            text += f" …（共 {len(entries)} 项）"
        return text
    except Exception as e:
        logger.debug("生成工作区清单失败: %s", e)
        return "（无法读取目录内容）"


def _format_scope(scope: str) -> str:
    """
    把前端选中的工作区作用域转成一段提示词。

    scope 是**相对当前工作区根**的路径（'' 表示工作区根自身）。
    安全：越界路径一律忽略（作用域只是提示信息，不该让整个请求失败）。
    """
    rel = (scope or "").strip().replace("\\", "/").strip("/")
    root = utils_get_base_dir()
    snapshot = _workspace_snapshot(root)

    if not rel or rel == ".":
        return (
            f"\n\n【当前工作区】根目录：`{root}`\n"
            f"根目录实际内容：{snapshot}\n"
            "【强制规则】\n"
            "1. 当用户询问这个工作区里有什么、某个文件的内容、项目叫什么、代码如何实现时，"
            "**必须先用工具实际查看**：列目录用 run_shell（`ls -la` 或 `Get-ChildItem`），"
            "读文件用 read_file。\n"
            "2. 相对路径一律相对上面这个根目录解析（read_file('README.md') 读的是工作区里的那个）。\n"
            "3. 禁止在没有调用工具的情况下断言「文件不存在」或「没有找到」。\n"
            "4. **不要**用知识库里的无关文档内容来回答关于当前工作区的问题；"
            "如果上面列出的工作区内容与问题无关，就明确说明工作区里没有相关内容。"
        )

    try:
        target = (root / rel).resolve()
    except OSError:
        return ""
    if target != root and not target.is_relative_to(root):
        logger.warning("忽略越界的工作区作用域: %r", scope)
        return ""

    kind = "文件" if target.is_file() else ("目录" if target.is_dir() else None)
    if kind is None:
        return ""

    note = (
        f"\n\n【当前工作区】根目录：`{root}`\n"
        f"根目录实际内容：{snapshot}\n"
        f"用户当前聚焦的{kind}：`{rel}`（完整路径 {target}）\n"
    )
    if kind == "文件":
        note += (
            "【强制规则】当用户说「这个文件」「它」「该文件」时，指的就是上面这个文件。"
            f"回答任何关于它内容的问题前，**必须先用 read_file 读取 `{target}`**，"
            "不要凭空推测，也不要因为知识库里没有它就回答「找不到」。"
        )
    else:
        note += (
            "【强制规则】当用户说「这个目录」「这里」时，指的就是上面这个目录。"
            f"需要了解其内容时**必须先用 run_shell 或 read_file 实际查看**"
            f"（例如列出 `{target}` 下的文件），不要凭猜测或知识库内容作答。"
        )
    return note


def _build_dynamic_system(messages: list, processed_message: str, scope: str = "",
                          workspace_id: Optional[str] = None) -> str:
    """构建动态系统提示词：注入对话历史、对话摘要、工具上下文、知识库内容、工作区作用域、文件工作区内容。"""
    dynamic_system = SYSTEM_PROMPT

    scope_note = _format_scope(scope)
    if scope_note:
        dynamic_system += scope_note

    dynamic_system += _format_dialogue_history(messages)

    if len(messages) > 3:
        memory_summary, _ = build_conversation_memory(messages, keep_last_n=2)
        if memory_summary:
            dynamic_system += f"\n\n【对话历史摘要（辅助参考）】\n{memory_summary}"

    tool_context = extract_tool_context(messages)
    if tool_context:
        dynamic_system += tool_context

    kb = _load_kb()
    kb_file_list = _get_kb_file_list_from(kb)
    if kb_file_list:
        dynamic_system += f"\n\n{kb_file_list}"
    kb_context = search_knowledge_base_from(kb, processed_message)
    if kb_context:
        dynamic_system += (
            f"\n\n【知识库上下文】以下是知识库中与用户问题**可能**相关的内容，仅供参考：\n\n"
            f"{kb_context}\n\n"
            f"使用原则（按优先级）：\n"
            f"1. **先判断相关性**：只有当这些内容确实切题时才引用；不相关就忽略，不要生搬硬套。\n"
            f"2. **工作区问题优先用工具**：如果用户问的是当前工作区里的文件、目录或代码，"
            f"必须用 run_shell / read_file 实际查看，**不要用知识库文档来替代**。\n"
            f"3. 知识库内容不足以回答时，用 read_file 读取对应文件获取完整内容。\n"
            f"4. 不要编造知识库中没有的信息；也不要把某个文档的内容说成是用户当前工作区的文件。"
        )
    elif len(messages) > 1:
        last_ai_msg = ""
        for msg in reversed(messages):
            if hasattr(msg, 'content') and msg.__class__.__name__ == 'AIMessage':
                last_ai_msg = str(msg.content)[:500]
                break
        if last_ai_msg and any(kw in last_ai_msg for kw in ['文件', '文档', '读取', '详细', '完整', '内容', '章节', 'PDF']):
            dynamic_system += "\n\n【上下文提醒】用户的消息可能是对之前对话的追问。请回顾之前的对话内容，理解用户的真实意图。如果之前提到了某个知识库文件，请使用 read_file 工具读取该文件的完整内容来回答用户。"

    if workspace_id:
        try:
            files_ctx = _ws.format_workspace_context(workspace_id)
            if files_ctx:
                dynamic_system += files_ctx
        except Exception as e:
            logger.warning("注入文件工作区上下文失败: %s", e)

    return dynamic_system


def _extract_attachments(messages: list, msg_count_before: int, uploaded_files: list) -> dict:
    """
    提取【用户上传】的附件。

    注意：不要再用它对工具生成的产物做「按位置切片」提取。
    原因：maybe_compress_agent_context 会在流式过程中把 messages 替换成一个
    更短的列表，而 msg_count_before 是针对旧列表记录的，切片会变成空，
    导致工具生成的图表被静默丢弃（表现为"有回复但没图表"）。
    工具产物改由 scan_tool_result_paths() 在产出当下收集。
    """
    attachments = {"images": [], "files": []}

    for uf in uploaded_files:
        if uf["type"] == "image":
            attachments["images"].append({"filename": uf["filename"], "url": uf["url"]})
        else:
            file_content = extract_file_content(Path(uf["path"]))
            attachments["files"].append({
                "filename": uf["filename"],
                "url": uf["url"],
                "content": file_content,
            })

    return attachments


def _artifact_url(p: Path) -> str:
    """
    产物文件的对外访问地址 —— 按它**实际所在的位置**给出，给不出可用地址就返回空串。

    【踩过的坑】早前一律写成 /files/{文件名}，但 /files、/images 只服务
    uploads/ 与 pang/（安全边界，刻意不含工作区）。而 Agent 现在最常产出的东西是
    工作区 pages/ 里的页面 —— 于是附件链接指向 /files/blog.html 直接 404
    （实测：/files/blog.html=404，/pages/file/blog.html=200）。
    宁可不给链接，也不给一个点开就报错的链接。
    """
    try:
        rp = p.resolve()
    except OSError:
        return ""

    pages_root = (utils_get_base_dir() / PAGES_DIR_NAME).resolve()
    if rp.is_relative_to(pages_root):
        rel = rp.relative_to(pages_root).as_posix()
        return f"/pages/file/{rel}"

    is_image = rp.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
    try:
        if rp.is_relative_to(UPLOADS_DIR.resolve()) or rp.is_relative_to(CHART_OUTPUT_DIR.resolve()):
            return f"/images/{p.name}" if is_image else f"/files/{p.name}"
    except OSError:
        pass

    # 工作区里其它位置：现有静态接口覆盖不到，交由界面按「不可打开」渲染
    return ""


def scan_tool_result_paths(raw_result: str) -> list:
    """
    从工具返回值中扫描出真实存在的产物文件，转换为附件条目。

    在工具执行完的那一刻调用（而不是事后按消息下标切片），
    因此不受消息列表被压缩/裁剪的影响。
    """
    found = []
    if not raw_result:
        return found
    for path_match in re.finditer(FILE_PATH_PATTERN, str(raw_result)):
        full_path = path_match.group(0)
        p = Path(full_path.replace("\\", "/"))
        if not p.is_file():
            # 正则可能命中 URL 片段（如 https://host/a.html 里的 "s://host/a.html"）
            continue
        url = _artifact_url(p)
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
            found.append({"filename": p.name, "url": url})
        else:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            found.append({
                "filename": p.name,
                "url": url,
                "size": size,
                "content": extract_file_content(p),
            })
    return found


def _replace_local_paths(text: str) -> str:
    """将回复中的本地文件/图片路径替换为可访问的 HTTP URL（给不出地址就保留原路径）。"""
    def _replacer(match):
        full_path = match.group(0)
        p = Path(full_path.replace("\\", "/"))
        if not p.exists():
            return full_path
        url = _artifact_url(p)
        if not url:
            # 工作区里不在 pages/ 的文件没有静态出口 —— 保留原路径，别给死链
            return full_path
        filename = p.name
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg')):
            return f"![图表]({url})"
        if url.startswith("/pages/file/"):
            return f"[🖥️ 预览页面: {filename}]({url})"
        return f"[📎点击下载: {filename}]({url})"

    result = re.sub(FILE_PATH_PATTERN, _replacer, text)
    # 剥掉包在链接/图片外的反引号（模型常写成 `[..](..)`）
    result = re.sub(r'`(!?\[[^\]]*\]\([^)]+\))`', r'\1', result)
    return result


# ── 会话与工作区绑定 ────────────────────────────────────────────────────────────

def _bind_session(session_id: str, workspace_id: Optional[str] = None) -> dict:
    """
    获取或创建会话，并绑定它所属的工作区。

    为什么每个会话要记工作区：工作区根是进程级共享状态，
    并发请求各自切换会互相踩（实测：A 会话的文件操作落到 B 的工作区）。
    把工作区记在会话上，处理请求时再用 utils.workspace_scope 绑定，
    就能保证一个请求内始终看同一个根。
    """
    with _sessions_lock:
        sess = sessions.get(session_id)
        if sess is None:
            sess = sessions[session_id] = {
                "messages": [],
                "last_access": time.time(),
                "workspace_id": workspace_id or None,
            }
        else:
            sess["last_access"] = time.time()
            # 会话已存在时以显式传入的为准；否则保留原有绑定
            if workspace_id:
                sess["workspace_id"] = workspace_id
        return sess


def _resolve_request_workspace(sess: dict, workspace_id: Optional[str]) -> Optional[str]:
    """
    决定本次请求应使用哪个工作区目录。

    优先级：请求显式指定 → 会话已绑定。
    返回 None 表示"不切换"，由 workspace_scope 从 registry 读当前激活工作区。
    folder 类型返回其 path；files 类型返回第一个文件的父目录。
    """
    for wid in (workspace_id, (sess or {}).get("workspace_id")):
        if not wid:
            continue
        try:
            w = _ws.get_workspace(wid)
        except Exception:
            w = None
        if w is None:
            continue
        p = Path(w.get("path", ""))
        if not p.is_dir():
            continue
        return str(p)
    return None


# ── 路由 ───────────────────────────────────────────────────────────────────────

@app.post("/chat", dependencies=[Depends(rate_limit_dependency)])
async def chat(req: ChatRequest):
    """
    接收用户消息，调用 Agent 处理后返回 AI 回复。
    - 若 session_id 为空，自动创建新会话
    - 消息历史保存在内存中，同一会话共享上下文
    - 上传的文件会保存到 uploads/ 目录，供工具读取
    """
    try:
        req.validate_message()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 1. 获取或创建会话（修复：保留前端传来的 session_id，不覆盖）
    session_id = req.session_id
    if not session_id:
        session_id = str(uuid.uuid4())
    sess = _bind_session(session_id, req.workspace_id)
    ws_root = _resolve_request_workspace(sess, req.workspace_id)

    messages = sess["messages"]
    msg_count_before = len(messages)  # 记录本次请求前的消息数量

    # 2. 处理消息：提取并保存上传文件，替换为路径引用
    processed_message, uploaded_files = extract_and_save_files(req.message)

    # 3. 追加用户消息
    messages.append(HumanMessage(content=processed_message))

    logger.info("chat session=%s, messages count=%d", session_id, len(messages))
    for idx, m in enumerate(messages):
        cls = m.__class__.__name__
        content = str(m.content)[:200] if hasattr(m, 'content') else str(m)[:200]
        logger.debug("  [%d] %s: %s", idx, cls, content)

    dynamic_system = _build_dynamic_system(messages, processed_message, req.scope or "",
                                            workspace_id=req.workspace_id or sess.get("workspace_id"))

    # 4. 构建处理链（每次请求重建，确保 prompt 包含最新消息列表）
    # 显式传入 backend 绑定过全部工具的 llm_with_tools，否则 agent_loop
    # 重建 chain 时会退回 LCtext2 模块级只绑 10 个本地工具的版本。
    # 注意：系统提示词以字面量 SystemMessage 注入，不参与模板渲染 ——
    # 工具返回值里的花括号（JSON/dict/代码）会破坏 ChatPromptTemplate 解析。
    chain = _rebuild_chain(dynamic_system, llm_with_tools)

    # 5/6. 在**本会话的工作区**内执行 LLM + 工具循环。
    # agent_loop 内部会调用 read_file / run_shell / execute_code 等工具，
    # 它们都读进程级的工作区根；不绑定的话并发请求会互相踩。
    with workspace_scope(ws_root):
        response = chain.invoke({"messages": messages})
        response = agent_loop(
            chain, messages, response,
            saved_system=dynamic_system,
            llm_override=llm_with_tools,
        )

    # 7. 保存 AI 回复到会话历史
    messages.append(AIMessage(content=response.content))

    # 8. 滑动窗口：裁剪过长的会话历史（加锁，会话可能已被清理）
    with _sessions_lock:
        if session_id in sessions:
            sessions[session_id]["messages"] = trim_session_history(messages)
            sessions[session_id]["last_access"] = time.time()

    # 8. 提取附件（用户上传 + 工具生成）
    attachments = _extract_attachments(messages, msg_count_before, uploaded_files)

    # 9. 将回复中的本地文件/图片路径转换为可访问的 HTTP URL
    reply_text = _replace_local_paths(response.content)

    return {"reply": reply_text, "session_id": session_id, "attachments": attachments}


# ── SSE 流式聊天端点 ────────────────────────────────────────────────────────────

# 工具名称 → 中文描述映射（用于前端进度显示）
MAX_TOOL_ROUNDS = 8
MAX_TOTAL_TOOL_CALLS = 24

TOOL_LABELS = {
    "read_file": "读取文件", "execute_code": "执行代码", "write_file": "写入文件",
    "edit_file": "编辑文件", "tavily_search": "搜索网络", "todo": "更新任务",
    "task_create": "创建任务", "task_list": "查询任务", "task_get": "获取任务",
    "task_update": "更新任务", "reset_code_environment": "重置环境",
    "run_shell": "执行命令",
    "create_page": "制作页面", "edit_page": "修改页面", "list_pages": "查看页面",
    "search_doc": "检索知识库", "search_doc_fast": "快速检索", "search_doc_quality": "深度检索",
}

# 工具名称 → 详细操作描述（带上下文说明，增强透明度）
TOOL_DESCRIPTIONS = {
    "read_file": "正在读取文件内容",
    "execute_code": "正在运行代码进行计算分析",
    "write_file": "正在创建新文件",
    "edit_file": "正在修改文件内容",
    "tavily_search": "正在搜索网络获取最新信息",
    "todo": "正在规划任务步骤",
    "task_create": "正在创建任务",
    "task_list": "正在查询任务列表",
    "task_get": "正在获取任务详情",
    "task_update": "正在更新任务状态",
    "reset_code_environment": "正在重置代码执行环境",
    "run_shell": "正在执行命令行指令",
    "create_page": "正在制作前端页面并写入工作区 pages/ 目录",
    "edit_page": "正在修改页面内容",
    "list_pages": "正在查看当前工作区的页面",
    "search_doc": "正在检索知识库文档",
    "search_doc_fast": "正在快速检索知识库",
    "search_doc_quality": "正在深度检索知识库（改写+多查询+HyDE）",
}

# 工具名称 → 进度提示表情符号/图标
TOOL_ICONS = {
    "read_file": "📄", "execute_code": "⚡", "write_file": "✏️",
    "edit_file": "🔧", "tavily_search": "🌐", "todo": "📋",
    "task_create": "📝", "task_list": "📋", "task_get": "🔍",
    "task_update": "✅", "reset_code_environment": "🔄",
    "run_shell": "⌨️",
    "create_page": "🎨", "edit_page": "🖌️", "list_pages": "🗂️",
    "search_doc": "📚", "search_doc_fast": "🔎", "search_doc_quality": "🧠",
}

def _format_sse(event: str, data: dict) -> str:
    """格式化一条 SSE 消息，格式: data: {"type": "...", "data": {...}}"""
    payload = {"type": event, "data": data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


_PYTHON_CODE_BLOCK_RE = re.compile(r'```(?:python|py)?\s*\n(.*?)```', re.DOTALL | re.IGNORECASE)

def _detect_python_blocks(text: str) -> list:
    """检测文本中的 Python 代码块，返回代码字符串列表。"""
    if not text:
        return []
    blocks = []
    for m in _PYTHON_CODE_BLOCK_RE.finditer(text):
        code = m.group(1).strip()
        if code and len(code) > 20:
            blocks.append(code)
    return blocks


def _auto_execute_detected_code(text: str) -> tuple:
    """
    自动检测文本中的 Python 代码块并执行。
    返回 (modified_text, chart_paths, execution_notes)。
    """
    blocks = _detect_python_blocks(text)
    if not blocks:
        return text, [], ""

    chart_paths = []
    notes = []

    for i, code in enumerate(blocks):
        try:
            executor = get_executor()
            result = executor.execute(code)
            if result.get('figures'):
                for fp in result['figures']:
                    chart_paths.append(fp)
                safe_paths = [p.replace('\\', '/') for p in result['figures']]
                notes.append(f"\n\n> 📊 已自动执行代码并生成图表: {', '.join(safe_paths)}")
            elif result.get('success') and result.get('output'):
                preview = result['output'][:200]
                notes.append(f"\n\n> ▶ 代码自动执行完成:\n> {preview}")
            elif result.get('error'):
                notes.append(f"\n\n> ⚠ 代码自动执行失败: {result['error'][:200]}")
        except Exception as e:
            notes.append(f"\n\n> ⚠ 代码自动执行异常: {str(e)[:200]}")

    combined_notes = "\n".join(notes)
    return text, chart_paths, combined_notes


def _summarize_tool_args(tool_name: str, args: dict) -> str:
    """生成工具参数的安全摘要，过滤敏感信息，仅展示关键参数。"""
    if not args:
        return ""
    try:
        if tool_name == "read_file":
            path = args.get("file_path", "")
            filename = Path(str(path)).name if path else ""
            return f"读取文件: {filename}" if filename else "读取文件"
        elif tool_name == "execute_code":
            code = args.get("code", "")
            lines = str(code).strip().split('\n')
            preview = lines[0][:80] if lines else ""
            if len(lines) > 1:
                preview += f" ... (共{len(lines)}行)"
            return f"执行代码: {preview}"
        elif tool_name == "write_file":
            path = args.get("file_path", "")
            filename = Path(str(path)).name if path else ""
            return f"写入文件: {filename}" if filename else "写入文件"
        elif tool_name == "edit_file":
            path = args.get("file_path", "")
            filename = Path(str(path)).name if path else ""
            return f"编辑文件: {filename}" if filename else "编辑文件"
        elif tool_name == "run_shell":
            cmd = str(args.get("command", "")).strip().replace("\n", " ")
            preview = cmd[:80]
            if len(cmd) > 80:
                preview += " ..."
            cwd = args.get("cwd", "")
            return f"执行命令: {preview}" + (f" (工作目录: {cwd})" if cwd else "")
        elif tool_name == "tavily_search":
            query = args.get("query", "")
            return f"搜索: {str(query)[:60]}" if query else "搜索网络"
        elif tool_name in ("search_doc", "search_doc_fast", "search_doc_quality"):
            query = args.get("query", "")
            mode_map = {"search_doc": "平衡", "search_doc_fast": "快速", "search_doc_quality": "深度"}
            mode_label = mode_map.get(tool_name, "")
            prefix = f"[{mode_label}] " if mode_label else ""
            return f"{prefix}检索知识库: {str(query)[:60]}" if query else f"{prefix}检索知识库"
        elif tool_name == "todo":
            items = args.get("items", [])
            count = len(items) if isinstance(items, list) else 0
            return f"更新{count}个任务项" if count else "更新任务列表"
        elif tool_name in ("create_page", "edit_page"):
            name = args.get("name", "")
            content = args.get("content", "")
            if tool_name == "edit_page":
                return f"修改页面: {name}" if name else "修改页面"
            size = len(str(content)) if content else 0
            return f"制作页面: {name}（{size} 字符）" if name else "制作页面"
        elif tool_name == "list_pages":
            return "查看页面列表"
        elif tool_name.startswith("task_"):
            return f"任务操作: {tool_name}"
        else:
            arg_str = str(args)[:60]
            return f"参数: {arg_str}"
    except Exception:
        return ""


class ThinkingStreamSplitter:
    """将 LLM 流式输出中的 <thinking>...</thinking> 内容分离为独立事件。
    支持 <thinking> 和 <think> 两种标签。
    用法:
        splitter = ThinkingStreamSplitter()
        async for chunk in chain.astream(...):
            async for evt in splitter.feed(chunk):
                yield evt
        async for evt in splitter.flush():
            yield evt
    """

    def __init__(self):
        self._buffer = ""
        self._in_thinking = False
        self._tag = None

    async def feed(self, chunk) -> list:
        """处理一个 chunk，返回产生的 SSE 事件列表"""
        events = []
        if not (chunk.content and isinstance(chunk.content, str)):
            return events

        text = chunk.content
        self._buffer += text

        # 循环检测标签切换
        while self._buffer:
            if not self._in_thinking:
                # 检查是否有 <thinking> 或 <think> 开始标签
                lowered = self._buffer.lower()
                start_pos = None
                tag_name = None
                for tag in ["<thinking>", "<think>"]:
                    idx = lowered.find(tag)
                    if idx != -1:
                        if start_pos is None or idx < start_pos:
                            start_pos = idx
                            tag_name = tag
                            break

                if start_pos is not None:
                    # 标签之前的内容作为普通 token 发送
                    if start_pos > 0:
                        before = self._buffer[:start_pos]
                        events.append(_format_sse("token", {"text": before}))
                    self._buffer = self._buffer[start_pos + len(tag_name):]
                    self._in_thinking = True
                    self._tag = tag_name.replace("<", "").replace(">", "")
                    # 通知前端思考开始
                    events.append(_format_sse("thinking_start", {"tag": self._tag}))
                else:
                    # 没有检测到开始标签，安全地发送前面的普通内容
                    # 保留最后几个字符以防标签被拆分
                    safe_len = len(self._buffer)
                    if safe_len > 10:
                        safe_part = self._buffer[:safe_len - 10]
                        self._buffer = self._buffer[safe_len - 10:]
                        events.append(_format_sse("token", {"text": safe_part}))
                    else:
                        break
            else:
                # 在思考模式中，检查是否有 </thinking> 或 </think> 结束标签
                end_tag = f"</{self._tag}>"
                lowered = self._buffer.lower()
                end_pos = lowered.find(end_tag)

                if end_pos != -1:
                    # 发送思考内容
                    thinking_content = self._buffer[:end_pos]
                    if thinking_content:
                        events.append(_format_sse("thinking", {"text": thinking_content}))
                    self._buffer = self._buffer[end_pos + len(end_tag):]
                    self._in_thinking = False
                    self._tag = None
                    events.append(_format_sse("thinking_end", {}))
                else:
                    # 思考中，安全发送前面的内容
                    safe_len = len(self._buffer)
                    if safe_len > 10:
                        safe_part = self._buffer[:safe_len - 10]
                        self._buffer = self._buffer[safe_len - 10:]
                        events.append(_format_sse("thinking", {"text": safe_part}))
                    else:
                        break

        return events

    async def flush(self) -> list:
        """流结束时刷新缓冲区"""
        events = []
        if self._in_thinking and self._buffer:
            events.append(_format_sse("thinking", {"text": self._buffer}))
            events.append(_format_sse("thinking_end", {}))
        elif self._buffer:
            events.append(_format_sse("token", {"text": self._buffer}))
        self._buffer = ""
        return events


def _build_executor_extra_system(
    plan: dict,
    judge_feedback: str = None,
    attempt: int = 1,
) -> str:
    """把 Planner 的执行计划拼进 Executor 的系统提示词，每轮闭环都注入。"""
    parts = [f"\n{'='*60}\n"]
    parts.append(f"【Multi-Agent 闭环执行 - Planner 计划（第 {attempt} 轮）】")
    parts.append(f"🎯 目标: {plan.get('goal', '(未明确)')}")
    parts.append("📋 执行步骤:")
    for i, step in enumerate(plan.get("plan", []), 1):
        parts.append(f"  {i}. {step}")
    criteria = plan.get("success_criteria", [])
    if criteria:
        parts.append("✅ 成功标准:")
        for sc in criteria:
            parts.append(f"  - {sc}")
    if judge_feedback and attempt > 1:
        parts.append(f"\n⚠️ 上一轮 Judge 反馈（必须解决）:\n{judge_feedback}")
        parts.append("请针对以上反馈调整执行策略，不要重复上一轮的错误。")
    parts.append(f"{'='*60}\n")
    return "\n".join(parts)


def _extract_tool_trace_summary(messages: list, max_items: int = 15) -> str:
    """从 session messages 里提取最近的工具执行摘要，供 Judge 评估。"""
    from langchain_core.messages import AIMessage, ToolMessage
    lines = ["### 最近工具执行轨迹"]
    count = 0
    for m in reversed(messages):
        if count >= max_items:
            break
        if isinstance(m, ToolMessage):
            content = str(m.content)[:150]
            lines.append(f"  ToolResult({m.tool_call_id}): {content}")
            count += 1
        elif isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                args_summary = str(tc.get("args", {}))[:100]
                lines.append(f"  ToolCall: {tc['name']}({args_summary})")
                count += 1
    if count == 0:
        lines.append("  (本轮未调用工具)")
    return "\n".join(reversed(lines))


@app.post("/chat/stream", dependencies=[Depends(rate_limit_dependency)])
async def chat_stream(req: ChatRequest):
    """
    SSE 流式聊天端点。
    事件类型：
      - token:        LLM 逐字输出 {"text": "..."}
      - tool_start:   开始执行工具 {"tool": "read_file", "label": "读取文件"}
      - tool_end:     工具执行完毕 {"tool": "read_file"}
      - attachments:  生成的附件 {"images": [...], "files": [...]}
      - done:         流式结束 {"session_id": "..."}
      - error:        发生错误 {"message": "..."}
    """
    try:
        req.validate_message()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    session_id = req.session_id
    if not session_id:
        session_id = str(uuid.uuid4())
    sess = _bind_session(session_id, req.workspace_id)
    ws_root = _resolve_request_workspace(sess, req.workspace_id)

    messages = sess["messages"]
    msg_count_before = len(messages)

    processed_message, uploaded_files = extract_and_save_files(req.message)
    messages.append(HumanMessage(content=processed_message))

    logger.info("chat_stream session=%s, messages count=%d", session_id, len(messages))
    for idx, m in enumerate(messages):
        cls = m.__class__.__name__
        content = str(m.content)[:200] if hasattr(m, 'content') else str(m)[:200]
        logger.debug("  [%d] %s: %s", idx, cls, content)

    dynamic_system = _build_dynamic_system(messages, processed_message, req.scope or "",
                                            workspace_id=req.workspace_id or sess.get("workspace_id"))

    # 统一走 _rebuild_chain，并显式传入 backend 绑定过全部工具的 llm_with_tools。
    # （早期版本这里直接用模块级 llm_with_tools，首轮只有 10 个本地工具，
    #   tavily_search / search_doc* 实际从未提供给模型。）
    chain = _rebuild_chain(dynamic_system, llm_with_tools)
    saved_system = dynamic_system

    # 用户上传的附件：只取一次
    attachments = _extract_attachments(messages, msg_count_before, uploaded_files)
    # 工具产物在产出当下收集（见 scan_tool_result_paths 的说明）
    _seen_attach = {a["filename"] for a in attachments["images"]} | \
                   {a["filename"] for a in attachments["files"]}

    def _record_tool_attachments(raw_result):
        for item in scan_tool_result_paths(str(raw_result)):
            if item["filename"] in _seen_attach:
                continue
            _seen_attach.add(item["filename"])
            if item["url"].startswith("/images/"):
                attachments["images"].append({"filename": item["filename"], "url": item["url"]})
            else:
                attachments["files"].append(item)

    async def event_generator():
        nonlocal session_id
        nonlocal chain
        nonlocal saved_system
        nonlocal messages
        scope_cm = workspace_scope(ws_root)
        scope_cm.__enter__()
        try:
            t_total_start = time.perf_counter()

            llm_cfg = build_llm_config_from_env()
            use_multi = ENABLE_MULTI_AGENT and worth_multi_agent(processed_message)

            planner = PlannerAgent(llm_cfg) if use_multi else None
            judge = JudgeAgent(llm_cfg) if use_multi else None
            plan = None
            judge_feedback = None
            executor_start_idx = len(messages)

            yield _format_sse("phase", {
                "phase": "multi_agent",
                "sub": "init",
                "enabled": use_multi,
                "max_retries": MAX_JUDGE_RETRIES,
                "label": ("🧠 启用 Planner→Executor→Judge 闭环" if use_multi
                          else "单 Agent 模式（问题复杂度不足，跳过闭环）"),
                "ts": round(time.perf_counter() - t_total_start, 3),
            })

            max_attempts = MAX_JUDGE_RETRIES if use_multi else 1
            executor_original_attachments = list(attachments.get("images", [])) + list(attachments.get("files", []))

            for attempt in range(1, max_attempts + 1):

                if use_multi or True:
                    executor_start_idx = len(messages)

                # ── Planner 阶段（多 Agent 模式）──
                if use_multi:
                    yield _format_sse("phase", {
                        "phase": "multi_agent",
                        "sub": "planner_start",
                        "attempt": attempt,
                        "label": f"📋 Planner 分析任务（第 {attempt} 轮）...",
                        "ts": round(time.perf_counter() - t_total_start, 3),
                    })

                    yield _format_sse("thinking_start", {"tag": "think"})
                    planner_start = time.perf_counter()

                    async for text_chunk, final_plan in planner.stream_plan(
                        processed_message,
                        history=messages,
                        judge_feedback=judge_feedback,
                    ):
                        yield _format_sse("thinking", {"text": text_chunk})
                        if final_plan is not None:
                            plan = final_plan

                    yield _format_sse("thinking_end", {})

                    plan_dur = round((time.perf_counter() - planner_start) * 1000)
                    yield _format_sse("plan_result", {
                        "goal": plan.get("goal", "") if plan else "",
                        "plan": plan.get("plan", []) if plan else [],
                        "success_criteria": plan.get("success_criteria", []) if plan else [],
                        "attempt": attempt,
                        "duration_ms": plan_dur,
                        "judge_feedback_applied": bool(judge_feedback) and attempt > 1,
                    })

                    # 把 Planner 的执行计划拼进 Executor 的系统提示词
                    extra_system = _build_executor_extra_system(plan, judge_feedback, attempt)
                    saved_system = dynamic_system + extra_system
                    chain = _rebuild_chain(saved_system, llm_with_tools)

                    yield _format_sse("phase", {
                        "phase": "multi_agent",
                        "sub": "executor_start",
                        "attempt": attempt,
                        "label": f"🔧 Executor 开始执行（第 {attempt}/{max_attempts} 轮）...",
                        "ts": round(time.perf_counter() - t_total_start, 3),
                    })
                else:
                    saved_system = dynamic_system
                    chain = _rebuild_chain(saved_system, llm_with_tools)

                # 重置 Executor 轮次统计
                tool_round = 0
                total_tool_time_ms = 0
                all_tool_calls = []
                consecutive_dup_count = 0
                prev_signature = None
                total_tool_call_count = 0
                t_llm1_start = t_total_start  # 用于 phase_end 里的 duration 计算

                # ── 第一轮 LLM 流式调用（带思考分流）──
                response_chunks = []
                splitter = ThinkingStreamSplitter()
                yield _format_sse("phase", {
                    "phase": "thinking", "label": "AI 正在思考...",
                    "ts": round(time.perf_counter() - t_total_start, 3),
                    "round": 1, "step_type": "llm",
                })
                t_llm1_start = time.perf_counter()
                async for chunk in chain.astream({"messages": messages}):
                    response_chunks.append(chunk)
                    for evt in await splitter.feed(chunk):
                        yield evt
                for evt in await splitter.flush():
                    yield evt
                t_llm1_end = time.perf_counter()
                yield _format_sse("phase_end", {
                    "phase": "thinking",
                    "ts": round(t_llm1_end - t_total_start, 3),
                    "duration_ms": round((t_llm1_end - t_llm1_start) * 1000),
                    "round": 1, "step_type": "llm",
                })

                full_response = response_chunks[0]
                for c in response_chunks[1:]:
                    full_response = full_response + c

                # ── Agent 工具调用循环 ──
                tool_round = 0
                total_tool_time_ms = 0
                total_llm_time_ms = round((t_llm1_end - t_llm1_start) * 1000)
                all_tool_calls = []
                consecutive_dup_count = 0
                prev_signature = None
                total_tool_call_count = 0

                while full_response.tool_calls:
                    tool_round += 1

                    if tool_round > MAX_TOOL_ROUNDS:
                        logger.warning("Agent 工具调用达到最大轮数 %d，强制终止", MAX_TOOL_ROUNDS)
                        yield _format_sse("phase", {
                            "phase": "processing",
                            "label": f"已达到工具调用上限（{MAX_TOOL_ROUNDS} 轮），强制生成最终回答...",
                        })
                        full_response.tool_calls = []
                        break

                    batch_tc_count = len(full_response.tool_calls)
                    if total_tool_call_count + batch_tc_count > MAX_TOTAL_TOOL_CALLS:
                        logger.warning(
                            "Agent 工具调用达到总数上限 %d，强制终止（已用 %d + %d）",
                            MAX_TOTAL_TOOL_CALLS, total_tool_call_count, batch_tc_count,
                        )
                        yield _format_sse("phase", {
                            "phase": "processing",
                            "label": f"已达到工具调用上限，强制生成最终回答...",
                        })
                        full_response.tool_calls = []
                        break

                    messages.append(full_response)

                    tc_list = full_response.tool_calls
                    batch_size = len(tc_list)
                    yield _format_sse("batch_start", {
                        "round": tool_round,
                        "batch_index": tool_round,
                        "total_batches": None,
                        "tool_count": batch_size,
                        "is_parallel": batch_size > 1,
                        "ts": round(time.perf_counter() - t_total_start, 3),
                    })

                    yield _format_sse("phase", {
                        "phase": "processing",
                        "label": f"正在执行第 {tool_round} 轮操作" + (f"（{batch_size} 个工具）" if batch_size > 1 else "..."),
                        "round": tool_round, "step_type": "tools",
                        "ts": round(time.perf_counter() - t_total_start, 3),
                    })

                    t_batch_start = time.perf_counter()
                    batch_tool_times = []
                    for tc_index, tc in enumerate(tc_list):
                        tool_name = tc["name"]
                        tool_label = TOOL_LABELS.get(tool_name, tool_name)
                        tool_desc = TOOL_DESCRIPTIONS.get(tool_name, tool_label)
                        tool_icon = TOOL_ICONS.get(tool_name, "🔧")
                        args_summary = _summarize_tool_args(tool_name, tc.get("args", {}))

                        yield _format_sse("tool_start", {
                            "tool": tool_name,
                            "label": tool_label,
                            "description": tool_desc,
                            "icon": tool_icon,
                            "args_summary": args_summary,
                            "round": tool_round,
                            "index_in_batch": tc_index,
                            "batch_size": batch_size,
                            "tool_call_id": tc.get("id", ""),
                            "ts": round(time.perf_counter() - t_total_start, 3),
                        })
                        t_tc_start = time.perf_counter()

                        tool_func = TOOL_MAP.get(tool_name)
                        if tool_func:
                            raw_result = await asyncio.to_thread(tool_func.invoke, tc["args"])
                        else:
                            raw_result = f"未知工具: {tool_name}"

                        t_tc_end = time.perf_counter()
                        tool_duration_ms = round((t_tc_end - t_tc_start) * 1000)
                        batch_tool_times.append(tool_duration_ms)
                        total_tool_time_ms += tool_duration_ms

                        _record_tool_attachments(raw_result)
                        safe_result = compress_tool_result(str(raw_result))
                        messages.append(ToolMessage(content=safe_result, tool_call_id=tc["id"]))

                        result_preview = str(raw_result)[:200]
                        all_tool_calls.append({
                            "round": tool_round,
                            "tool": tool_name,
                            "label": tool_label,
                            "duration_ms": tool_duration_ms,
                        })
                        total_tool_call_count += 1

                        yield _format_sse("tool_end", {
                            "tool": tool_name,
                            "result_preview": result_preview,
                            "round": tool_round,
                            "index_in_batch": tc_index,
                            "batch_size": batch_size,
                            "duration_ms": tool_duration_ms,
                            "ts": round(t_tc_end - t_total_start, 3),
                        })

                    t_batch_end = time.perf_counter()
                    yield _format_sse("batch_end", {
                        "round": tool_round,
                        "batch_index": tool_round,
                        "tool_count": batch_size,
                        "batch_duration_ms": round((t_batch_end - t_batch_start) * 1000),
                        "tool_durations_ms": batch_tool_times,
                        "ts": round(t_batch_end - t_total_start, 3),
                    })

                    sig_parts = []
                    for tc in tc_list:
                        try:
                            sig_parts.append(tc["name"] + ":" + json.dumps(tc.get("args", {}), sort_keys=True, ensure_ascii=False))
                        except Exception:
                            sig_parts.append(tc["name"])
                    cur_signature = "|".join(sig_parts)

                    if cur_signature == prev_signature:
                        consecutive_dup_count += 1
                        if consecutive_dup_count >= 2:
                            logger.warning(
                                "Agent 检测到连续 %d 次相同工具调用，判定为死循环，强制终止",
                                consecutive_dup_count,
                            )
                            yield _format_sse("phase", {
                                "phase": "processing",
                                "label": "检测到重复工具调用，强制生成最终回答...",
                            })
                            full_response.tool_calls = []
                            break
                    else:
                        consecutive_dup_count = 0
                    prev_signature = cur_signature

                    messages, saved_system = maybe_compress_agent_context(messages, saved_system)

                    base_system = saved_system
                    tool_ctx = extract_tool_context(messages)
                    if tool_ctx:
                        base_system += tool_ctx
                    saved_system = base_system
                    chain = _rebuild_chain(base_system, llm_with_tools)

                    response_chunks = []
                    splitter2 = ThinkingStreamSplitter()
                    yield _format_sse("phase", {
                        "phase": "thinking",
                        "label": "AI 正在继续思考...",
                        "round": tool_round + 1, "step_type": "llm",
                        "ts": round(time.perf_counter() - t_total_start, 3),
                    })
                    t_llm_start = time.perf_counter()
                    async for chunk in chain.astream({"messages": messages}):
                        response_chunks.append(chunk)
                        for evt in await splitter2.feed(chunk):
                            yield evt
                    for evt in await splitter2.flush():
                        yield evt
                    t_llm_end = time.perf_counter()
                    llm_dur = round((t_llm_end - t_llm_start) * 1000)
                    total_llm_time_ms += llm_dur
                    yield _format_sse("phase_end", {
                        "phase": "thinking",
                        "round": tool_round + 1, "step_type": "llm",
                        "duration_ms": llm_dur,
                        "ts": round(t_llm_end - t_total_start, 3),
                    })

                    full_response = response_chunks[0]
                    for c in response_chunks[1:]:
                        full_response = full_response + c

                # ── 阶段 3: 生成最终回复 ──
                t_answ_start = time.perf_counter()
                yield _format_sse("phase", {
                    "phase": "answering", "label": "正在整理最终回答...",
                    "ts": round(t_answ_start - t_total_start, 3),
                    "step_type": "final",
                })

                auto_chart_paths = []
                auto_notes = ""
                if _AUTO_EXECUTE_CODE_BLOCKS and not full_response.tool_calls and full_response.content:
                    yield _format_sse("phase", {"phase": "processing", "label": "检测到代码，正在自动执行..."})
                    _, auto_chart_paths, auto_notes = _auto_execute_detected_code(full_response.content)
                    if auto_chart_paths or auto_notes:
                        full_response.content = full_response.content + auto_notes
                        yield _format_sse("phase", {"phase": "answering", "label": "已自动执行代码，正在整合结果..."})

                messages.append(AIMessage(content=full_response.content))

                with _sessions_lock:
                    if session_id in sessions:
                        sessions[session_id]["messages"] = trim_session_history(messages)
                        sessions[session_id]["last_access"] = time.time()

                reply_text = _replace_local_paths(full_response.content)

                if auto_chart_paths:
                    for ap in auto_chart_paths:
                        safe_ap = ap.replace('\\', '/')
                        reply_text += f"\n\n📊 自动生成的图表: {safe_ap}"

                logger.info(
                    "附件汇总: 图片=%d, 文件=%d (%s)",
                    len(attachments["images"]), len(attachments["files"]),
                    [a["filename"] for a in attachments["images"]],
                )

                if auto_chart_paths:
                    for ap in auto_chart_paths:
                        p = Path(ap)
                        if p.exists() and p.suffix.lower() == '.png':
                            fname = p.name
                            if fname not in [a.get('filename') for a in attachments.get('images', [])]:
                                attachments.setdefault('images', []).append({
                                    'filename': fname,
                                    'url': f'/images/{fname}'
                                })

                t_total_end = time.perf_counter()
                total_time_ms = round((t_total_end - t_total_start) * 1000)

                yield _format_sse("attachments", attachments)
                yield _format_sse("phase_end", {
                    "phase": "answering",
                    "duration_ms": round((t_total_end - t_answ_start) * 1000),
                    "ts": round(t_total_end - t_total_start, 3),
                })
                yield _format_sse("summary", {
                    "total_ms": total_time_ms,
                    "tool_calls": all_tool_calls,
                    "total_tool_time_ms": total_tool_time_ms,
                    "total_llm_time_ms": total_llm_time_ms,
                    "rounds": tool_round,
                })
                yield _format_sse("phase", {"phase": "done", "label": "完成",
                                              "ts": round(time.perf_counter() - t_total_start, 3)})
                yield _format_sse("done", {"session_id": session_id, "reply_text": reply_text,
                                        "total_ms": total_time_ms})

                # ── Judge 评审 + 循环控制（多 Agent 模式）──
                if use_multi and plan:
                    # 找到最新的 AIMessage(content=最终回答)
                    # 注意：不要在这里写 `from langchain_core.messages import AIMessage` ——
                    # 函数内的 import 会让 AIMessage 在整个 event_generator 作用域里
                    # 变成"局部变量"，导致 L1560 的 messages.append(AIMessage(...))
                    # 抛 UnboundLocalError（实测：/chat/stream 直接 error 事件）。
                    # 模块顶层（L79）已导入 AIMessage，直接用即可。
                    final_aimsg = None
                    for m in reversed(messages):
                        if isinstance(m, AIMessage) and (getattr(m, "tool_calls", None) is None):
                            final_aimsg = m.content
                            break
                    final_answer = final_aimsg or reply_text or ""
                    tool_trace = _extract_tool_trace_summary(messages)

                    judge_prompt = judge.build_judge_prompt(
                        user_message=processed_message,
                        plan=plan,
                        final_answer=final_answer,
                        tool_trace=tool_trace,
                        attempt=attempt,
                        judge_feedback=judge_feedback,
                    )

                    yield _format_sse("phase", {
                        "phase": "multi_agent",
                        "sub": "judge_start",
                        "attempt": attempt,
                        "label": f"🧐 Judge 评审（第 {attempt}/{max_attempts} 轮）...",
                        "ts": round(time.perf_counter() - t_total_start, 3),
                    })

                    yield _format_sse("thinking_start", {"tag": "think"})
                    judge_start = time.perf_counter()
                    judge_text_buf = ""
                    final_judge_result = None
                    score = 100
                    passed = True

                    async for text_chunk, final_judge_result in judge.stream_judge(judge_prompt):
                        yield _format_sse("thinking", {"text": text_chunk})
                        judge_text_buf += text_chunk
                        if final_judge_result is not None:
                            score = final_judge_result.get("score", 100)
                            passed = bool(final_judge_result.get("passed", score >= JUDGE_PASS_SCORE))

                    yield _format_sse("thinking_end", {})

                    judge_dur = round((time.perf_counter() - judge_start) * 1000)
                    yield _format_sse("judge_result", {
                        "score": score,
                        "passed": passed,
                        "strengths": final_judge_result.get("strengths", []) if final_judge_result else [],
                        "issues": final_judge_result.get("issues", []) if final_judge_result else [],
                        "feedback_for_replan": final_judge_result.get("feedback_for_replan", "") if final_judge_result else "",
                        "attempt": attempt,
                        "duration_ms": judge_dur,
                        "judge_model": os.getenv("JUDGE_MODEL") or os.getenv("LLM_MODEL", ""),
                    })

                    logger.info("Multi-Agent Judge 第 %d/%d 轮: score=%d, passed=%s", attempt, max_attempts, score, passed)

                    if passed:
                        yield _format_sse("phase", {
                            "phase": "multi_agent",
                            "sub": "pass",
                            "attempts": attempt,
                            "score": score,
                            "label": f"✅ Judge 通过！本轮闭环完成（用时 {attempt} 轮，Judge 打分 {score}）",
                            "ts": round(time.perf_counter() - t_total_start, 3),
                        })
                        break
                    elif attempt >= max_attempts:
                        yield _format_sse("phase", {
                            "phase": "multi_agent",
                            "sub": "max_retries",
                            "label": f"⚠️ 达到最大重试次数 {max_attempts}，强制通过当前结果",
                            "ts": round(time.perf_counter() - t_total_start, 3),
                        })
                        break
                    else:
                        judge_feedback = (
                            (final_judge_result or {}).get("feedback_for_replan")
                            or f"Judge 打分 {score}（及格线 {JUDGE_PASS_SCORE}）。"
                               f"问题: {'; '.join((final_judge_result or {}).get('issues', []))}"
                        )
                        yield _format_sse("phase", {
                            "phase": "multi_agent",
                            "sub": "replan",
                            "attempt": attempt + 1,
                            "score": score,
                            "feedback": judge_feedback[:300],
                            "label": f"🔁 Judge 未通过（{score} < {JUDGE_PASS_SCORE}），带着反馈重新规划...",
                            "ts": round(time.perf_counter() - t_total_start, 3),
                        })
                        # 回滚：移除本轮 Executor 产生的所有 messages
                        del messages[executor_start_idx:]
                        # 重置 session 存储里的 messages
                        with _sessions_lock:
                            if session_id in sessions:
                                sessions[session_id]["messages"] = messages
                        # 重置附件（本轮 Executor 新产生的）
                        attachments["images"] = [a for a in attachments.get("images", [])
                                                  if a.get("filename") in executor_original_attachments]
                        attachments["files"] = [a for a in attachments.get("files", [])
                                                if a.get("filename") in executor_original_attachments]
                        _seen_attach = {a.get("filename", "") for a in attachments.get("images", [])} | \
                                       {a.get("filename", "") for a in attachments.get("files", [])}
                        logger.info("Multi-Agent 回滚 Executor 产物，剩余 messages=%d，附件 images=%d, files=%d",
                                    len(messages), len(attachments.get("images", [])), len(attachments.get("files", [])))
                        continue
                else:
                    # 单 Agent 模式：执行一次即结束
                    break

        except Exception as e:
            traceback.print_exc()
            yield _format_sse("error", {"message": str(e)})
        finally:
            try:
                scope_cm.__exit__(None, None, None)
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


_IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}


def _serve_from_whitelist(filename: str, image_only: bool):
    """
    在 SERVABLE_DIRS 白名单内查找并提供文件。

    【安全】filename 必须是单一路径段：拒绝任何路径分隔符、'..'、
    绝对路径与驱动器前缀，避免穿越到白名单目录之外。
    同时拒绝敏感文件名/后缀（如 .env）作为双保险。
    """
    if not filename or filename in (".", ".."):
        raise HTTPException(status_code=404, detail="文件不存在")

    # 只允许纯文件名：拦截 ../、..\\、绝对路径、URL 编码后的分隔符
    if any(sep in filename for sep in ("/", "\\", "\x00", ":", "..")):
        logger.warning("拒绝可疑的静态资源请求: %r", filename)
        raise HTTPException(status_code=404, detail="文件不存在")

    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="文件不存在")

    lowered = filename.lower()
    if lowered in SERVABLE_DENY_NAMES or Path(lowered).suffix in SERVABLE_DENY_SUFFIXES:
        logger.warning("拒绝提供敏感文件: %r", filename)
        raise HTTPException(status_code=404, detail="文件不存在")

    for base_dir in SERVABLE_DIRS:
        candidate = (base_dir / filename).resolve()
        # 再次确认解析后的路径仍在白名单目录内（防符号链接逃逸）
        try:
            if not candidate.is_relative_to(base_dir.resolve()):
                continue
        except (OSError, ValueError):
            continue
        if not candidate.is_file():
            continue

        suffix = candidate.suffix.lower()
        if image_only:
            media_type = _IMAGE_MEDIA_TYPES.get(suffix)
            if media_type is None:
                continue
            return FileResponse(str(candidate), media_type=media_type)

        return FileResponse(
            str(candidate),
            media_type="application/octet-stream",
            filename=filename,
        )

    raise HTTPException(status_code=404, detail="图片不存在" if image_only else "文件不存在")


@app.get("/images/{filename}")
async def serve_image(filename: str):
    """提供图片访问（仅限 pang/ 与 uploads/ 白名单目录）。"""
    return _serve_from_whitelist(filename, image_only=True)


@app.get("/files/{filename}")
async def serve_file(filename: str):
    """提供文件下载（仅限 uploads/ 与 pang/ 白名单目录）。

    【安全】早期版本会回退到 Path(__file__).parent（项目根目录），
    导致 GET /files/.env 可直接下载 API 密钥，现已改为白名单目录。
    """
    return _serve_from_whitelist(filename, image_only=False)


@app.post("/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    """清空指定会话的历史记录"""
    with _sessions_lock:
        sessions.pop(session_id, None)
    return {"status": "ok"}

@app.get("/sessions")
async def list_sessions():
    """列出所有活跃会话（调试用）。"""
    with _sessions_lock:
        now = time.time()
        result = []
        for sid, data in sessions.items():
            result.append({
                "session_id": sid,
                "messages_count": len(data["messages"]),
                "last_access_age": int(now - data["last_access"]),
            })
    return {"sessions": result, "total": len(result)}

# ── 知识库 API ──────────────────────────────────────────────────────────────────

@app.get("/kb/documents")
async def list_kb_documents():
    kb = _load_kb()
    docs = []
    for d in kb["documents"]:
        docs.append({
            "id": d["id"],
            "filename": d["filename"],
            "size": d["size"],
            "uploadedAt": d["uploadedAt"],
            "content": d.get("content", "")[:200] + "..." if len(d.get("content", "")) > 200 else d.get("content", "")
        })
    return {"documents": docs}

@app.post("/kb/upload")
async def upload_kb_file(file: UploadFile = File(...)):
    MAX_KB_SIZE = 50 * 1024 * 1024
    
    file_ext = Path(file.filename).suffix.lower()
    
    content = await file.read()
    if len(content) > MAX_KB_SIZE:
        return {"error": f"文件过大，最大允许 {MAX_KB_SIZE // (1024*1024)}MB"}, 400
    
    file_id = str(uuid.uuid4())
    saved_path = KB_DIR / f"{file_id}{file_ext}"
    
    try:
        with open(saved_path, "wb") as f:
            f.write(content)
    except Exception as e:
        logger.error("知识库文件保存失败: %s", e)
        return {"error": f"文件保存失败: {str(e)}"}, 500
    
    text_content = _extract_text(saved_path)
    
    kb = _load_kb()
    doc = {
        "id": file_id,
        "filename": file.filename,
        "size": len(content),
        "uploadedAt": int(time.time() * 1000),
        "content": text_content,
        "path": str(saved_path)
    }
    kb["documents"].append(doc)
    _save_kb(kb)
    logger.info("知识库文件已上传: %s", file.filename)

    chroma_result = None
    if HAS_CHROMA:
        try:
            kb_obj = get_kb()
            chroma_result = kb_obj.add_file(saved_path, file.filename)
        except Exception as e:
            logger.error("ChromaDB 索引失败: %s", e)
            chroma_result = {"error": str(e)}
    
    return {
        "document": {
            "id": file_id, "filename": file.filename,
            "size": len(content), "uploadedAt": doc["uploadedAt"],
            "content": text_content[:200] + ("..." if len(text_content) > 200 else ""),
        },
        "chroma_index": chroma_result,
    }

@app.get("/kb/{doc_id}")
async def get_kb_document(doc_id: str):
    kb = _load_kb()
    for d in kb["documents"]:
        if d["id"] == doc_id:
            return {"document": {"id": d["id"], "filename": d["filename"], "size": d["size"], "uploadedAt": d["uploadedAt"], "content": d.get("content", "")}}
    return {"error": "Document not found"}, 404

@app.delete("/kb/{doc_id}")
async def delete_kb_document(doc_id: str):
    kb = _load_kb()
    doc = None
    for d in kb["documents"]:
        if d["id"] == doc_id:
            doc = d
            break
    if not doc:
        return {"error": "Document not found"}, 404
    kb["documents"] = [d for d in kb["documents"] if d["id"] != doc_id]
    _save_kb(kb)
    try:
        p = Path(doc.get("path", ""))
        if p.exists():
            p.unlink()
    except Exception:
        pass
    if HAS_CHROMA and doc:
        try:
            kb_obj = get_kb()
            kb_obj.remove_file(doc["filename"])
        except Exception:
            pass
    return {"status": "ok"}


@app.post("/kb/reindex")
async def reindex_kb():
    kb = _load_kb()
    updated = []
    errors = []
    chroma_result = None
    for doc in kb["documents"]:
        path_str = doc.get("path", "")
        if not path_str or not Path(path_str).exists():
            errors.append(f"{doc['filename']}: 文件不存在")
            continue
        try:
            new_content = _extract_text(Path(path_str))
            doc["content"] = new_content
            updated.append(doc["filename"])
        except Exception as e:
            errors.append(f"{doc['filename']}: {str(e)}")
    _save_kb(kb)

    if HAS_CHROMA:
        try:
            kb_obj = get_kb()
            chroma_updated = []
            chroma_errors = []
            for doc in kb["documents"]:
                path_str = doc.get("path", "")
                if not path_str or not Path(path_str).exists():
                    continue
                try:
                    result = kb_obj.add_file(Path(path_str), doc["filename"])
                    if "error" in result:
                        chroma_errors.append(f"{doc['filename']}: {result['error']}")
                    else:
                        chroma_updated.append(doc["filename"])
                except Exception as e:
                    chroma_errors.append(f"{doc['filename']}: {str(e)}")

            # 清理孤儿索引：kb_store.json 已删除但向量库仍残留的文档，
            # 否则 search_doc 会检索到"已删除"的内容
            known = {d["filename"] for d in kb["documents"]}
            orphans_removed = []
            try:
                for f in kb_obj.list_files():
                    name = f.get("filename")
                    if name and name not in known:
                        kb_obj.remove_file(name)
                        orphans_removed.append(name)
            except Exception as e:
                chroma_errors.append(f"孤儿索引清理失败: {e}")

            chroma_result = {
                "indexed": chroma_updated,
                "orphans_removed": orphans_removed,
                "errors": chroma_errors,
                "total_chunks": sum(
                    len(kb_obj.collection.get(where={"source": f})["ids"])
                    for f in chroma_updated
                ) if chroma_updated else 0,
            }
        except Exception as e:
            logger.exception("知识库重建索引失败")
            chroma_result = {"error": str(e)}

    return {
        "updated": updated,
        "errors": errors,
        "total": len(kb["documents"]),
        "chroma": chroma_result,
    }


@app.get("/kb/chroma/list")
async def list_chroma_files():
    if not HAS_CHROMA:
        return {"error": "ChromaDB 未安装", "files": []}
    try:
        kb_obj = get_kb()
        files = kb_obj.list_files()
        return {"files": files, "total": len(files)}
    except Exception as e:
        return {"error": str(e), "files": []}


@app.post("/kb/chroma/search")
async def search_chroma(query: str, top_k: int = 5, mode: str = "balanced"):
    """
    直接检索向量知识库（调试/集成用）。

    参数校验：top_k 会被传给 ChromaDB 的 n_results，负数或超大值会让
    底层抛异常；mode 限制在管道支持的三种取值内。
    """
    if not HAS_CHROMA:
        return {"error": "ChromaDB 未安装", "results": []}
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    if top_k < 1 or top_k > 50:
        raise HTTPException(status_code=400, detail="top_k 必须在 1~50 之间")
    if mode not in ("fast", "balanced", "quality"):
        raise HTTPException(status_code=400, detail="mode 必须是 fast / balanced / quality")
    try:
        kb_obj = get_kb()
        results = kb_obj.search(query, top_k, mode)
        return {"results": results, "total": len(results)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("向量检索失败")
        return {"error": str(e), "results": []}


# 是否自动执行模型回复中的 Python 代码块（危险，默认关闭）
_AUTO_EXECUTE_CODE_BLOCKS = os.getenv("AUTO_EXECUTE_CODE_BLOCKS", "0").strip() in ("1", "true", "True", "yes")


def search_knowledge_base(query: str, top_k: int = 3) -> str:
    return search_knowledge_base_from(_load_kb(), query, top_k)


# chunk 级 BM25 索引缓存：(_kb_signature, index_state)
_kb_bm25_cache: dict = {
    "signature": None, "chunks": [], "token_sets": [], "bm25": None, "doc_meta": [],
}


def _kb_signature(kb: dict) -> tuple:
    """知识库指纹，用于判断缓存是否失效。"""
    return tuple(
        (d.get("id", ""), len(d.get("content", "") or ""))
        for d in kb.get("documents", [])
    )


def _get_kb_bm25(kb: dict):
    """
    构建/复用 chunk 级 BM25 索引。

    返回 (chunks, token_sets, bm25, doc_meta)；知识库为空或构建失败时返回全 None。
    使用 BM25 而非「关键词是否出现」的原因：BM25 带 IDF 权重，
    常见词（如"公司"）权重低、特征词（如"迟到"）权重高。
    """
    signature = _kb_signature(kb)
    if _kb_bm25_cache["signature"] == signature and _kb_bm25_cache["bm25"] is not None:
        return (_kb_bm25_cache["chunks"], _kb_bm25_cache["token_sets"],
                _kb_bm25_cache["bm25"], _kb_bm25_cache["doc_meta"])

    try:
        from knowledge_base import SimpleBM25, _tokenize, chunk_text_semantic

        chunks = []
        token_sets = []
        doc_meta = []
        for doc in kb.get("documents", []):
            content = doc.get("content", "") or ""
            if not is_valid_content(content):
                continue

            # 与 knowledge_base.chunk_text_semantic 保持一致的分块粒度（500/80）。
            # 早前这里用 800 字符粗切，导致「打分用的块」和「返回给模型的块」
            # 不是同一段文本：BM25 可能因为某段含有特征词而给它高分，
            # 但返回的却是该段所属的那 800 字大块（甚至被覆盖阈值截掉），
            # 表现出来就是"找对了文档，但给出的片段里没有答案"。
            pieces = chunk_text_semantic(content, 500, 80)
            for piece_item in pieces:
                piece = piece_item["text"] if isinstance(piece_item, dict) else piece_item
                page = piece_item.get("page", 0) if isinstance(piece_item, dict) else 0
                if not piece or len(piece.strip()) < 30:
                    continue
                chunks.append(piece)
                token_sets.append(set(_tokenize(piece)))
                doc_meta.append({
                    "filename": doc.get("filename", "未知"),
                    "path": doc.get("path", ""),
                    "page": page,
                })

        if not chunks:
            _kb_bm25_cache.update({"signature": signature, "chunks": [],
                                   "token_sets": [], "bm25": None, "doc_meta": []})
            return None, None, None, None

        bm25 = SimpleBM25()
        bm25.build(chunks, [str(i) for i in range(len(chunks))])

        _kb_bm25_cache.update({
            "signature": signature,
            "chunks": chunks,
            "token_sets": token_sets,
            "bm25": bm25,
            "doc_meta": doc_meta,
        })
        logger.info("知识库 BM25 索引已构建: %d 个 chunk", len(chunks))
        return chunks, token_sets, bm25, doc_meta
    except Exception as e:
        logger.warning("知识库 BM25 索引构建失败: %s", e)
        return None, None, None, None


# 相关性门控参数。
#
# 为什么需要门控：BM25 的 IDF 在几百个 chunk 的规模下，单个常见词也能拿到
# 1.0 分以上；若只看绝对分数，"你好" 这类闲聊照样会捞进大段文档。
#
# 为什么用「IDF 加权覆盖率」而不是简单的命中比例：
# 中文分词是字符 n-gram，一个问句会切出 40+ 个相互重叠的 token，其中大量是
# 「怎么」「什么」这类高频虚词 n-gram —— 它们在几乎所有中文文本里都出现，
# 只要命中几个就能把覆盖率抬上去，于是「今天天气怎么样」也能凑够阈值。
# ── 知识库注入门控参数 ────────────────────────────────────────────────────────
# 门控要解决：哪些片段值得塞进系统提示词。
# 定标过程（用 12 条真实提问 + 9 条闲聊/无关查询实测）：
#   1) 单看 BM25 绝对分：闲聊也能拿高分，无用。
#   2) 单看「命中 token 比例」：中文 n-gram 会切出「天天」「个快」这类罕见组合，
#      覆盖率虚高，「今天天气怎么样」能达到 0.62 —— 仍会误放。
#   3) 单看语义相似度：本嵌入模型对「1+1等于几」给出 0.4879，
#      高于真实提问「迟到早退怎么处罚」的 0.4165 —— 短查询上分布重叠，
#      任何单阈值都必然误判。
#   4) 最终用「语义阈值 AND 词面重合」双条件：两个信号同时成立才注入。
#      实测 相关通过 11/12、垃圾通过 1/9（那条是"帮我写个快速排序"，
#      对一本含代码的 NLP 教材而言并非完全无关，可接受）。
KB_SEMANTIC_THRESHOLD = 0.45         # 语义相似度下限
KB_LEXICAL_OVERLAP_THRESHOLD = 0.10  # IDF 加权词面重合下限（辅助条件）
KB_WEIGHTED_COVERAGE_THRESHOLD = 0.55  # 无嵌入时的兜底阈值（纯词面）
# 只要求命中 1 个不同 token：长查询里大量 n-gram 是 OOV，
# 真正能对上词表的往往只有一两个（例：「我不想干了要提前多久说」只对上「提前」）。
# 精度由语义阈值兜底，这里放宽以保住召回。
KB_MIN_MATCHED_TOKENS = 1
KB_MIN_QUERY_TOKENS = 2              # 查询 token 少于此值时不注入
KB_RELATIVE_SCORE_FLOOR = 0.2        # 相对最佳命中的分数下限


def _kb_semantic_scores(query: str, texts: list):
    """
    用知识库当前嵌入后端给候选文本打分。

    返回 list[float] 或 None（无可用嵌入时）。
    """
    try:
        from knowledge_base import get_kb
        kb_obj = get_kb()
        if kb_obj is None:
            return None
        embed_fn = kb_obj._get_embedding_fn()
        if embed_fn is None:
            return None
        import numpy as np

        qv = np.asarray(embed_fn([query])[0], dtype=np.float32)
        dv = np.asarray(embed_fn(list(texts)), dtype=np.float32)
        qn = float(np.linalg.norm(qv)) or 1.0
        dn = np.linalg.norm(dv, axis=1)
        dn[dn == 0] = 1.0
        return (dv @ qv / (dn * qn)).tolist()
    except Exception as e:
        logger.debug("知识库语义打分失败，退回纯词面门控: %s", e)
        return None


def search_knowledge_base_from(kb: dict, query: str, top_k: int = 3) -> str:
    """
    按相关性检索知识库并返回可注入提示词的片段。

    修复要点：
      - 原实现用「关键词是否出现」计分，中文 bigram/trigram 几乎必然命中，
        任何中文提问（含"你好"）都会注入数千字无关文档。
      - 现为 BM25 召回 + 「语义相似度 AND 词面重合」双门控，
        闲聊/无关问题不注入任何内容，真正相关的片段不会被误杀。
    """
    if not kb.get("documents"):
        return ""
    if not query or not query.strip():
        return ""

    try:
        from knowledge_base import _tokenize
    except Exception:
        return ""

    query_tokens = set(_tokenize(query))
    if len(query_tokens) < KB_MIN_QUERY_TOKENS:
        return ""

    chunks, token_sets, bm25, doc_meta = _get_kb_bm25(kb)
    if not chunks or bm25 is None:
        return ""

    scores = bm25.get_scores(query)
    if not scores:
        return ""

    # 查询词的 IDF 权重。分母只累计**词表内**的 token：
    # 查询里很多 n-gram 在语料中从未出现（OOV），若也算进分母，
    # 重合度会被系统性稀释，导致相关片段被误杀。
    idf = getattr(bm25, "idf", {}) or {}
    weights_in_vocab = {t: idf[t] for t in query_tokens if t in idf and idf[t] > 0}
    total_weight = sum(weights_in_vocab.values())
    if total_weight <= 0:
        return ""

    max_score = max(scores.values()) or 1.0
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # 先按 BM25 取前 N 个候选，只对它们算语义分（避免整库编码）
    candidate_idx = []
    for chunk_id, score in ranked:
        try:
            idx = int(chunk_id)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(chunks) and score >= KB_RELATIVE_SCORE_FLOOR * max_score:
            candidate_idx.append(idx)
        if len(candidate_idx) >= 40:
            break

    if not candidate_idx:
        return ""

    sem_scores = _kb_semantic_scores(query, [chunks[i] for i in candidate_idx])
    use_semantic = sem_scores is not None

    hits = []
    for pos, idx in enumerate(candidate_idx):
        present = query_tokens & token_sets[idx]
        if len(present) < KB_MIN_MATCHED_TOKENS:
            continue
        overlap = sum(weights_in_vocab.get(t, 0.0) for t in present) / total_weight

        if use_semantic:
            # 双条件：语义像 且 词面有重合
            if sem_scores[pos] < KB_SEMANTIC_THRESHOLD:
                continue
            if overlap < KB_LEXICAL_OVERLAP_THRESHOLD:
                continue
            relevance = sem_scores[pos]
        else:
            # 无嵌入后端时退回纯词面门控
            if overlap < KB_WEIGHTED_COVERAGE_THRESHOLD:
                continue
            relevance = overlap

        meta = doc_meta[idx]
        doc_key = meta["filename"]
        # 同一文档最多取 2 个片段，保证多文档覆盖
        if sum(1 for h in hits if h[0] == doc_key) >= 2:
            continue
        hits.append((doc_key, chunks[idx], meta, relevance, overlap))
        if len(hits) >= top_k:
            break

    if not hits:
        return ""

    parts = []
    for _fname, text, meta, _rel, _ov in hits:
        path_hint = f"\n[文件路径: {meta['path']}]" if meta.get("path") else ""
        parts.append(f"[文档: {meta['filename']}{path_hint}]\n{text}")
    logger.debug(
        "知识库注入 %d 个片段 (查询 token=%d, 语义门控=%s)",
        len(hits), len(query_tokens), "开" if use_semantic else "关",
    )
    return "\n\n---\n\n".join(parts)


def _get_kb_file_list() -> str:
    return _get_kb_file_list_from(_load_kb())


def _get_kb_file_list_from(kb: dict) -> str:
    if not kb.get("documents"):
        return ""
    parts = ["【知识库文件列表】以下是知识库中所有可用的文件，你可以使用 read_file 工具读取完整内容："]
    for doc in kb["documents"]:
        if not is_valid_content(doc.get("content", "")):
            continue
        parts.append(f"  - {doc['filename']} (路径: {doc.get('path', '未知')})")
    return "\n".join(parts) if len(parts) > 1 else ""


# ── 知识库索引自愈 ─────────────────────────────────────────────────────────────
# 背景：知识库存在两套存储 —— kb_store.json（提示词注入用）与 ChromaDB
# （search_doc 工具用）。早期版本上传/删除只保证前者有数据，ChromaDB 可能
# 长期为空，导致 Agent 调用 search_doc 永远返回"知识库中暂无文档"。
# 这里在启动时做一次对账，把缺失的文档补进 ChromaDB。

def _sync_kb_index() -> dict:
    """把 kb_store.json 中有、但 ChromaDB 中缺失的文档补索引。"""
    if not HAS_CHROMA:
        return {"skipped": "ChromaDB 未安装"}

    kb = _load_kb()
    docs = kb.get("documents", [])
    if not docs:
        return {"skipped": "知识库为空"}

    try:
        kb_obj = get_kb()
    except Exception as e:
        logger.error("知识库初始化失败: %s", e)
        return {"error": str(e)}

    try:
        indexed = {f["filename"] for f in kb_obj.list_files()}
    except Exception as e:
        logger.error("读取 ChromaDB 索引失败: %s", e)
        return {"error": str(e)}

    added, skipped, errors = [], [], []
    for doc in docs:
        filename = doc.get("filename", "")
        path = doc.get("path", "")
        if not filename or not path:
            continue
        if filename in indexed:
            skipped.append(filename)
            continue
        if not Path(path).exists():
            errors.append(f"{filename}: 源文件不存在")
            continue
        try:
            result = kb_obj.add_file(Path(path), filename)
            if "error" in result:
                errors.append(f"{filename}: {result['error']}")
            else:
                added.append(filename)
                logger.info("知识库索引补齐: %s (%d chunks)", filename, result.get("chunks", 0))
        except Exception as e:
            errors.append(f"{filename}: {str(e)}")

    return {"added": added, "already_indexed": skipped, "errors": errors}


@app.get("/runtime/info")
async def runtime_info(request: Request):
    """
    只读运行时信息，供前端「设置 → 模型」页展示。

    只暴露模型名、温度、嵌入后端、工具数量，以及**打了码**的密钥首尾；
    绝不返回完整 API 密钥或本地文件路径。
    """
    embedding = None
    try:
        if HAS_CHROMA:
            kb_obj = get_kb()
            if kb_obj is not None:
                embedding = getattr(kb_obj, "_embedding_backend", None)
    except Exception:
        embedding = None

    tool_names = sorted(k for k, v in TOOL_MAP.items() if v is not None)

    return {
        "model": os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_ID") or "未知",
        "temperature": os.getenv("LLM_TEMPERATURE", ""),
        "embedding": embedding,
        "tools": len(tool_names),
        "tool_names": tool_names,
        "workspace": str(utils_get_base_dir()),
        "app_dir": str(APP_DIR),
        # 接口配置（密钥仅首尾），与 /settings/api 同源，供「模型」页一次性取全
        **_api_settings_view(request),
    }


# ── 工作区 API ─────────────────────────────────────────────────────────────────
# 一个「工作区」= 用户在本机选择的文件夹。Agent 的文件读写 / 命令行 / 知识库
# 都以当前工作区为根。会话记录由前端按工作区分别保存。

import workspaces as _ws  # noqa: E402


@app.get("/workspaces")
async def list_workspaces():
    """列出已登记的工作区，并附带当前激活项。"""
    info = _ws.list_workspaces()
    aid = info.get("active_id")
    info["active"] = next((w for w in info["workspaces"] if w["id"] == aid), None)
    info["base_dir"] = str(utils_get_base_dir())
    return info


@app.post("/workspaces")
async def create_workspace(payload: dict):
    """
    新增并激活一个工作区。

    folder 模式（默认）:
        body: {"path": "D:\\code\\demo", "name": "可选名称"}
        path 必须是本机已存在的文件夹。

    files 模式:
        body: {"kind": "files", "files": ["D:\\a.py", "D:\\b.py"], "name": "可选名称"}
        files 是文件路径列表，系统会自动读取每个文件的全部内容注入提示词。
    """
    kind = (payload or {}).get("kind", "folder")
    name = (payload or {}).get("name", "")

    if kind == "files":
        files_list = (payload or {}).get("files", [])
        try:
            result = _ws.add_workspace("", name=name or "", kind="files", files=files_list)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if result["created"]:
            try:
                _ws.activate(result["workspace"]["id"])
            except ValueError:
                pass
        return {"workspace": result["workspace"], "created": result["created"],
                "active_id": result["workspace"]["id"],
                "base_dir": str(utils_get_base_dir())}

    path = (payload or {}).get("path", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path")
    try:
        result = _ws.add_workspace(path, name)
        active = _ws.activate(result["workspace"]["id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"workspace": result["workspace"], "created": result["created"],
            "active": active, "base_dir": str(utils_get_base_dir())}


@app.post("/workspaces/{ws_id}/activate")
async def activate_workspace(ws_id: str):
    """切换当前工作区。之后所有工具都以新根为基准。"""
    try:
        active = _ws.activate(ws_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 子进程的 cwd 与环境变量是启动时确定的，切换工作区后必须重建，
    # 否则代码执行仍停留在旧工作区（实测过）
    try:
        get_executor().restart_for_workspace()
    except Exception as e:
        logger.warning("切换工作区后重建代码环境失败: %s", e)
    return {"active": active, "base_dir": str(utils_get_base_dir())}


@app.patch("/workspaces/{ws_id}")
async def patch_workspace(ws_id: str, payload: dict):
    """重命名工作区（只改显示名，不动磁盘路径）。"""
    name = (payload or {}).get("name", "")
    try:
        return {"workspace": _ws.rename_workspace(ws_id, name)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/workspaces/{ws_id}")
async def delete_workspace(ws_id: str, delete_index: bool = False):
    """
    移除工作区登记。

    delete_index=True 时仅清理应用自己的知识库索引；
    **不会**删除用户磁盘上的文件夹。
    """
    try:
        result = _ws.remove_workspace(ws_id, delete_files=delete_index)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@app.get("/workspaces/browse")
async def browse_filesystem(path: str = "", show_hidden: bool = False,
                            include_files: bool = False, file_select: bool = False):
    """
    浏览本机目录。

    include_files=True 时同时返回文件条目，用于"选择文件工作区"模式。
    file_select=True 时前端会把文件作为可选项（支持多选）。
    """
    try:
        return _ws.browse(path, show_hidden=show_hidden,
                          include_files=include_files, file_select=file_select)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/workspaces/{ws_id}/refresh")
async def refresh_file_workspace(ws_id: str):
    """
    刷新文件工作区：重新从磁盘读取文件内容。
    folder 类型工作区不支持此操作。
    """
    try:
        return _ws.refresh_file_workspace(ws_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/workspaces/mkdir")
async def make_folder(payload: dict):
    """在指定目录下新建文件夹（供「新建文件夹」按钮用）。"""
    parent = (payload or {}).get("parent", "").strip()
    name = (payload or {}).get("name", "")
    if not parent:
        raise HTTPException(status_code=400, detail="缺少 parent")
    try:
        return _ws.create_folder(parent, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── 工作区文件树（相对当前工作区根，懒加载）─────────────────────────────────────

# WORKSPACE_SKIP_DIRS 定义在文件前部（_workspace_snapshot 也用它）
# 不展示的文件/后缀（密钥与运行时产物）
WORKSPACE_SKIP_FILES = {".env", ".env.local", ".env.production"}
WORKSPACE_SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".lock"}
WORKSPACE_MAX_ENTRIES = 300


@app.get("/workspace/tree")
async def workspace_tree(dir: str = ""):
    """
    返回**当前工作区**下某一层的目录项（懒加载）。

    dir 为相对工作区根的路径，留空表示根目录。
    只能访问工作区根以内 —— 越界返回 403。
    """
    root = utils_get_base_dir().resolve()

    rel = (dir or "").strip().replace("\\", "/").strip("/")

    # 显式的可疑路径段校验。真正的边界是下面的 is_relative_to；
    # 这一层把明显越界的请求直接拒掉并给出更明确的错误，
    # 而不是等到 resolve() 之后才发现（例如 "a/../../.." 会跑到盘符根）。
    if rel:
        segments = [s for s in rel.split("/") if s not in ("", ".")]
        if any(s == ".." or (len(s) > 1 and set(s) == {"."}) for s in segments):
            logger.warning("拒绝可疑的工作区路径: %r", dir)
            raise HTTPException(status_code=403, detail="路径不合法")

    target = root if not rel else (root / rel)
    try:
        target = target.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="路径无法解析")

    if target != root and not target.is_relative_to(root):
        logger.warning("拒绝越界的工作区请求: %r", dir)
        raise HTTPException(status_code=403, detail="只能访问工作区目录内的路径")

    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在: {dir or '.'}")

    dirs, files = [], []
    try:
        for entry in os.scandir(target):
            name = entry.name
            if entry.is_dir(follow_symlinks=False):
                if name in WORKSPACE_SKIP_DIRS or name.startswith("."):
                    continue
                dirs.append({
                    "name": name,
                    "path": str(Path(rel) / name).replace("\\", "/") if rel else name,
                    "type": "dir",
                })
            elif entry.is_file(follow_symlinks=False):
                if name in WORKSPACE_SKIP_FILES:
                    continue
                if Path(name).suffix.lower() in WORKSPACE_SKIP_SUFFIXES:
                    continue
                try:
                    st = entry.stat()
                    size, mtime = st.st_size, int(st.st_mtime)
                except OSError:
                    size, mtime = 0, 0
                files.append({
                    "name": name,
                    "path": str(Path(rel) / name).replace("\\", "/") if rel else name,
                    "type": "file",
                    "size": size,
                    "mtime": mtime,
                })
    except PermissionError:
        raise HTTPException(status_code=403, detail="没有权限读取该目录")

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    entries = dirs + files
    truncated = len(entries) > WORKSPACE_MAX_ENTRIES
    if truncated:
        entries = entries[:WORKSPACE_MAX_ENTRIES]

    return {
        "root": str(root),
        "root_name": root.name or str(root),
        "path": rel,
        "entries": entries,
        "truncated": truncated,
        "total": len(entries),
    }


# ── 工作区文件内容预览（只读，供界面上的「文件」面板）──────────────────────────
# 单次预览上限：超过就截断，避免一个几百 MB 的日志把浏览器卡死
WORKSPACE_PREVIEW_MAX_BYTES = 256 * 1024


@app.get("/workspace/file")
async def read_workspace_file(path: str = ""):
    """
    读取**当前工作区**内某个文本文件的内容（只读，用于界面预览）。

    【安全边界说明】这个接口确实按设计读取工作区文件 —— 它是「查看工作区文件」
    的唯一用途，与 /files、/images 的约束并不冲突：那两个接口是**对外提供下载**，
    所以绝不能包含工作区；这里则是用户在界面上点开自己选定的工作区里的一个文件，
    且：只读、只服务当前工作区（resolve 后校验仍在根内，防 ../ 与符号链接逃逸）、
    仅文本（检出 NUL 字节即视为二进制拒绝）、单次上限 256KB、
    拒绝 .env / 密钥 / 证书类文件名与后缀。
    """
    root = utils_get_base_dir().resolve()
    rel = (path or "").strip().replace("\\", "/").strip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="缺少 path")

    segments = [s for s in rel.split("/") if s not in ("", ".")]
    if any(s == ".." for s in segments):
        logger.warning("拒绝可疑的工作区文件请求: %r", path)
        raise HTTPException(status_code=403, detail="路径不合法")

    try:
        target = (root / Path(*segments)).resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="路径无法解析")

    if target != root and not target.is_relative_to(root):
        logger.warning("拒绝越界的工作区文件请求: %r", path)
        raise HTTPException(status_code=403, detail="只能读取当前工作区内的文件")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

    name = target.name
    suffix = target.suffix.lower()
    if (name in WORKSPACE_SKIP_FILES or name in SERVABLE_DENY_NAMES
            or suffix in SERVABLE_DENY_SUFFIXES or suffix in WORKSPACE_SKIP_SUFFIXES):
        logger.warning("拒绝预览敏感文件: %r", path)
        raise HTTPException(status_code=403, detail="该文件不允许在界面中预览")

    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            raw = fh.read(WORKSPACE_PREVIEW_MAX_BYTES)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    # 二进制识别：文本文件里不该出现 NUL。不做编码嗅探，解不出来就当二进制。
    if b"\x00" in raw[:8192]:
        raise HTTPException(status_code=415, detail="二进制文件不支持预览")

    truncated = size > WORKSPACE_PREVIEW_MAX_BYTES
    text = raw.decode("utf-8", errors="replace")
    return {
        "name": name,
        "path": rel,
        "size": size,
        "truncated": truncated,
        "lines": text.count("\n") + 1,
        "content": text,
    }


# ── 页面预览（当前工作区的 pages/ 子目录）──────────────────────────────────────
# Agent 用 create_page 生成的页面写在这里，前端「页面」面板用 iframe 预览。
#
# 【为什么单独做一套服务，而不是直接挂 StaticFiles】页面目录位于**用户选定的
# 工作区**里，路径随工作区切换而变；而 StaticFiles 只能在启动时绑定一个目录。
# 更要紧的是安全边界：只放行 pages/ 这一个子目录，绝不放宽整个工作区 ——
# 一旦能顺着工作区根提供服务，把工作区指向含密钥的项目就等于开了下载口
# （README 里 /files/.env 那次事故就是同一个教训）。

# 后缀 → 预览用的 MIME 类型。用**白名单**：不在表里的后缀一律 404，
# 不依赖浏览器的类型嗅探，pages/ 里的其他文件也就没有对外出口。
PAGE_PREVIEW_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
}


def _current_pages_dir() -> Path:
    """当前工作区下的 pages/ 目录（可能尚不存在）。"""
    return utils_get_base_dir() / PAGES_DIR_NAME


def _resolve_page_request(name: str) -> Path:
    """
    把 /pages/file/{name} 解析为 pages/ 目录内的真实文件路径。

    路径经 resolve() 后仍要求落在 pages/ 之内：单纯检查字符串里的 `..`
    挡不住符号链接/junction 逃逸（Windows 上目录联接很常见）。
    """
    root = _current_pages_dir().resolve()
    raw = (name or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise HTTPException(status_code=404, detail="页面不存在")

    segments = [s for s in raw.split("/") if s not in ("", ".")]
    if not segments or any(s == ".." for s in segments):
        logger.warning("拒绝可疑的页面请求: %r", name)
        raise HTTPException(status_code=403, detail="路径不合法")

    filename = segments[-1].lower()
    if filename in SERVABLE_DENY_NAMES or Path(filename).suffix in SERVABLE_DENY_SUFFIXES:
        logger.warning("拒绝提供敏感文件: %r", name)
        raise HTTPException(status_code=404, detail="页面不存在")

    try:
        target = (root / Path(*segments)).resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="路径无法解析")

    if target != root and not target.is_relative_to(root):
        logger.warning("拒绝越界的页面请求: %r", name)
        raise HTTPException(status_code=403, detail="只能访问当前工作区 pages/ 目录内的文件")
    return target


@app.get("/pages")
async def list_pages_api():
    """列出当前工作区 pages/ 目录下的页面与配套资源（供前端「页面」面板）。"""
    root = _current_pages_dir()
    ws_root = utils_get_base_dir()
    pages, total_size = [], 0

    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(root).as_posix()
            suffix = Path(rel).suffix.lower()
            if suffix not in ALLOWED_PAGE_SUFFIXES:
                continue
            try:
                st = f.stat()
                size, mtime = st.st_size, int(st.st_mtime)
            except OSError:
                size, mtime = 0, 0
            total_size += size
            pages.append({
                "name": rel,
                "size": size,
                "mtime": mtime,
                "is_page": suffix in (".html", ".htm"),
                "url": f"/pages/file/{rel}",
            })

    # 页面排在资源前面 —— 用户要看的是页面，.css/.js 只是它们的附属品
    pages.sort(key=lambda p: (not p["is_page"], p["name"].lower()))
    return {
        "workspace": str(ws_root),
        "workspace_name": ws_root.name or str(ws_root),
        "dir": str(root),
        "exists": root.is_dir(),
        "pages": pages,
        "total": len(pages),
        "total_size": total_size,
    }


# 编辑模式（预览面板的「编辑页面」）下注入的元素选取脚本。
# 脚本本身由前端静态挂载提供（frontend/picker.js → GET /picker.js）。
PICKER_SCRIPT_TAG = '<script src="/picker.js" data-dsh-picker="1"></script>'


def _inject_picker_script(html: str) -> str:
    """
    把元素选取脚本追加到**文档末尾**。

    【为什么不用「插到最后一个 </body> 之前」】`</body>` 这个字面量在页面里
    可能出现多次：HTML 注释、JS 字符串（document.write('</body>')）、
    <template> 里的文本等。用 rfind 定位会被这些假标签骗到，把 <script>
    插进注释内部 —— 结果是标签既不进 DOM 也不执行，而且**完全不报错**
    （实测踩到过：探针显示 tagPresent=false，排查了半天）。
    追加到末尾则由 HTML 解析器把它当作普通尾部内容收进 body 并执行，
    不受注释/字符串里的假标签影响。

    【为什么要注入到页面内部】预览 iframe 带 sandbox 且刻意不含
    allow-same-origin —— 父页面拿不到它的 contentDocument，选取逻辑只能
    跑在页面里，再通过 postMessage 回传（父页面用 event.source 校验来源）。
    """
    return html + "\n" + PICKER_SCRIPT_TAG + "\n"


@app.get("/pages/file/{name:path}")
async def serve_page(name: str, edit: int = 0):
    """
    提供页面预览（仅限当前工作区 pages/ 内的静态资源）。

    edit=1 时在 HTML 里注入元素选取脚本（「编辑页面」模式）。
    其余情况原样返回文件。
    """
    target = _resolve_page_request(name)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"页面不存在: {name}")

    suffix = target.suffix.lower()
    media_type = PAGE_PREVIEW_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(status_code=404, detail="不支持预览该类型文件")

    # no-store：Agent 改完页面后，预览必须立刻反映新内容。
    # 不加这个头，浏览器会拿缓存，用户会以为「改了没生效」。
    headers = {"Cache-Control": "no-store, must-revalidate"}

    if edit and suffix in (".html", ".htm"):
        try:
            html = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"读取失败: {e}")
        return HTMLResponse(
            content=_inject_picker_script(html),
            media_type=media_type,
            headers=headers,
        )

    return FileResponse(str(target), media_type=media_type, headers=headers)


@app.delete("/pages/file/{name:path}")
async def delete_page_file(name: str):
    """删除页面文件（供界面上的删除按钮使用，只作用于 pages/ 目录内）。"""
    target = _resolve_page_request(name)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"页面不存在: {name}")
    try:
        target.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    logger.info("已删除页面文件: %s", target)
    return {"status": "ok", "deleted": name}


# ── 静态资源挂载（必须在所有 API 路由之后注册）────────────────────────────────
# 挂载在 "/" 上会兜住 /styles.css、/script.js 等相对路径请求。
# Starlette 按注册顺序匹配，API 路由先注册因此优先级更高；未命中的请求
# 才会落到这里。需要局域网访问时注意：本挂载只在 frontend/ 目录内提供文件。
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning("前端目录不存在，跳过静态资源挂载: %s", FRONTEND_DIR)


# ── 启动入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    # 【安全】默认只监听回环地址，避免把带 API 密钥的服务暴露到局域网。
    # 确需对外提供时，在 .env 里显式设置 HOST=0.0.0.0。
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8001"))
    logger.info("喵喵助手后端启动中...")
    logger.info("访问地址: http://%s:%d", "localhost" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    if host == "0.0.0.0":
        logger.warning("正在监听 0.0.0.0，服务对局域网可见，请确认已轮换密钥并收窄 CORS_ORIGINS")
    uvicorn.run(app, host=host, port=port, log_level="info")