# -*- coding: utf-8 -*-
"""
workspaces.py - 工作区管理

两种工作区类型：
  - folder（默认）: 用户选择一个文件夹，Agent 的文件读写 / 命令行都以此为根
  - files : 用户选择一个或多个文件，系统自动读取全部内容并注入提示词，
            Agent 根据这些内容回答或修改（类似 deepseek harness 的工作区）

存储位置：应用目录下的 `.workspaces/registry.json`。
刻意**不**放在工作区里 —— 否则用户可以直接改它来篡改配置。
"""

import os
import json
import time
import uuid
import shutil
import logging
from pathlib import Path
from typing import Optional

from utils import WORKSPACES_DIR, APP_DIR, set_base_dir, get_base_dir

logger = logging.getLogger("NewAgent.workspaces")

REGISTRY_PATH = WORKSPACES_DIR / "registry.json"

# 不允许作为工作区的目录（系统关键目录，选了也没意义且危险）
_FORBIDDEN_ROOTS = {
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "/System", "/usr", "/etc", "/bin", "/sbin",
}


def _load() -> dict:
    if not REGISTRY_PATH.exists():
        return {"workspaces": [], "active_id": None}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("registry 格式错误")
        data.setdefault("workspaces", [])
        data.setdefault("active_id", None)
        return data
    except Exception as e:
        logger.error("工作区注册表读取失败，将重建: %s", e)
        return {"workspaces": [], "active_id": None}


def _save(data: dict):
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(REGISTRY_PATH)
    except Exception as e:
        logger.error("工作区注册表保存失败: %s", e)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _entry_view(ws: dict) -> dict:
    """对外暴露的工作区视图（不含内部字段）。"""
    kind = ws.get("kind", "folder")
    if kind == "files":
        files_info = ws.get("files", [])
        return {
            "id": ws["id"],
            "name": ws["name"],
            "path": ws["path"],
            "kind": "files",
            "createdAt": ws.get("created_at", 0),
            "fileCount": len(files_info),
            "totalChars": sum(f.get("size", 0) for f in files_info),
            "exists": True,
        }
    return {
        "id": ws["id"],
        "name": ws["name"],
        "path": ws["path"],
        "kind": "folder",
        "createdAt": ws.get("created_at", 0),
        "exists": Path(ws["path"]).is_dir(),
    }


def list_workspaces() -> dict:
    data = _load()
    items = [_entry_view(w) for w in data["workspaces"]]
    active = data.get("active_id")

    need_fix = False
    if active:
        cur = next((w for w in data["workspaces"] if w["id"] == active), None)
        cur_view = next((i for i in items if i["id"] == active), None)
        if cur is None or cur_view is None:
            need_fix = True
        elif cur.get("kind") != "files" and not cur_view["exists"]:
            need_fix = True
    if need_fix:
        def _is_valid(w: dict) -> bool:
            if w.get("kind") == "files":
                return bool(w.get("files"))
            return Path(w["path"]).is_dir()

        nxt = next((w for w in data["workspaces"] if _is_valid(w)), None)
        new_active = nxt["id"] if nxt else None
        if new_active != active:
            logger.warning("激活的工作区已失效，自动切换到: %s",
                           nxt["name"] if nxt else "（无可用工作区）")
            data["active_id"] = new_active
            _save(data)
            active = new_active
            if nxt and nxt.get("kind") != "files":
                try:
                    set_base_dir(nxt["path"])
                except Exception as e:
                    logger.warning("切换工作区失败: %s", e)
                    set_base_dir(APP_DIR)
            else:
                set_base_dir(APP_DIR)

    return {"workspaces": items, "active_id": active}


def get_workspace(ws_id: str) -> Optional[dict]:
    for w in _load()["workspaces"]:
        if w["id"] == ws_id:
            return w
    return None


def _read_file_content(path: Path, max_chars: int = 500000) -> str:
    """读取文件内容，按扩展名选择解码方式；超限截断。"""
    ext = path.suffix.lower()
    text_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css", ".scss", ".less",
        ".json", ".md", ".txt", ".csv", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".swift", ".kt",
        ".sh", ".bat", ".ps1", ".sql", ".r", ".m", ".mm", ".scala", ".pl", ".lua",
        ".env", ".gitignore", ".editorconfig",
    }
    try:
        if ext in text_exts or ext == "":
            raw = path.read_bytes()
            for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                try:
                    return raw.decode(enc)[:max_chars]
                except UnicodeDecodeError:
                    continue
        if ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"):
            try:
                from utils import extract_text as _extract
                return _extract(path)[:max_chars]
            except Exception:
                pass
        try:
            raw = path.read_bytes()
            return raw.decode("utf-8", errors="replace")[:max_chars]
        except Exception as e:
            logger.warning("读取文件失败 %s: %s", path, e)
            return f"[无法读取: {e}]"
    except Exception as e:
        logger.warning("读取文件失败 %s: %s", path, e)
        return f"[无法读取: {e}]"


