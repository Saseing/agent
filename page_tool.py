"""
page_tool.py - 前端页面制作工具

提供 create_page / edit_page / list_pages 三个 LangChain 工具，让 Agent
能按自然语言描述直接产出可在应用内实时预览的前端页面。

【产物位置】统一写进「当前工作区根」下的 pages/ 子目录。
工作区是用户自己选的本地文件夹，页面产物跟着工作区走 —— 切到另一个
工作区，看到的自然是那个工作区自己的页面集，互不串味。

【为什么不做 Node/Vite 构建】一个页面 = 单文件 HTML（CSS/JS 可内联，
也可用本工具写同目录的 .css/.js 再用相对路径引用）。好处是零构建、
零 Node 依赖、写完即可预览，与本应用「依赖全部本地化、内网/离线可用」
的既有取舍一致。真需要工程化构建（Vite + Vue/React）的项目，仍走
run_shell + npm 那条路。

【安全】所有写入路径都解析到 pages/ 之内，拒绝 `..`、绝对路径与盘符；
拒绝可执行/脚本类后缀（.py/.exe/.bat/.ps1/.sh/.dll 等）。
注意这是「减速带」而不是安全边界 —— 模型本来就能用 write_file 与
run_shell 做更多事，这里只是避免页面工具被顺手用来落一个可执行文件。
"""

import logging
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger("NewAgent.page_tool")

# 页面产物目录名（位于工作区根之下）
PAGES_DIR_NAME = "pages"

# 页面主文件后缀
PAGE_SUFFIXES = (".html", ".htm")

# 允许页面工具落盘的资源后缀（白名单：宁可少，也不要顺手写出奇怪的东西）
ALLOWED_PAGE_SUFFIXES = {
    ".html", ".htm",
    ".css", ".js", ".mjs", ".json", ".svg", ".md", ".txt", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".woff", ".woff2", ".ttf",
}

# 明确拒绝的可执行 / 脚本后缀（双保险，配合上面的白名单）
DENY_PAGE_SUFFIXES = {
    ".py", ".pyc", ".exe", ".dll", ".bat", ".cmd", ".com", ".msi",
    ".ps1", ".psm1", ".sh", ".bash", ".vbs", ".jar", ".scr", ".reg",
    ".env", ".key", ".pem", ".p12", ".pfx",
}

# 单文件大小上限（纯防呆：模型偶尔会把整段二进制/超长内容塞进来）
MAX_PAGE_BYTES = 2 * 1024 * 1024


def _workspace_root() -> Path:
    """
    当前工作区根目录。

    必须函数内导入：`from utils import get_base_dir` 拿到的是函数，
    而 `from utils import BASE_DIR` 会在导入时把值拷进本模块 globals，
    之后切换工作区就失效了（LCtext2.py 里有同样的注释）。
    """
    from utils import get_base_dir
    return get_base_dir()


