# -*- coding: utf-8 -*-
"""
动态代码执行工具

LLM 生成的 Python 代码在**独立子进程**中通过 IPython 执行，支持 matplotlib 绘图。

安全模型（重要）：
  - 子进程隔离：崩溃/死循环只影响子进程，父进程可直接 kill，超时可真正生效。
  - 环境白名单：子进程只继承运行必需的环境变量，OPENAI_API_KEY /
    TAVILY_API_KEY 等密钥不会进入子进程，模型代码读 os.environ 拿不到密钥。
  - AST 静态过滤：`_check_code_safety` 作为「减速带」，拦截明显的危险调用。
    它**不是**安全边界（黑名单天然不完备），真正的边界是上面的进程隔离。
    此外子进程仍以当前用户身份运行，能读写该用户有权限的任意文件——
    如需更强隔离请将 _exec_worker.py 放入容器或低权限账户运行。
"""

import os
import sys
import ast
import re
import json
import time
import atexit
import queue
import logging
import threading
import subprocess
from typing import Dict, Any, Tuple
from pathlib import Path

from langchain_core.tools import tool

from utils import CHART_OUTPUT_DIR, UPLOADS_DIR, KB_DIR, TASKS_DIR, get_base_dir

logger = logging.getLogger("NewAgent.code_executor")

# ── 代码执行超时（秒） ───────────────────────────────────────────────────────────
EXECUTION_TIMEOUT = 30

# 向子进程透传的环境变量白名单。
# 【安全】模型生成的代码可以通过 os.environ 读取进程环境，因此绝不能把
# OPENAI_API_KEY / TAVILY_API_KEY 等密钥带进子进程。只透传运行必需的变量。
_ENV_PASSTHROUGH = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
)

# 显式拒绝的密钥特征（与白名单叠加，双保险）
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API")


