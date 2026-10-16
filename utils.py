"""
utils.py - 公共工具模块
统一管理文件路径、文本提取、路径安全检查等公共逻辑，
消除 backend.py / knowledge_base.py / code_executor_tool.py 之间的重复代码。
"""

import os
import json
import threading
import logging
from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager
from collections import defaultdict, deque
from typing import Optional

logger = logging.getLogger("NewAgent.utils")

# ── 路径常量 ─────────────────────────────────────────────────────────────────────
# 这里区分两类目录，改动前务必看清：
#
#   APP_DIR      应用自身所在目录（D:\...\NewAgent）——**永不改变**。
#                代码、.env、知识库索引、上传缓存、生成的图表都放这里。
#   BASE_DIR     当前「工作区」根目录 ——**可切换**，由用户选择的本地文件夹决定。
#                Agent 读写文件、跑命令的默认位置。
#
# 注意：其它模块**不要**写 `from utils import BASE_DIR` —— 那会在导入时
# 拷贝一份值，之后切换工作区就失效了。要用 `utils.get_base_dir()`
# 或在函数内部引用 `utils.BASE_DIR`。
APP_DIR = Path(__file__).parent.resolve()

BASE_DIR = APP_DIR  # 当前工作区根，初始即应用目录（向后兼容旧行为）

UPLOADS_DIR = APP_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

CHART_OUTPUT_DIR = APP_DIR / "pang"
CHART_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

KB_DIR = APP_DIR / "knowledge_base"
KB_DIR.mkdir(exist_ok=True)

TASKS_DIR = APP_DIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)

WORKSPACES_DIR = APP_DIR / ".workspaces"
WORKSPACES_DIR.mkdir(exist_ok=True)

# ── 当前工作区根 ──────────────────────────────────────────────────────────────
# 用 ContextVar 而不是普通全局变量。
#
# 踩过的坑：最初用模块级全局 + 可重入锁，实测并发下仍然串味 ——
# 两个 SSE 生成器跑在**同一个事件循环线程**里，A 在 await（等 LLM）时
# B 插入执行，锁对"同一线程内的协作式嵌套"毫无作用：
#   [ws-scope] enter 深度=1 根=D:\dsh
#   [ws-scope] enter 深度=2 根=D:\java-demo     ← B 套在 A 里面
#   A 恢复后用工具时，全局根已经是 B 的了
#
# ContextVar 按「异步上下文」隔离：每个请求任务各持一份，
# 且能自动穿透 asyncio.to_thread（工具在线程池里执行也读得到正确值）。
_ws_root: ContextVar[Optional[Path]] = ContextVar("newagent_ws_root", default=None)


def _resolve_fallback_root() -> Path:
    """ContextVar 没设置时，从 registry 找当前激活工作区的路径。"""
    try:
        from workspaces import list_workspaces as _lw
        info = _lw()
        aid = info.get("active_id")
        if aid:
            for w in info.get("workspaces", []):
                if w.get("id") == aid:
                    p = Path(w.get("path", ""))
                    if p.is_dir():
                        return p.resolve()
    except Exception:
        pass
    return APP_DIR


def get_base_dir() -> Path:
    """返回当前工作区根目录（按上下文隔离，支持并发请求各用各的）。"""
    root = _ws_root.get()
    if root is not None:
        return root
    return _resolve_fallback_root()


def get_app_dir() -> Path:
    """返回应用自身目录（固定不变）。"""
    return APP_DIR


def set_base_dir(path) -> Path:
    """
    切换当前上下文的工作区根目录，并同步刷新依赖它的白名单。

    只接受已存在的目录；不存在时抛 ValueError。
    """
    global BASE_DIR, ALLOWED_READ_ROOTS, ALLOWED_WRITE_ROOTS
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"无法解析路径: {path} ({e})")
    if not p.is_dir():
        raise ValueError(f"目录不存在: {p}")

    _ws_root.set(p)
    BASE_DIR = p  # 兼容仍以模块属性方式读取的代码
    ALLOWED_READ_ROOTS = [p, APP_DIR, UPLOADS_DIR, KB_DIR, CHART_OUTPUT_DIR, TASKS_DIR]
    ALLOWED_WRITE_ROOTS = [p, UPLOADS_DIR, CHART_OUTPUT_DIR]
    return p