def add_workspace(path: str, name: str = "", kind: str = "folder",
                  files: Optional[list] = None) -> dict:
    """
    新增工作区。

    kind="folder"（默认）:
        path 必须是已存在的目录；重复路径直接返回已有项。

    kind="files":
        files 是文件路径列表（至少 1 个），系统会读取每个文件的内容
        并存入 registry，之后由后端注入系统提示词。
        path 可留空或传第一个文件所在目录。
    """
    if kind == "files":
        if not files:
            raise ValueError("files 模式需要至少一个文件路径")

        resolved_files = []
        for fp in files:
            p = Path(fp).expanduser()
            try:
                p = p.resolve()
            except OSError as e:
                raise ValueError(f"路径无法解析: {fp} ({e})")
            if not p.is_file():
                raise ValueError(f"不是有效文件: {p}")
            resolved_files.append(p)

        data = _load()
        for w in data["workspaces"]:
            if w.get("kind") == "files":
                existing = {str(Path(f["path"])).lower() for f in w.get("files", [])}
                new_set = {str(p).lower() for p in resolved_files}
                if existing == new_set:
                    return {"workspace": _entry_view(w), "created": False}

        files_info = []
        total_size = 0
        for p in resolved_files:
            content = _read_file_content(p)
            size = len(content)
            total_size += size
            files_info.append({
                "path": str(p),
                "name": p.name,
                "size": size,
                "content": content,
            })

        display_name = (name or "").strip() or (
            resolved_files[0].name if len(resolved_files) == 1
            else f"{resolved_files[0].name} 等 {len(resolved_files)} 个文件"
        )

        ws = {
            "id": "ws_" + uuid.uuid4().hex[:12],
            "name": display_name,
            "path": str(resolved_files[0].parent),
            "kind": "files",
            "created_at": int(time.time() * 1000),
            "files": files_info,
            "total_chars": total_size,
        }
        data["workspaces"].append(ws)
        data["active_id"] = ws["id"]
        _save(data)
        logger.info("新增文件工作区: %s (%d 个文件, %d 字符)",
                    ws["name"], len(files_info), total_size)
        return {"workspace": _entry_view(ws), "created": True}

    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"路径无法解析: {path} ({e})")
    if not p.is_dir():
        raise ValueError(f"目录不存在或不是文件夹: {p}")

    ps = str(p)
    for bad in _FORBIDDEN_ROOTS:
        bad_path = Path(bad)
        matched = False
        try:
            matched = (ps.lower() == bad.lower()
                       or p.is_relative_to(bad_path))
        except (ValueError, OSError):
            matched = False
        if matched:
            raise ValueError(f"不允许把系统目录作为工作区: {ps}")

    data = _load()
    for w in data["workspaces"]:
        if w.get("kind") == "folder" and Path(w["path"]) == p:
            return {"workspace": _entry_view(w), "created": False}

    ws = {
        "id": "ws_" + uuid.uuid4().hex[:12],
        "name": (name or "").strip() or p.name or str(p),
        "path": str(p),
        "kind": "folder",
        "created_at": int(time.time() * 1000),
    }
    data["workspaces"].append(ws)
    data["active_id"] = ws["id"]
    _save(data)
    logger.info("新增工作区: %s (%s)", ws["name"], ws["path"])
    return {"workspace": _entry_view(ws), "created": True}


def remove_workspace(ws_id: str, delete_files: bool = False) -> dict:
    """
    移除工作区登记。

    delete_files=True 时**只删除应用自己的知识库索引**，
    绝不删除用户磁盘上的原始文件夹 —— 那是用户的数据，误删不可逆。
    """
    data = _load()
    target = None
    for w in data["workspaces"]:
        if w["id"] == ws_id:
            target = w
            break
    if target is None:
        raise ValueError("工作区不存在")

    was_active = data.get("active_id") == ws_id
    data["workspaces"] = [w for w in data["workspaces"] if w["id"] != ws_id]
    if was_active:
        nxt = next((w for w in data["workspaces"] if Path(w["path"]).is_dir()), None)
        data["active_id"] = nxt["id"] if nxt else None
    _save(data)

    # 关键：移除的是当前工作区时，必须同步进程内的根目录。
    # 否则 utils.BASE_DIR 会停留在刚被移除（甚至已删除）的路径上，
    # 与注册表不一致 —— 会出现文件树读不出来但列表看着正常的情况。
    if was_active:
        try:
            set_base_dir(nxt["path"] if nxt else APP_DIR)
        except Exception as e:
            logger.warning("移除工作区后回退根目录失败: %s", e)
            set_base_dir(APP_DIR)

    removed_index = False
    if delete_files:
        idx_dir = WORKSPACES_DIR / "kb" / ws_id
        if idx_dir.exists():
            try:
                shutil.rmtree(idx_dir)
                removed_index = True
            except Exception as e:
                logger.warning("删除工作区索引失败: %s", e)

    logger.info("移除工作区: %s (删索引=%s)", target["name"], removed_index)
    return {"removed": _entry_view(target), "index_removed": removed_index,
            "active_id": data.get("active_id"),
            "base_dir": str(get_base_dir()),
            "note": "仅移除了登记与索引，磁盘上的文件夹未被删除"}