def pages_dir(create: bool = False) -> Path:
    """当前工作区的 pages/ 目录；create=True 时顺带创建。"""
    d = _workspace_root() / PAGES_DIR_NAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_page_path(name: str, create_dir: bool = False) -> Path:
    """
    把模型给的页面名解析为 pages/ 内的安全绝对路径。

    接受的写法：`index.html`、`demo/index.html`、`style.css`（自动补 .html）。
    拒绝的写法：绝对路径、盘符、含 `..` 的路径、白名单外的后缀。

    【为什么用 resolve() 后再比对】单纯检查字符串里的 `..` 挡不住
    符号链接/junction 逃逸（Windows 上目录联接很常见）。落盘前把最终
    路径 resolve 出来，再用 is_relative_to 做路径组件级比较 —— 与
    utils._is_under 同一套思路（str.startswith 会把 pages_old 误判成 pages）。
    """
    raw = (name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("页面名不能为空")

    if raw.startswith("/") or ":" in raw:
        raise ValueError("只接受 pages/ 目录内的相对路径，不要写盘符或绝对路径")

    segments = [s for s in raw.split("/") if s not in ("", ".")]
    if not segments:
        raise ValueError("页面名不合法")

    for seg in segments:
        if seg == ".." or set(seg) == {"."}:
            raise ValueError("页面路径中不允许出现 ..")
        if seg.startswith("."):
            # 页面目录里不放隐藏文件：既避免 .env / .git 这类名字被顺手写出，
            # 也让「补后缀」逻辑不会把 .env 变成人畜无害的 .env.html 混进去。
            raise ValueError(f"页面名不能以 . 开头: {seg!r}")
        if any(ch in seg for ch in ('<', '>', '"', '|', '?', '*', '\x00')):
            raise ValueError(f"页面名含非法字符: {seg!r}")

    rel = Path(*segments)
    if not rel.suffix:
        # 不写后缀时默认按 HTML 页面处理（模型经常会省）
        rel = rel.with_suffix(".html")

    suffix = rel.suffix.lower()
    if suffix in DENY_PAGE_SUFFIXES:
        raise ValueError(f"页面工具不允许写入 {suffix} 文件（可执行/脚本类后缀）")
    if suffix not in ALLOWED_PAGE_SUFFIXES:
        raise ValueError(
            f"不支持的页面文件类型 {suffix}；"
            f"允许: {', '.join(sorted(ALLOWED_PAGE_SUFFIXES))}"
        )

    base = pages_dir(create=create_dir).resolve()
    try:
        target = (base / rel).resolve()
    except OSError as e:
        raise ValueError(f"路径无法解析: {name} ({e})")

    if target != base and not target.is_relative_to(base):
        raise ValueError("页面路径越界：只能写入当前工作区的 pages/ 目录")
    return target


def _preview_url(path: Path) -> str:
    """页面在应用里的预览地址（相对 API 根）。"""
    try:
        rel = path.relative_to(pages_dir().resolve()).as_posix()
    except ValueError:
        rel = path.name
    return f"/pages/file/{rel}"


def _join_names(names) -> str:
    """把模型可能传成的字符串/列表统一成 '、' 分隔的名字串。"""
    if isinstance(names, str):
        items = [names]
    else:
        items = list(names or [])
    return "、".join(str(x) for x in items if str(x).strip())


def _write_page_text(target: Path, content: str) -> int:
    """统一入口：编码、大小与内容校验后落盘。返回写入字节数。"""
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)
    if len(content.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ValueError(
            f"内容过大（>{MAX_PAGE_BYTES // 1024 // 1024}MB）。"
            "页面请拆分：主 HTML 只保留结构与样式，数据用单独文件或接口获取。"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return len(content.encode("utf-8"))


@tool
def create_page(name: str, content: str) -> str:
    """创建或覆盖一个前端页面文件，写入当前工作区的 pages/ 目录。

    参数：
    - name: 页面文件名。不写后缀时自动补 .html；
            也支持写页面配套资源，如 style.css / app.js / logo.svg；
            可用子目录，如 demo/index.html。
    - content: 文件完整内容。HTML 页面**必须**自带
            `<!DOCTYPE html>`、`<meta charset="utf-8">` 与完整标签结构，
            样式写在 <style> 里、脚本写在 <script> 里即可（单文件最省事）。

    要求：
    - 页面应是自包含的（CSS/JS 内联，或引用同目录的 .css/.js 文件）；
    - 不要引用 CDN 外链资源 —— 本应用面向离线/内网环境，
      外链在用户机器上很可能加载失败，样式会直接崩掉；
    - 需要交互图表时优先用原生 Canvas/SVG 或纯 CSS 实现。

    返回：写入结果与预览地址（用户在左侧「页面」面板可实时看到）。
    """
    try:
        target = resolve_page_path(name, create_dir=True)
        size = _write_page_text(target, content)
        rel = _preview_url(target)
        return (
            f"成功创建 {target.name}（{size} 字节）\n"
            f"完整路径: {target}\n"
            f"预览地址: {rel}\n"
            f"用户可在左侧边栏「页面」面板中实时预览。\n"
            f"（回复用户时请用上面的 /pages/file/ 地址，不要写成 /files/xxx.html，后者会 404）"
        )
    except ValueError as e:
        return f"创建页面失败：{e}"
    except Exception as e:
        logger.exception("create_page 失败")
        return f"创建页面出错：{e}"


@tool
def edit_page(name: str, old_text: str, new_text: str) -> str:
    """在已创建的页面文件中查找文本并替换（仅替换第一次出现的匹配）。

    修改页面时优先用本工具而不是重新 create_page 覆盖 —— 整页重写容易
    丢掉用户已经认可的部分，也更容易触发上下文长度限制。

    参数：
    - name: 页面文件名（同 create_page，不写后缀按 .html 处理）
    - old_text: 要被替换的原始文本（必须与文件中完全一致）
    - new_text: 替换后的新文本

    返回：编辑结果与预览地址。
    """
    try:
        target = resolve_page_path(name)
        if not target.is_file():
            return (f"错误：页面不存在 - {name}\n"
                    f"（当前页面目录: {pages_dir()}；可先用 list_pages 查看已有页面）")
        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return (f"错误：在 {target.name} 中未找到指定文本。\n"
                    "请先用 read_file 读取该文件确认原文，再传入完全一致的 old_text。")
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return (
            f"成功编辑 {target.name}\n"
            f"完整路径: {target}\n"
            f"预览地址: {_preview_url(target)}"
            "（回复用户时请用这个 /pages/file/ 地址，不要写成 /files/xxx.html，后者会 404）"
        )
    except ValueError as e:
        return f"编辑页面失败：{e}"
    except Exception as e:
        logger.exception("edit_page 失败")
        return f"编辑页面出错：{e}"


@tool
def list_pages() -> str:
    """列出当前工作区 pages/ 目录下已生成的页面与配套资源。

    返回每个文件的名字、大小、修改时间与预览地址。用户问「页面在哪」
    「做过哪些页面」时使用；返回的路径可直接交给 read_file 读取。
    """
    try:
        base = pages_dir()
        if not base.is_dir():
            return (f"当前工作区还没有页面目录：{base}\n"
                    "（用 create_page 创建页面时会自动生成）")

        rows = []
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(base).as_posix()
            if Path(rel).suffix.lower() not in ALLOWED_PAGE_SUFFIXES:
                continue
            try:
                st = f.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0
            rows.append((rel, size, mtime))

        if not rows:
            return (f"页面目录为空：{base}\n"
                    "（可用 create_page 创建第一个页面）")

        import datetime
        lines = [f"页面目录: {base}", f"共 {len(rows)} 个文件："]
        for rel, size, mtime in rows:
            ts = datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
            tag = "" if rel.lower().endswith(PAGE_SUFFIXES) else " [资源]"
            lines.append(f"- {rel}{tag} · {size} 字节 · {ts} · 预览 /pages/file/{rel}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("list_pages 失败")
        return f"列出页面出错：{e}"