@contextmanager
def workspace_scope(path):
    """
    在 with 块内把工作区根切到 path，退出后还原到进入前的值。

    用于处理一个请求时绑定它所属的工作区。因为基于 ContextVar，
    并发请求互不影响（各在自己的上下文里）。

    path 为 None 或空时，会自动从 registry 找当前激活的工作区路径。
    """
    token = None
    try:
        target = None
        if path:
            target = Path(path).resolve()
        else:
            target = _resolve_fallback_root()
        token = _ws_root.set(target)
        yield target
    finally:
        if token is not None:
            _ws_root.reset(token)


# read_file 工具允许访问的目录白名单（set_base_dir 会重建）
ALLOWED_READ_ROOTS = [
    BASE_DIR,
    APP_DIR,
    UPLOADS_DIR,
    KB_DIR,
    CHART_OUTPUT_DIR,
    TASKS_DIR,
]

# write_file / edit_file 工具允许写入的目录白名单（set_base_dir 会重建）
ALLOWED_WRITE_ROOTS = [
    BASE_DIR,
    UPLOADS_DIR,
    CHART_OUTPUT_DIR,
]

# /files/{name} 与 /images/{name} 允许对外提供的目录白名单。
# 【安全】绝不能包含 APP_DIR —— 应用根目录下有 .env（API 密钥），
# 一旦纳入，任何人请求 GET /files/.env 即可下载全部密钥。
SERVABLE_DIRS = [
    UPLOADS_DIR,
    CHART_OUTPUT_DIR,
]

# 即使位于上述目录内，也拒绝对外提供的文件名/后缀（双保险）
SERVABLE_DENY_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx", ".sqlite", ".db"}
SERVABLE_DENY_NAMES = {".env", ".env.local", ".env.production", "kb_store.json"}

# 本地文件路径正则（用于提取附件和路径替换）
FILE_PATH_PATTERN = (
    r'[A-Za-z]:[\w\\/.\-]+'
    r'\.(?:png|jpg|jpeg|gif|webp|svg|bmp|py|txt|csv|json|md|html|css|js|java|c|cpp'
    r'|go|rs|rb|php|ts|tsx|vue|xml|yaml|yml|sql|sh|bat|xlsx|pdf|docx)'
)

# 需要用 _extract_text 处理的文件类型
EXTRACT_TEXT_SUFFIXES = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf"}

# 文件上传大小限制
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10MB

# API 速率限制
DEFAULT_RATE_LIMIT_MAX = 20
DEFAULT_RATE_LIMIT_WINDOW = 60


def _is_under(p: Path, roots: list) -> bool:
    """
    判断已解析的绝对路径 p 是否位于 roots 中任一目录之内（含目录本身）。

    安全要点：必须用 Path.is_relative_to 做「路径组件级」比较，
    不能用 str.startswith —— 后者会把 'D:\\proj' 与 'D:\\proj_backup'
    误判为同一目录，导致白名单被绕过（已实测复现）。
    """
    for root in roots:
        try:
            if p == root.resolve() or p.is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def allowed_read_roots() -> list:
    """
    当前上下文允许读取的目录。

    每次调用都重新计算 —— 工作区根是按上下文隔离的 ContextVar，
    用模块级常量会拿到别的请求/别的时刻的值。
    """
    return [get_base_dir(), APP_DIR, UPLOADS_DIR, KB_DIR, CHART_OUTPUT_DIR, TASKS_DIR]


def allowed_write_roots() -> list:
    """当前上下文允许写入的目录（工作区 + 应用的上传/图表目录）。"""
    return [get_base_dir(), UPLOADS_DIR, CHART_OUTPUT_DIR]