def rename_workspace(ws_id: str, name: str) -> dict:
    """重命名工作区（只改显示名，不动磁盘路径）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    if len(name) > 60:
        raise ValueError("名称过长（最多 60 字）")

    data = _load()
    for w in data["workspaces"]:
        if w["id"] == ws_id:
            old = w["name"]
            w["name"] = name
            _save(data)
            logger.info("工作区重命名: %s -> %s", old, name)
            return _entry_view(w)
    raise ValueError("工作区不存在")


def activate(ws_id: str) -> dict:
    """切换当前工作区。files 类型只标记 active_id，不切换目录根。"""
    if not ws_id:
        raise ValueError("缺少工作区 id")
    ws = get_workspace(ws_id)
    if ws is None:
        raise ValueError("工作区不存在")

    if ws.get("kind") == "files":
        data = _load()
        data["active_id"] = ws_id
        _save(data)
        return _entry_view(ws)

    p = Path(ws["path"])
    if not p.is_dir():
        raise ValueError(f"工作区目录已不存在: {p}")

    set_base_dir(p)

    data = _load()
    data["active_id"] = ws_id
    _save(data)
    return _entry_view(ws)


def restore_active_on_startup() -> Optional[dict]:
    """启动时恢复上次激活的工作区。失败只告警，不影响服务启动。"""
    info = list_workspaces()
    active_id = info.get("active_id")
    if not active_id:
        # 没有任何工作区：默认把应用目录当作工作区，保持旧行为
        set_base_dir(APP_DIR)
        return None
    try:
        return activate(active_id)
    except Exception as e:
        logger.warning("恢复上次工作区失败（%s），回退到应用目录", e)
        set_base_dir(APP_DIR)
        return None




# ── 目录浏览（供前端"选择本地文件夹"用）──────────────────────────────────────

# 浏览时跳过的目录名，避免列表噪声过大
_BROWSE_SKIP = {
    "$RECYCLE.BIN", "System Volume Information", "node_modules",
    "__pycache__", ".git", ".venv", "venv", "$WinREAgent",
}


def list_drives() -> list:
    """列出可用盘符（Windows）/ 根目录（类 Unix）。"""
    drives = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if Path(root).exists():
                drives.append({"name": root, "path": root, "type": "drive"})
    else:
        drives.append({"name": "/", "path": "/", "type": "drive"})
        home = Path.home()
        drives.append({"name": f"~ ({home.name})", "path": str(home), "type": "home"})
    return drives


def browse(path: str = "", show_hidden: bool = False,
           include_files: bool = False, file_select: bool = False) -> dict:
    """
    浏览本机目录。

    include_files=True 时同时返回文件条目（type="file"），用于"选择文件工作区"模式。
    file_select=True 时允许在文件上单击选择（前端用来判断要不要高亮）。
    """
    if not path:
        return {"path": "", "parent": None, "entries": list_drives(),
                "is_root": True, "roots": list_drives(),
                "include_files": include_files, "file_select": file_select}

    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"路径无法解析: {path} ({e})")
    if not p.is_dir():
        raise ValueError(f"不是有效目录: {p}")

    skip_file_exts = {".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg",
                      ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
                      ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4",
                      ".zip", ".tar", ".gz", ".7z", ".rar"}

    entries = []
    files = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                name = e.name
                if not show_hidden and (name.startswith(".") or name in _BROWSE_SKIP):
                    continue
                if is_dir:
                    entries.append({"name": name, "path": str(Path(p) / name), "type": "dir"})
                elif include_files:
                    ext = Path(name).suffix.lower()
                    if ext in skip_file_exts:
                        continue
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    files.append({
                        "name": name, "path": str(Path(p) / name),
                        "type": "file", "size": size, "ext": ext,
                    })
    except PermissionError:
        raise ValueError(f"没有权限读取该目录: {p}")

    entries.sort(key=lambda x: x["name"].lower())
    if include_files:
        files.sort(key=lambda x: x["name"].lower())
        entries = entries + files

    parent = p.parent
    parent_path = None if parent == p else str(parent)

    return {
        "path": str(p),
        "name": p.name or str(p),
        "parent": parent_path,
        "entries": entries[:500],
        "total": len(entries),
        "truncated": len(entries) > 500,
        "is_root": False,
        "roots": list_drives(),
        "include_files": include_files,
        "file_select": file_select,
    }


def create_folder(parent: str, name: str) -> dict:
    """在指定目录下新建文件夹（供"新建文件夹"按钮用）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("文件夹名称不能为空")
    if any(c in name for c in '\\/:*?"<>|'):
        raise ValueError("文件夹名称不能包含 \\ / : * ? \" < > | 这些字符")
    if name in (".", ".."):
        raise ValueError("文件夹名称不合法")

    base = Path(parent).expanduser()
    try:
        base = base.resolve()
    except OSError as e:
        raise ValueError(f"路径无法解析: {parent} ({e})")
    if not base.is_dir():
        raise ValueError(f"父目录不存在: {base}")

    target = base / name
    if target.exists():
        raise ValueError(f"已存在同名文件或文件夹: {target}")
    try:
        target.mkdir(parents=False)
    except OSError as e:
        raise ValueError(f"创建失败: {e}")
    logger.info("新建文件夹: %s", target)
    return {"path": str(target), "name": name}


