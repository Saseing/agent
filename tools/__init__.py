"""
Agent 工具集 —— 模型可调用的能力都放在这里。

| 模块 | 工具 | 说明 |
|---|---|---|
| `page_tool.py` | `create_page` / `edit_page` / `list_pages` | 前端页面制作，产物落在「工作区/pages/」 |
| `file_tool.py` | `write_file` / `edit_file` | 文件写入与最小替换编辑 |
| `shell_tool.py` | `run_shell` | 命令行执行（bash；Windows 上降级为 PowerShell） |
| `code_executor_tool.py` | `execute_code` / `reset_code_environment` | Python 代码执行（子进程隔离） |
| `_exec_worker.py` | —— | `code_executor_tool` 拉起的**子进程脚本**，不是工具本身 |

【注册点】加了新工具要**同时**改两处，否则会出现"模型能调用、后端找不到工具"：
  1. `LCtext2.py` 的 `LC_TOOLS` 与 `TOOL_MAP`（Agent 循环执行工具时用）
  2. `backend.py` 的 `all_tools` 与 `TOOL_MAP`（绑定给主对话模型时用）

【关于下面的 sys.path 兜底】这些工具模块用 `from utils import ...` 引用项目根的公共模块；
正常入口（`backend.py`、`LCtext2.py`）已经插入了项目根路径，这里再兜一层，
使得单独 `import tools.xxx`（例如写测试、跑脚本）也能正常工作。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

__all__ = ["page_tool", "file_tool", "shell_tool", "code_executor_tool"]