def is_path_allowed(file_path: str) -> bool:
    """检查路径是否在允许读取的白名单目录内。"""
    try:
        return _is_under(Path(file_path).resolve(), allowed_read_roots())
    except Exception:
        return False


def is_path_writable(file_path: str) -> bool:
    """检查路径是否在允许写入的白名单目录内。"""
    try:
        return _is_under(Path(file_path).resolve(), allowed_write_roots())
    except Exception:
        return False


def resolve_safe_path(file_path: str) -> Path:
    """将路径安全解析为绝对路径，同时允许白名单目录内的写入。"""
    try:
        p = Path(file_path).resolve()
        if is_path_writable(file_path):
            return p
        raise ValueError(f"路径超出允许写入的目录范围: {file_path}")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"路径解析失败: {file_path} - {e}")


def extract_file_content(file_path: Path, max_len: int = 5000) -> str:
    """
    统一的文件内容提取（用于附件预览）。
    对 office/pdf 文件使用 extract_text()，其他文件直接读取。
    超长内容自动截断。
    """
    try:
        suffix = file_path.suffix.lower()
        if suffix in EXTRACT_TEXT_SUFFIXES:
            content = extract_text(file_path)
        else:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_len:
            content = content[:max_len] + "\n... (内容过长，已截断)"
        return content
    except Exception:
        return "(无法读取文件内容)"


def get_work_dir() -> Path:
    """返回工作目录。"""
    return BASE_DIR


# ── HuggingFace 本地缓存探测 ─────────────────────────────────────────────────────

def read_cached_model_file(repo_id: str, filename: str) -> Optional[Path]:
    """
    从本地 HF 缓存中直接读取模型仓库里的文件路径（**绝不发起网络请求**）。

    为什么不用 hf_hub_download：它会先向 huggingface.co 发 HEAD 请求校验版本，
    无外网时会阻塞到超时（实测 45 秒以上），把启动流程拖死。
    这里直接在 snapshots/<rev>/ 里找文件。
    """
    cache_root = os.getenv("HF_HUB_CACHE") or os.getenv("HUGGINGFACE_HUB_CACHE")
    if not cache_root:
        hf_home = os.getenv("HF_HOME")
        cache_root = str(Path(hf_home) / "hub") if hf_home else str(
            Path.home() / ".cache" / "huggingface" / "hub"
        )

    snapshots = Path(cache_root) / ("models--" + repo_id.replace("/", "--")) / "snapshots"
    if not snapshots.is_dir():
        return None
    try:
        for rev in sorted(snapshots.iterdir(), reverse=True):
            candidate = rev / filename
            if candidate.is_file():
                return candidate
    except OSError:
        pass
    return None


def hf_model_is_cached(repo_id: str) -> bool:
    """
    检查 HuggingFace 模型是否已存在于本地缓存（不发起任何网络请求）。

    用途：SentenceTransformer 在模型未缓存时会去 huggingface.co 下载，
    在无外网环境下会重试到超时（实测单次可阻塞 8 分钟以上）。
    因此调用前必须先判断缓存是否存在，避免把服务卡死。
    """
    cache_root = os.getenv("HF_HUB_CACHE") or os.getenv("HUGGINGFACE_HUB_CACHE")
    if not cache_root:
        hf_home = os.getenv("HF_HOME")
        if hf_home:
            cache_root = str(Path(hf_home) / "hub")
        else:
            cache_root = str(Path.home() / ".cache" / "huggingface" / "hub")

    folder = "models--" + repo_id.replace("/", "--")
    model_dir = Path(cache_root) / folder
    if not model_dir.is_dir():
        return False
    # 至少要有可用的快照目录（snapshots/<rev>/ 内有文件）
    snapshots = model_dir / "snapshots"
    if not snapshots.is_dir():
        return False
    try:
        return any(any(True for _ in rev.iterdir()) for rev in snapshots.iterdir() if rev.is_dir())
    except OSError:
        return False


# sentence_transformers 可用性探测结果（None = 尚未探测）
_st_importable: Optional[bool] = None
_ST_PROBE_CODE = (
    "import sentence_transformers, sys; "
    "sys.stdout.write('ST_OK')"
)