def get_workspace_context(ws_id: str) -> Optional[dict]:
    """
    获取工作区的上下文信息。

    folder 类型: 返回 None（由后端的 _workspace_snapshot 自行处理）
    files 类型:  返回 {"kind": "files", "name": "...", "files": [{name, path, content}, ...]}
    """
    ws = get_workspace(ws_id)
    if ws is None:
        return None
    if ws.get("kind") != "files":
        return None
    return {
        "kind": "files",
        "name": ws["name"],
        "id": ws["id"],
        "total_chars": ws.get("total_chars", 0),
        "files": ws.get("files", []),
    }


def format_workspace_context(ws_id: str) -> str:
    """
    把文件工作区的内容格式化为可注入系统提示词的文本。
    folder 类型返回空串。
    """
    ctx = get_workspace_context(ws_id)
    if ctx is None:
        return ""

    parts = [
        f"\n\n【文件工作区】{ctx['name']}",
        f"以下 {len(ctx['files'])} 个文件的完整内容已自动加载，"
        f"你必须以这些内容为依据回答或修改：",
    ]
    for i, f in enumerate(ctx["files"], 1):
        content = f.get("content", "")
        if content:
            parts.append(
                f"\n--- 文件 {i}: {f['name']} ({f['path']}) ---\n{content}\n--- 结束 ---"
            )
        else:
            parts.append(f"\n--- 文件 {i}: {f['name']} --- (内容为空或无法读取)")

    parts.append(
        "\n【强制规则】"
        "\n1. 以上文件内容就是用户当前工作区的全部内容，回答相关问题时**必须基于这些内容**。"
        "\n2. 如果用户要求修改某个文件，直接在回复中输出完整修改后的内容，或用 write_file 工具写入。"
        "\n3. 不要使用 read_file 工具重复读取这些文件，内容已经在上面给出。"
        "\n4. 如果问题与这些文件无关，正常回答即可。"
    )
    return "\n".join(parts)


def refresh_file_workspace(ws_id: str) -> dict:
    """
    重新读取文件工作区的文件内容（磁盘上文件可能已被修改）。
    """
    ws = get_workspace(ws_id)
    if ws is None:
        raise ValueError("工作区不存在")
    if ws.get("kind") != "files":
        raise ValueError("只有 files 类型工作区支持刷新")

    files_info = []
    total_size = 0
    for f in ws.get("files", []):
        p = Path(f["path"])
        if p.is_file():
            content = _read_file_content(p)
            size = len(content)
        else:
            content = f.get("content", "")
            size = f.get("size", 0)
        total_size += size
        files_info.append({
            "path": str(p),
            "name": p.name,
            "size": size,
            "content": content,
        })

    data = _load()
    for w in data["workspaces"]:
        if w["id"] == ws_id:
            w["files"] = files_info
            w["total_chars"] = total_size
            break
    _save(data)
    logger.info("刷新文件工作区: %s (%d 个文件, %d 字符)",
                ws["name"], len(files_info), total_size)
    return {
        "id": ws_id,
        "fileCount": len(files_info),
        "totalChars": total_size,
    }