def _build_worker_env() -> dict:
    """构造子进程环境：白名单透传 + 排除一切疑似密钥的变量。"""
    env = {}
    for name in _ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    upper_hints = tuple(h.upper() for h in _SECRET_HINTS)
    for name, value in os.environ.items():
        if name in env:
            continue
        if any(h in name.upper() for h in upper_hints):
            continue
        # 只放行非密钥类的自定义变量，避免误带敏感信息
        if name.startswith("NEWAGENT_"):
            env[name] = value

    env["NEWAGENT_CHART_DIR"] = str(CHART_OUTPUT_DIR)
    # 工作区根在**子进程启动时**取值，因此切换工作区后重启的 worker 会用新根
    env["NEWAGENT_BASE_DIR"] = str(get_base_dir())
    env["NEWAGENT_UPLOADS_DIR"] = str(UPLOADS_DIR)
    # 避免子进程把项目根目录加入 sys.path 后意外导入宿主模块
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class CodeExecutor:
    """
    安全的代码执行器，基于 IPython 提供 notebook 风格的代码执行
    - 变量在多次执行之间保持
    - 限制可导入的库
    - AST 级安全检查，阻断危险函数调用
    - 执行超时保护
    - 自动设置中文字体
    """
    
    ALLOWED_IMPORTS = {
        'pandas', 'pd', 'numpy', 'np', 'matplotlib', 'matplotlib.pyplot', 'plt',
        'scipy', 'sklearn', 'os', 'sys', 'json', 'csv', 'datetime', 'time',
        'math', 'statistics', 're', 'pathlib', 'io', 'collections', 'itertools',
        'functools', 'operator', 'warnings', 'typing', 'dataclasses',
        'decimal', 'fractions', 'copy', 'pprint', 'textwrap', 'string',
    }
    
    DANGEROUS_BUILTINS = {
        'exec', 'eval', 'compile', '__import__', 'open', 'input',
        'help', 'breakpoint', 'exit', 'quit',
        'getattr', 'setattr', 'delattr', 'hasattr',
        'globals', 'locals', 'vars', 'dir',
        'memoryview', '__build_class__', 'type', 'classmethod', 'staticmethod',
        'property', 'super', 'object', 'BaseException', 'Exception',
    }
    
    DANGEROUS_ATTRS = {
        '__import__', '__builtins__', '__subclasses__',
        '__class__', '__bases__', '__mro__', '__dict__',
        '__globals__', '__code__', '__func__',
        '__closure__', '__self__', '__objclass__',
        '__reduce__', '__reduce_ex__', '__getstate__', '__setstate__',
        '__getattr__', '__setattr__', '__delattr__',
        '__init__', '__new__', '__prepare__',
    }
    
    DANGEROUS_OS_CALLS = {
        'system', 'popen', 'execv', 'execve', 'execvp', 'execvpe',
        'spawnl', 'spawnle', 'spawnlp', 'spawnlpe',
        'spawnv', 'spawnve', 'spawnvp', 'spawnvpe',
        'getcwd', 'chdir', 'chmod', 'chown', 'link', 'symlink',
        'remove', 'unlink', 'rmdir', 'mkdir', 'makedirs',
        'rename', 'replace', 'copy', 'copy2', 'copytree',
    }
    
    DANGEROUS_PATH_CALLS = {
        'open', 'read_text', 'read_bytes', 'write_text', 'write_bytes',
        'unlink', 'rmdir', 'mkdir', 'rename', 'replace',
        'symlink_to', 'hardlink_to', 'touch', 'chmod', 'chown',
    }
    
    MAX_CODE_LENGTH = 50000
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or CHART_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 子进程状态
        self._proc = None
        self._reader = None
        self._queue = None
        self._lock = threading.Lock()
        self._generation = 0

        self._start_worker()

    # ── 子进程生命周期 ────────────────────────────────────────────────────────

    def _start_worker(self):
        """启动（或重启）代码执行子进程。"""
        import queue

        self._stop_worker()

        worker = Path(__file__).parent / "_exec_worker.py"
        if not worker.exists():
            raise RuntimeError(f"代码执行子进程脚本缺失: {worker}")

        creationflags = 0
        if os.name == "nt":
            # 不弹出控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._proc = subprocess.Popen(
            [sys.executable, "-u", str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(get_base_dir()),
            env=_build_worker_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._queue = queue.Queue()
        self._generation += 1
        generation = self._generation

        # 读取线程：把子进程 stdout 的每一行推入队列
        def _pump(proc=self._proc, q=self._queue, gen=generation):
            try:
                for line in proc.stdout:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # EOF 哨兵

        # stderr 单独消费，避免管道写满导致子进程阻塞
        def _drain(proc=self._proc):
            try:
                for line in proc.stderr:
                    text = line.rstrip()
                    if text:
                        logger.debug("[exec-worker] %s", text)
            except Exception:
                pass

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()
        threading.Thread(target=_drain, daemon=True).start()

        # 就绪握手
        ready = self._read_response(timeout=90)
        if not ready or ready.get("output") != "ready":
            self._stop_worker()
            raise RuntimeError("代码执行子进程启动失败（未收到就绪握手）")
        if ready.get("error"):
            raise RuntimeError(f"代码执行子进程初始化失败: {ready['error']}")

        logger.info("代码执行子进程已启动 (pid=%s, generation=%d)", self._proc.pid, generation)

    def _stop_worker(self):
        """终止子进程并释放资源。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                        proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream and not stream.closed:
                        stream.close()
                except Exception:
                    pass

    def _read_response(self, timeout: float):
        """从队列读取一条 JSON 响应，超时返回 None。"""
        import queue as _queue

        if self._queue is None:
            return None
        try:
            line = self._queue.get(timeout=timeout)
        except _queue.Empty:
            return None
        if line is None:  # EOF：子进程已退出
            return None
        line = line.strip()
        if not line:
            return self._read_response(timeout)
        try:
            return json.loads(line)
        except Exception as e:
            logger.warning("子进程返回非 JSON 数据: %r (%s)", line[:200], e)
            return None

    def _call_worker(self, payload: dict, timeout: float):
        """
        向子进程发送一条请求并等待响应。
        返回 (response, timed_out)。超时或子进程死亡时返回 (None, True)。
        """
        if self._proc is None or self._proc.poll() is not None:
            return None, True
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            logger.warning("写入子进程失败: %s", e)
            return None, True
        return self._read_response(timeout), False

    # ── 执行接口 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_paths_in_code(code: str) -> str:
        """
        扫描代码中的文件路径引用，将相对路径自动解析为绝对路径。
        处理 pd.read_csv(), pd.read_excel(), open(), Path() 等文件操作。
        优先匹配当前工作区内的文件，然后是 uploads/ 等应用资料目录。
        使用正斜杠或双反斜杠避免 Python 字符串转义问题。
        """
        base = get_base_dir()
        search_dirs = [base, UPLOADS_DIR, CHART_OUTPUT_DIR, KB_DIR, TASKS_DIR]
        search_dirs_exist = [d for d in search_dirs if d.exists()]
        
        if not search_dirs_exist:
            return code
        
        file_map = {}
        for search_dir in search_dirs_exist:
            try:
                for fpath in search_dir.iterdir():
                    if fpath.is_file():
                        fname = fpath.name
                        abs_path = str(fpath.resolve()).replace('\\', '/')
                        if fname not in file_map:
                            file_map[fname] = abs_path
            except PermissionError:
                continue
        
        if not file_map:
            return code
        
        for fname, abs_path in file_map.items():
            escaped_fname = re.escape(fname)
            pattern = r'''(['"])(\s*?)''' + escaped_fname + r'''(['"])'''
            
            def _replacer(m, _fname=fname, _abs=abs_path):
                has_prefix = m.group(2)
                quote = m.group(1)
                if has_prefix.strip():
                    return m.group(0)
                return quote + _abs + quote
            
            code = re.sub(pattern, _replacer, code)
        
        return code
    
    def _check_code_safety(self, code: str) -> Tuple[bool, str]:
        if len(code) > self.MAX_CODE_LENGTH:
            return False, f"代码过长（{len(code)} 字符），最大允许 {self.MAX_CODE_LENGTH} 字符"
        
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"语法错误: {e}"
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split('.')[0]
                    if root not in self.ALLOWED_IMPORTS:
                        return False, f"不允许的导入: {alias.name}"
            
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split('.')[0]
                    if root not in self.ALLOWED_IMPORTS:
                        return False, f"不允许的导入: {node.module}"
            
            elif isinstance(node, ast.Attribute):
                if node.attr in self.DANGEROUS_ATTRS:
                    return False, f"不允许的属性访问: {node.attr}"
            
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in self.DANGEROUS_BUILTINS:
                        return False, f"不允许的函数调用: {node.func.id}"
                
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in self.DANGEROUS_ATTRS:
                        return False, f"不允许的属性访问: {node.func.attr}"
                    
                    if isinstance(node.func.value, ast.Name):
                        obj_name = node.func.value.id
                        if obj_name == 'os' and node.func.attr in self.DANGEROUS_OS_CALLS:
                            return False, f"不允许的系统调用: os.{node.func.attr}()"
                        if obj_name == 'Path' and node.func.attr in self.DANGEROUS_PATH_CALLS:
                            return False, f"不允许的文件操作: Path.{node.func.attr}()"
                        if obj_name == 'sys':
                            return False, f"不允许访问 sys 模块"
                
                elif isinstance(node.func, ast.Subscript):
                    pass
        
        return True, ""
    
    def execute(self, code: str) -> Dict[str, Any]:
        is_safe, safety_error = self._check_code_safety(code)
        if not is_safe:
            return {
                'success': False, 'output': '',
                'error': f"代码安全检查失败: {safety_error}", 'figures': []
            }

        resolved_code = self._resolve_paths_in_code(code)

        # 串行化：子进程是单线程执行模型，并发调用必须排队
        with self._lock:
            # 子进程已死（崩溃/被 OOM kill）则重建，避免后续调用永久失败
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._start_worker()
                except Exception as e:
                    return {
                        'success': False, 'output': '',
                        'error': f"代码执行环境不可用: {e}", 'figures': []
                    }

            response, timed_out = self._call_worker(
                {"cmd": "execute", "code": resolved_code},
                timeout=EXECUTION_TIMEOUT,
            )

            if timed_out or response is None:
                # 真正的强杀：直接终止子进程（线程方案做不到这一点）
                self._stop_worker()
                logger.warning("代码执行超时或子进程异常，已终止并将在下次调用时重建")
                return {
                    'success': False, 'output': '',
                    'error': (
                        f'代码执行超时（{EXECUTION_TIMEOUT}秒）或执行进程异常，已强制终止。'
                        '请检查是否存在死循环、超大内存分配或过慢的计算。'
                        '注意：本次终止已清空执行环境中的全部变量。'
                    ),
                    'figures': []
                }

        result = {
            'success': bool(response.get('success')),
            'output': response.get('output', '') or '',
            'error': response.get('error', '') or '',
            'figures': response.get('figures', []) or [],
        }

        if not result['success'] and 'FileNotFoundError' in result.get('error', ''):
            result['error'] += (
                "\n\n【提示】文件未找到。请检查文件名是否正确，或使用绝对路径。"
                f"可用文件目录: {str(UPLOADS_DIR)}"
            )

        return result

    def reset(self):
        """
        重置执行环境（清空变量与图表）。

        这里**终止并重建**子进程，而不是只发 reset 指令：
        子进程的工作目录与 NEWAGENT_BASE_DIR 是启动时确定的，
        切换工作区后只有换一个进程才能让新根生效
        （实测：不重建时子进程 cwd 仍停留在旧工作区）。
        """
        with self._lock:
            self._stop_worker()
            try:
                self._start_worker()
            except Exception as e:
                logger.warning("重建代码执行子进程失败: %s", e)

    def restart_for_workspace(self):
        """
        工作区切换后调用：结束当前子进程，让下次执行在新工作区中重建。

        不立刻重建是为了省掉一次无谓的进程启动 —— 用户可能只是切了工作区
        而没有马上执行代码。
        """
        with self._lock:
            self._stop_worker()
        logger.info("代码执行环境已标记重建（工作区变更）")

    def shutdown(self):
        """进程退出时回收子进程。"""
        self._stop_worker()


# ── 全局执行器实例（子进程常驻，变量跨调用保持） ──────────────────────────────────
_executor: CodeExecutor = None


def get_executor() -> CodeExecutor:
    global _executor
    if _executor is None:
        _executor = CodeExecutor()
        atexit.register(_shutdown_executor)
    return _executor


def _shutdown_executor():
    """进程退出时回收代码执行子进程。"""
    global _executor
    if _executor is not None:
        try:
            _executor.shutdown()
        except Exception:
            pass
        _executor = None


# ── LangChain 工具定义 ────────────────────────────────────────────────────────────

@tool
def execute_code(code: str) -> str:
    """执行 Python 代码，支持数据分析、可视化等操作。
    
    使用场景：
    - 生成 matplotlib 图表（折线图、柱状图、饼图、散点图等任意类型）
    - 执行 pandas 数据分析
    - 运行 numpy 计算
    
    代码规范（重要）：
    - 图片会自动保存到 pang/ 目录，无需手动 savefig
    - 变量在多次调用之间保持（类似 Jupyter Notebook）
    - 已预导入: pandas, numpy, matplotlib.pyplot, os, json, pathlib, time
    - 中文字体已配置为 SimHei
    - 执行超时 30 秒
    - 代码必须自包含：每次调用都应包含完整的数据加载和变量定义，不要依赖之前的执行结果
    - 读取用户上传的CSV/Excel文件时，可以直接使用文件名（如 'data.csv'），系统会自动解析路径
    - 错误信息中若出现 FileNotFoundError，请确认文件名或使用绝对路径
    
    示例代码：
    ```python
    # 读取数据（使用文件名，系统自动解析）
    df = pd.read_csv('贵州茅台利润表.csv')
    
    # 数据预览
    print(df.head())
    print(df.columns.tolist())
    
    # 绘制折线图
    plt.figure(figsize=(10, 6))
    plt.plot(df['报告日'], df['营业总收入'], marker='o')
    plt.title('营业总收入趋势')
    plt.xlabel('日期')
    plt.ylabel('金额')
    plt.grid(True, linestyle='--', alpha=0.6)
    ```
    
    参数:
    - code: 要执行的 Python 代码
    """
    executor = get_executor()
    result = executor.execute(code)
    
    output_parts = []
    
    if result['output']:
        output_parts.append(f"执行输出:\n{result['output']}")
    
    if result['figures']:
        output_parts.append(f"\n生成了 {len(result['figures'])} 张图片:")
        for fig_path in result['figures']:
            output_parts.append(f"  - {fig_path}")
    
    if result['error']:
        output_parts.append(f"\n执行错误:\n{result['error']}")
    
    if not result['success'] and not result['error']:
        output_parts.append("\n代码执行完成，无输出。")
    
    return "\n".join(output_parts) if output_parts else "代码执行完成，无输出。"


@tool
def reset_code_environment() -> str:
    """重置代码执行环境，清除所有变量和图表。
    当需要重新开始分析或遇到变量冲突时使用。"""
    executor = get_executor()
    executor.reset()
    return "代码执行环境已重置，所有变量已清除。"