def sentence_transformers_importable(timeout: int = 30) -> bool:
    """
    在**子进程**中探测 `import sentence_transformers` 是否安全。

    为什么必须开子进程：本机环境下该 import 会触发原生层访问违例
    （进程以 0xC0000005 直接退出，Python 层面没有任何异常可捕获），
    在主进程里 try/except 拦不住，只会把整个服务带走。
    结果会缓存，探测只做一次。
    """
    global _st_importable
    if _st_importable is not None:
        return _st_importable

    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _ST_PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
        )
        _st_importable = proc.returncode == 0 and "ST_OK" in (proc.stdout or "")
        if not _st_importable:
            logger.warning(
                "sentence_transformers 不可用（returncode=%s），将使用 transformers 直连方案",
                proc.returncode,
            )
    except subprocess.TimeoutExpired:
        logger.warning("sentence_transformers 探测超时，视为不可用")
        _st_importable = False
    except Exception as e:
        logger.warning("sentence_transformers 探测失败: %s", e)
        _st_importable = False
    return _st_importable


# ── 文本提取（统一版） ────────────────────────────────────────────────────────────

def _extract_pdf(file_path: Path) -> str:
    """
    PDF 文本提取：多级回退策略，最大化文本提取质量。
    1. 优先使用 pymupdf(fitz) - 最佳文本提取质量
    2. 回退到 pdfplumber - 对表格友好
    3. 回退到 pypdf / PyPDF2 - 基础文本提取
    4. 最后回退到 pdfminer.six
    每页标注 [第N页] 便于溯源。
    """
    methods = [
        ("pymupdf", _extract_pdf_pymupdf),
        ("pdfplumber", _extract_pdf_pdfplumber),
        ("pypdf", _extract_pdf_pypdf),
        ("pypdf2", _extract_pdf_pypdf2),
        ("pdfminer", _extract_pdf_pdfminer),
    ]

    for name, extractor in methods:
        try:
            text = extractor(file_path)
            if text and len(text.strip()) > 100:
                logger.info("PDF 提取成功: %s (方法=%s, 长度=%d)", file_path.name, name, len(text))
                return text
        except Exception as e:
            logger.debug("PDF 提取方法 %s 失败: %s", name, e)
            continue

    logger.warning("PDF 解析失败: 所有方法均失败 - %s", file_path)
    return '[PDF 文件 - 无法提取文本内容，可能是扫描版或加密 PDF]'


def _extract_pdf_pymupdf(file_path: Path) -> str:
    """使用 PyMuPDF (fitz) 提取 - 最佳质量。"""
    import fitz
    doc = fitz.open(str(file_path))
    parts = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text") or ""
        if text.strip():
            parts.append(f"[第{page_num+1}页]\n{text}")
    doc.close()
    return '\n'.join(parts)


