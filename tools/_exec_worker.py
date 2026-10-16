# -*- coding: utf-8 -*-
"""
tools/_exec_worker.py - 代码执行子进程

由 code_executor_tool.CodeExecutor 以子进程方式启动，通过 stdin/stdout
上的 JSON Lines 协议通信。每个请求一行 JSON，每个响应一行 JSON。

为什么独立成进程：
  1. 真正的超时终止 —— 父进程可以直接 kill 子进程；线程无法被强杀。
  2. 崩溃隔离 —— 段错误/OOM 只影响子进程，父进程自动重建。
  3. 环境隔离 —— 子进程环境变量被白名单过滤，模型代码读不到 API 密钥。

协议：
  请求  {"cmd": "execute", "code": "..."}  |  {"cmd": "reset"}  |  {"cmd": "ping"}
  响应  {"success": bool, "output": str, "error": str, "figures": [str]}
"""

import sys
import os
import json
import time
import uuid
import traceback


def _emit(payload: dict):
    """写一行 JSON 到 stdout 并立即刷新（协议帧）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str):
    """诊断信息走 stderr，不污染协议流。"""
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── 初始化执行环境 ────────────────────────────────────────────────────────────
_import_error = None
try:
    import matplotlib
    matplotlib.use("Agg")

    from IPython.core.interactiveshell import InteractiveShell
    from IPython.utils.capture import capture_output
except Exception as e:  # pragma: no cover
    _import_error = f"{type(e).__name__}: {e}"

CHART_OUTPUT_DIR = os.environ.get("NEWAGENT_CHART_DIR", ".")
os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)

if _import_error is None:
    shell = InteractiveShell.instance()
    shell.user_ns["chart_output_dir"] = CHART_OUTPUT_DIR
    shell.user_ns["BASE_DIR"] = os.environ.get("NEWAGENT_BASE_DIR", ".")
    shell.user_ns["UPLOADS_DIR"] = os.environ.get("NEWAGENT_UPLOADS_DIR", ".")

    _BOOTSTRAP = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
import pandas as pd
import numpy as np
import os
import json
from pathlib import Path
import time
"""
else:
    shell = None
    _BOOTSTRAP = ""


def _setup():
    if shell is None:
        return
    try:
        shell.run_cell(_BOOTSTRAP)
    except Exception as e:
        _log(f"bootstrap failed: {e}")


def _do_execute(code: str) -> dict:
    """在 IPython 命名空间中执行代码，捕获输出与新增图表。"""
    import matplotlib.pyplot as plt

    figures_before = set(plt.get_fignums())

    with capture_output() as captured:
        result = shell.run_cell(code)

    output = captured.stdout or ""

    if result.error_before_exec:
        return {"success": False, "output": output,
                "error": f"执行前错误: {result.error_before_exec}", "figures": []}
    if result.error_in_exec:
        return {"success": False, "output": output,
                "error": f"执行错误: {result.error_in_exec}", "figures": []}

    new_figs = set(plt.get_fignums()) - figures_before
    saved = []
    for fig_num in new_figs:
        fig = plt.figure(fig_num)
        if fig.get_axes():
            # 文件名必须唯一：早期版本用 chart_{秒级时间戳}_{fig_num}.png，
            # 同一秒内两次绘图会生成同名文件，后者覆盖前者；
            # 而 backend._extract_attachments 按文件名去重，
            # 结果就是"图表文件被覆盖 + 附件里只剩一张甚至为空"。
            filename = f"chart_{int(time.time() * 1000)}_{fig_num}_{uuid.uuid4().hex[:6]}.png"
            filepath = os.path.join(CHART_OUTPUT_DIR, filename)
            fig.savefig(filepath, dpi=150, bbox_inches="tight")
            saved.append(os.path.abspath(filepath))
        plt.close(fig_num)

    return {"success": True, "output": output, "error": "", "figures": saved}


def handle(req: dict) -> dict:
    cmd = req.get("cmd")

    if cmd == "ping":
        return {"success": True, "output": "pong", "error": "", "figures": []}

    if _import_error is not None:
        return {"success": False, "output": "",
                "error": f"执行环境初始化失败: {_import_error}", "figures": []}

    if cmd == "reset":
        try:
            import matplotlib.pyplot as plt
            shell.reset()
            _setup()
            plt.close("all")
            return {"success": True, "output": "环境已重置", "error": "", "figures": []}
        except Exception as e:
            return {"success": False, "output": "", "error": f"重置失败: {e}", "figures": []}

    if cmd == "execute":
        try:
            return _do_execute(req.get("code", ""))
        except Exception as e:
            return {"success": False, "output": "",
                    "error": f"执行异常: {e}\n{traceback.format_exc()}", "figures": []}

    return {"success": False, "output": "", "error": f"未知指令: {cmd}", "figures": []}


def main():
    _setup()

    # 就绪握手：父进程据此确认子进程可用
    _emit({"success": True, "output": "ready", "error": _import_error or "", "figures": []})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _emit({"success": False, "output": "",
                   "error": f"协议解析失败: {e}", "figures": []})
            continue

        resp = handle(req)
        _emit(resp)

        if req.get("cmd") == "shutdown":
            break


if __name__ == "__main__":
    main()
