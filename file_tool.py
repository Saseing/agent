"""
file_tool.py - 文件写入与编辑工具

提供 write_file 和 edit_file 两个 LangChain 工具，
供 Agent 创建新文件或精确编辑已有文件。
路径安全检查统一使用 utils 模块的白名单机制。
"""

from langchain_core.tools import tool

from utils import resolve_safe_path, is_path_writable


@tool
def write_file(file_path: str, content: str) -> str:
    """将内容写入指定文件。如果文件不存在则自动创建，已存在则覆盖。
    会自动创建不存在的父目录。

    参数说明：
    - file_path: 文件路径（可以是相对路径或绝对路径，必须在允许写入的目录内）
    - content: 要写入的文本内容

    返回：写入的字节数或错误信息。
    """
    try:
        fp = resolve_safe_path(file_path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"成功写入 {len(content)} 字节到 {fp}"
    except ValueError as e:
        return f"路径安全检查失败: {e}"
    except Exception as e:
        return f"写入文件时出错：{e}"


@tool
def edit_file(file_path: str, old_text: str, new_text: str) -> str:
    """在已有文件中查找指定文本并替换（仅替换第一次出现的匹配）。
    文件必须已存在，否则返回错误。

    参数说明：
    - file_path: 文件路径（可以是相对路径或绝对路径，必须在允许写入的目录内）
    - old_text: 要被替换的原始文本
    - new_text: 替换后的新文本

    返回：编辑结果信息或错误信息。
    """
    try:
        fp = resolve_safe_path(file_path)
        if not fp.exists():
            return f"错误：文件不存在 - {file_path}"
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"错误：在文件中未找到指定文本"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"成功编辑文件 {fp}"
    except ValueError as e:
        return f"路径安全检查失败: {e}"
    except Exception as e:
        return f"编辑文件时出错：{e}"