def _extract_pdf_pdfplumber(file_path: Path) -> str:
    """使用 pdfplumber 提取 - 对表格友好。"""
    import pdfplumber
    parts = []
    with pdfplumber.open(str(file_path)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            table_text = ""
            if tables:
                for t in tables:
                    for row in t:
                        cells = [str(c) if c else "" for c in row]
                        table_text += " | ".join(cells) + "\n"
            combined = text
            if table_text:
                combined = combined + "\n[表格数据]\n" + table_text
            if combined.strip():
                parts.append(f"[第{page_num+1}页]\n{combined}")
    return '\n'.join(parts)


def _extract_pdf_pypdf(file_path: Path) -> str:
    """使用 pypdf 提取。"""
    from pypdf import PdfReader
    reader = PdfReader(str(file_path))
    parts = []
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            parts.append(f"[第{page_num+1}页]\n{text}")
    return '\n'.join(parts)


def _extract_pdf_pypdf2(file_path: Path) -> str:
    """使用 PyPDF2 提取。"""
    from PyPDF2 import PdfReader
    reader = PdfReader(str(file_path))
    parts = []
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            parts.append(f"[第{page_num+1}页]\n{text}")
    return '\n'.join(parts)


def _extract_pdf_pdfminer(file_path: Path) -> str:
    """使用 pdfminer.six 提取 - 最后的回退。"""
    from pdfminer.high_level import extract_text as pdfminer_extract
    text = pdfminer_extract(str(file_path))
    return text if text else ""


def extract_text(file_path: Path) -> str:
    """
    从各种文件类型中提取纯文本内容。
    支持: .txt .md .csv .json .yaml .yml .xml .html .htm .pdf
          .docx .doc .xlsx .xls .pptx .ppt
    返回提取到的文本字符串；失败时返回以 [ 开头的错误标记。
    """
    ext = file_path.suffix.lower()
    try:
        if ext in ('.txt', '.md', '.csv', '.json', '.yaml', '.yml', '.xml',
                   '.html', '.htm', '.py', '.js', '.css', '.ts', '.tsx',
                   '.vue', '.java', '.c', '.cpp', '.go', '.rs', '.rb',
                   '.php', '.sql', '.sh', '.bat'):
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()

        elif ext == '.pdf':
            return _extract_pdf(file_path)

        elif ext in ('.docx', '.doc'):
            try:
                import docx
                doc = docx.Document(str(file_path))
                return '\n'.join(p.text for p in doc.paragraphs)
            except ImportError:
                logger.warning("Word 解析失败: 缺少 python-docx - %s", file_path)
                return '[Word 文件 - 请安装 python-docx 以读取内容]'

        elif ext in ('.xlsx', '.xls'):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(str(file_path), data_only=True)
                parts = []
                for sheet in wb.sheetnames:
                    ws = wb[sheet]
                    parts.append(f'[工作表: {sheet}]')
                    for row in ws.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c is not None]
                        if cells:
                            parts.append('\t'.join(cells))
                return '\n'.join(parts)
            except ImportError:
                logger.warning("Excel 解析失败: 缺少 openpyxl - %s", file_path)
                return '[Excel 文件 - 请安装 openpyxl 以读取内容]'

        elif ext in ('.pptx', '.ppt'):
            try:
                from pptx import Presentation
                prs = Presentation(str(file_path))
                parts = []
                for i, slide in enumerate(prs.slides):
                    parts.append(f'[幻灯片 {i+1}]')
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            parts.append(shape.text)
                return '\n'.join(parts)
            except ImportError:
                logger.warning("PPT 解析失败: 缺少 python-pptx - %s", file_path)
                return '[PPT 文件 - 请安装 python-pptx 以读取内容]'

        else:
            logger.info("不支持的文件类型: %s (%s)", ext, file_path)
            return f'[不支持的文件类型: {ext}]'

    except Exception as e:
        logger.error("文件解析失败: %s - %s", file_path, e)
        return f'[文件解析失败: {str(e)}]'


# ── 内容验证 ─────────────────────────────────────────────────────────────────────

INVALID_CONTENT_MARKERS = [
    "[Word 文件 - 请安装",
    "[PDF 文件 - 请安装",
    "[Excel 文件 - 请安装",
    "[PPT 文件 - 请安装",
    "[不支持的文件类型",
    "[文件解析失败",
]


def is_valid_content(content: str) -> bool:
    """检查提取的内容是否有效（非空、非错误标记）。"""
    if not content or not content.strip():
        return False
    for marker in INVALID_CONTENT_MARKERS:
        if marker in content:
            return False
    return True


# ── 工具结果上下文提取 ──────────────────────────────────────────────────────────

TOOL_CONTEXT_TEMPLATE = """
【历史工具执行记录】
以下是你在本次对话中已执行的工具操作及结果。当用户提到"上面的结果"、"之前的数据"、"刚才的计算"等指代时，请参考以下记录：

{tool_history}

请基于这些已有的执行结果来回答用户的问题，不要重新执行相同的操作。"""


def extract_tool_context(messages: list, max_result_len: int = 500) -> str:
    """
    从消息历史中提取所有工具调用及其结果，构建自然语言上下文。
    将 AIMessage(tool_calls) + ToolMessage 配对成可读的描述。

    返回形如:
      1. execute_code(code='1+1') → 结果: 2
      2. read_file(file_path='sales.csv') → 结果: 读取成功，共100行
    """
    from langchain_core.messages import AIMessage, ToolMessage, HumanMessage

    records = []
    i = 0
    while i < len(messages):
        msg = messages[i]

        # 查找 AIMessage 中的 tool_calls
        if isinstance(msg, AIMessage) and getattr(msg, 'tool_calls', None):
            for tc in msg.tool_calls:
                tool_name = tc.get("name", "unknown")
                tool_args = tc.get("args", {})
                call_id = tc.get("id", "")

                # 找到对应的 ToolMessage
                result_text = None
                for j in range(i + 1, len(messages)):
                    next_msg = messages[j]
                    if isinstance(next_msg, ToolMessage) and next_msg.tool_call_id == call_id:
                        result_text = str(next_msg.content)
                        break
                    elif isinstance(next_msg, AIMessage):
                        break

                # 格式化参数（简化显示）
                arg_strs = []
                for k, v in tool_args.items():
                    val_str = str(v)
                    if len(val_str) > 80:
                        val_str = val_str[:80] + "..."
                    arg_strs.append(f"{k}={repr(val_str)}")
                args_desc = ", ".join(arg_strs)

                # 格式化结果
                if result_text is not None:
                    if len(result_text) > max_result_len:
                        result_display = result_text[:max_result_len] + "..."
                    else:
                        result_display = result_text
                else:
                    result_display = "(未找到结果)"

                # 对常见工具做特殊处理
                if tool_name == "execute_code":
                    records.append(
                        f"  {len(records)+1}. 执行代码: ```{tool_args.get('code', '')[:100]}```\n"
                        f"     → 执行结果: {result_display}"
                    )
                elif tool_name == "read_file":
                    records.append(
                        f"  {len(records)+1}. 读取文件: {tool_args.get('file_path', '(未知)')}\n"
                        f"     → 文件内容: {result_display}"
                    )
                elif tool_name in ("write_file", "edit_file"):
                    records.append(
                        f"  {len(records)+1}. {tool_name}: {args_desc}\n"
                        f"     → 操作结果: {result_display}"
                    )
                else:
                    records.append(
                        f"  {len(records)+1}. {tool_name}({args_desc})\n"
                        f"     → 返回结果: {result_display}"
                    )

        i += 1

    if not records:
        return ""

    return TOOL_CONTEXT_TEMPLATE.format(tool_history="\n".join(records))


# ── 会话历史滑动窗口 ─────────────────────────────────────────────────────────────

MAX_SESSION_MESSAGES = 30
TOOL_RESULT_TRUNCATE_THRESHOLD = 2000

# 上下文摘要注入模板
CONTEXT_RECAP_TEMPLATE = """
【对话历史摘要】
以下是之前对话的摘要，请结合此摘要理解当前对话的上下文：
{recap}
"""


def compress_tool_result(content: str, max_len: int = TOOL_RESULT_TRUNCATE_THRESHOLD) -> str:
    """
    压缩工具返回结果：过长的内容截断并标记，避免撑爆上下文。
    """
    if not content or not isinstance(content, str):
        return content
    if len(content) <= max_len:
        return content
    half = max_len // 2
    # 保留头尾，中间截断
    return (
        content[:half]
        + f"\n\n... (内容过长，已截断 {len(content) - max_len} 字符) ...\n\n"
        + content[-half:]
    )


def build_conversation_memory(messages: list, keep_last_n: int = 6) -> tuple:
    """
    将历史对话压缩为摘要 + 保留最近 N 条消息。

    返回 (memory_summary, recent_messages) 元组：
    - memory_summary: 旧对话的文字摘要（注入系统提示词）
    - recent_messages: 最近 N 条完整对话消息
    """
    from langchain_core.messages import (
        SystemMessage, HumanMessage, AIMessage, ToolMessage
    )

    if len(messages) <= keep_last_n:
        return "", list(messages)

    # 分离系统消息和对话消息
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    conversation_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    if len(conversation_msgs) <= keep_last_n:
        return "", list(messages)

    # 旧消息 = 去掉最近 N 条的剩余部分
    split_idx = len(conversation_msgs) - keep_last_n
    split_idx = max(split_idx, 0)

    # ── 配对修复：硬切可能切断 AIMessage(tool_calls) 与 ToolMessage 的配对 ──
    # LLM API 要求 ToolMessage 必须紧跟带同一 tool_call_id 的 AIMessage(tool_calls)。
    # 如果切分点刚好落在 AIMessage(tool_calls) 和它的 ToolMessage 之间，
    # 或者切分点本身就落在某个 ToolMessage 上，都会产生"孤立 ToolMessage"。
    #
    # 修复：只要 split_idx 处（或之后）出现 ToolMessage 前面没有对应的
    # AIMessage(tool_calls)，就把 split_idx 向前推进，直到把整个配对拉进 recent。
    i = split_idx
    while i < len(conversation_msgs):
        cur = conversation_msgs[i]
        if isinstance(cur, ToolMessage):
            # cur 是 ToolMessage，回溯找它前面的 AIMessage(tool_calls)
            j = i - 1
            while j >= 0 and not isinstance(conversation_msgs[j], AIMessage):
                j -= 1
            if j >= 0 and getattr(conversation_msgs[j], "tool_calls", None):
                # 把 split_idx 推到这个 AIMessage 上
                split_idx = min(split_idx, j)
                i = j
            else:
                # 找不到配对 AIMessage，理论上不该发生，但为安全直接丢弃
                split_idx = i + 1
                i += 1
        else:
            i += 1

    old_msgs = conversation_msgs[:split_idx]
    recent_msgs = conversation_msgs[split_idx:]

    # 从旧消息构建摘要
    summary_parts = []
    current_speaker = None
    current_content = ""

    for msg in old_msgs:
        if isinstance(msg, HumanMessage):
            # 如果有之前的 AI 回复待处理，先保存
            if current_speaker == "ai" and current_content:
                summary_parts.append(f"  AI: {current_content[:200]}")
            current_speaker = "human"
            current_content = str(msg.content)[:300]
        elif isinstance(msg, AIMessage):
            if current_speaker == "human" and current_content:
                summary_parts.append(f"  用户: {current_content}")
            current_speaker = "ai"
            current_content = str(msg.content)[:300]
        elif isinstance(msg, ToolMessage):
            # 工具调用结果只保留关键信息
            tool_content = str(msg.content)
            if len(tool_content) > 100:
                tool_content = tool_content[:100] + "..."
            summary_parts.append(f"  [工具调用返回]: {tool_content}")

    # 保存最后一条
    if current_speaker and current_content:
        prefix = "用户" if current_speaker == "human" else "AI"
        summary_parts.append(f"  {prefix}: {current_content[:300]}")

    memory_summary = "\n".join(summary_parts) if summary_parts else ""

    return memory_summary, system_msgs + recent_msgs


def trim_session_history(messages: list, max_messages: int = MAX_SESSION_MESSAGES) -> list:
    """
    智能滑动窗口：
    1. 长工具结果截断（compress_tool_result）
    2. 旧消息压缩为摘要（build_conversation_memory）
    3. 只保留最近的消息作为完整上下文
    """
    from langchain_core.messages import (
        SystemMessage, HumanMessage, AIMessage, ToolMessage
    )

    if not messages:
        return messages

    # Step 1: 压缩过长的 ToolMessage 内容
    compressed = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
            truncated = compress_tool_result(msg.content)
            if truncated != msg.content:
                compressed.append(ToolMessage(
                    content=truncated,
                    tool_call_id=getattr(msg, 'tool_call_id', '')
                ))
            else:
                compressed.append(msg)
        else:
            compressed.append(msg)

    # Step 2: 分离系统消息和对话消息
    system_msgs = [m for m in compressed if isinstance(m, SystemMessage)]
    conversation_msgs = [m for m in compressed if not isinstance(m, SystemMessage)]

    if len(conversation_msgs) <= max_messages:
        return compressed

    # Step 3: 使用 build_conversation_memory 构建摘要 + 最近消息
    memory_summary, recent_without_system = build_conversation_memory(
        compressed, keep_last_n=min(10, max_messages - 2)
    )

    if memory_summary:
        # 将摘要注入到第一条系统提示词中
        recap_content = CONTEXT_RECAP_TEMPLATE.format(recap=memory_summary)
        existing_system = system_msgs[0].content if system_msgs else "你是一个智能AI助手。"
        new_system = SystemMessage(content=existing_system + "\n" + recap_content)
        # recent_without_system 包含 system_msgs + recent_msgs
        recent_conv = [m for m in recent_without_system if not isinstance(m, SystemMessage)]
        return [new_system] + recent_conv

    return compressed


# ── API 速率限制器 ──────────────────────────────────────────────────────────────

class RateLimiter:
    """
    基于 IP 的滑动窗口速率限制器。

    修复：早期版本只用 defaultdict 累积 IP，窗口过期后空 deque 永不回收，
    攻击者伪造大量源 IP 即可让 _requests 无限增长（内存耗尽）。
    现在每次 check 都会周期性清扫过期的空桶。
    """

    # 每处理这么多次请求做一次全量清扫
    SWEEP_INTERVAL = 500

    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque] = defaultdict(deque)
        self._ops_since_sweep = 0

    def _sweep(self, now: float):
        """移除窗口外已空的 IP 桶，控制内存占用。"""
        window_start = now - self.window_seconds
        stale = []
        for ip, dq in self._requests.items():
            while dq and dq[0] < window_start:
                dq.popleft()
            if not dq:
                stale.append(ip)
        for ip in stale:
            # 再次确认未被并发写入
            if not self._requests.get(ip):
                self._requests.pop(ip, None)

    def check(self, client_ip: str) -> bool:
        import time
        now = time.time()
        window_start = now - self.window_seconds
        dq = self._requests[client_ip]
        while dq and dq[0] < window_start:
            dq.popleft()
        if len(dq) >= self.max_requests:
            return False
        dq.append(now)

        self._ops_since_sweep += 1
        if self._ops_since_sweep >= self.SWEEP_INTERVAL:
            self._ops_since_sweep = 0
            self._sweep(now)
            if len(self._requests) > 20000:
                logger.warning("速率限制器跟踪的 IP 数异常偏高: %d", len(self._requests))
        return True

    def remaining(self, client_ip: str) -> int:
        import time
        now = time.time()
        window_start = now - self.window_seconds
        dq = self._requests[client_ip]
        while dq and dq[0] < window_start:
            dq.popleft()
        return max(0, self.max_requests - len(dq))


# ── Agent 上下文压缩（agent_loop 和 stream 共用） ─────────────────────────────────

AGENT_CONTEXT_MAX_CONV = 12
AGENT_CONTEXT_KEEP_LAST = 8


def maybe_compress_agent_context(messages: list, base_system: str) -> tuple:
    """
    当对话消息过多时，压缩旧对话为摘要。
    供 agent_loop (LCtext2.py) 和 stream event_generator (backend.py) 共用。

    返回 (compressed_messages, updated_system) 元组。
    如果未触发压缩，返回原始 messages 和 base_system。
    """
    from langchain_core.messages import SystemMessage

    conv_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(conv_msgs) <= AGENT_CONTEXT_MAX_CONV:
        return messages, base_system

    memory_summary, recent = build_conversation_memory(
        messages, keep_last_n=AGENT_CONTEXT_KEEP_LAST
    )
    if not memory_summary:
        return messages, base_system

    recap = f"\n\n【对话历史摘要】\n{memory_summary}"
    new_messages = [SystemMessage(content=base_system + recap)] + recent
    return new_messages, base_system + recap