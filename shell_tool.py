# -*- coding: utf-8 -*-
"""
shell_tool.py - 通用命令行执行工具

为 Agent 提供执行 shell 命令的能力。设计要点：

1. **优先 bash，自动降级**
   按 bash → Git Bash → PowerShell 的顺序探测可用解释器。
   Linux/macOS 或装了 Git Bash 的 Windows 上就是真 bash；
   纯 Windows 环境降级到 PowerShell，工具不会因为缺 bash 而直接不可用。

2. **环境变量白名单**
   与代码执行器一致：子进程只继承运行必需的环境变量，
   OPENAI_API_KEY / TAVILY_API_KEY 等密钥不会进入子进程，
   因此命令里 `env` / `echo $OPENAI_API_KEY` 拿不到密钥。

3. **超时真正可终止**
   用 subprocess.run(timeout=...)，超时会 kill 子进程。

【安全边界，务必理解】
   本工具**绕过所有 AST 静态检查** —— 那套过滤器解析的是 Python AST，
   看不懂命令行字符串（`rm -rf`、`curl | sh` 都不在其视野内）。
   因此这里的边界只有三条：环境变量过滤、超时、输出截断。
   它不会阻止命令读写当前用户有权限访问的文件、发起网络请求或下载执行远程脚本。
   只应在可信环境使用；不要把它暴露给不受信任的用户。
"""

import os
import re
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from langchain_core.tools import tool

from utils import UPLOADS_DIR, CHART_OUTPUT_DIR, KB_DIR, TASKS_DIR, get_base_dir

logger = logging.getLogger("NewAgent.shell")

# 单条命令超时（秒）
SHELL_TIMEOUT = 60

# 返回给模型的输出上限，超出截断（头尾保留）
MAX_OUTPUT_CHARS = 8000

# 子进程环境变量白名单。密钥类变量（含 KEY/TOKEN/SECRET/PASSWORD 等）
# 一律不透传，避免命令通过 env/echo 读到凭据。
_ENV_PASSTHROUGH = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "USERNAME", "USER", "LOGNAME", "SHELL",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "LANG", "LC_ALL", "TERM", "TZ",
    "PYTHONIOENCODING", "PYTHONUTF8",
)
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API")

# 允许作为 cwd 的目录别名。注意 BASE_DIR 相关的项**在调用时**求值 ——
# 工作区是可切换的，导入时固定下来会失效。
def _dir_aliases() -> dict:
    base = get_base_dir()
    return {
        "": base,
        ".": base,
        "base": base,
        "root": base,
        "workspace": base,
        "uploads": UPLOADS_DIR,
        "pang": CHART_OUTPUT_DIR,
        "charts": CHART_OUTPUT_DIR,
        "knowledge_base": KB_DIR,
        "kb": KB_DIR,
        "tasks": TASKS_DIR,
    }


def _build_env() -> dict:
    """构造子进程环境：白名单透传 + 排除一切疑似密钥的变量。"""
    env = {}
    for name in _ENV_PASSTHROUGH:
        v = os.environ.get(name)
        if v is not None:
            env[name] = v

    hints = tuple(h.upper() for h in _SECRET_HINTS)
    for name, v in os.environ.items():
        if name in env:
            continue
        if any(h in name.upper() for h in hints):
            continue
        if name.startswith("NEWAGENT_"):
            env[name] = v

    # Git Bash 下禁用路径自动转换，否则 / 开头的参数会被改写成 Windows 路径
    env.setdefault("MSYS_NO_PATHCONV", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


_bash_path: Optional[str] = None
_bash_probed = False


def find_bash() -> Optional[str]:
    """
    查找可用的 bash（结果缓存）。

    顺序：PATH 上的 bash → 常见 Git Bash 安装位置 → WSL。
    """
    global _bash_path, _bash_probed
    if _bash_probed:
        return _bash_path
    _bash_probed = True

    p = shutil.which("bash")
    if p:
        _bash_path = p
        return _bash_path

    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe"),
        "/bin/bash",
        "/usr/bin/bash",
        "/usr/local/bin/bash",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            _bash_path = c
            return _bash_path

    # WSL 兜底：有发行版时 `wsl -e bash -c` 可用
    if os.name == "nt" and shutil.which("wsl"):
        try:
            r = subprocess.run(["wsl", "-e", "bash", "-c", "echo ok"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if r.returncode == 0 and b"ok" in (r.stdout or b""):
                _bash_path = "wsl:bash"
                return _bash_path
        except Exception:
            pass
    return None


def _resolve_shell(requested: str) -> Tuple[Optional[list], str, Optional[str], str]:
    """
    解析要使用的 shell。返回 (argv 前缀, 展示名, 错误信息, shell 族别)。
    族别：'bash' / 'powershell' / 'sh'，用于后续命令翻译。
    """
    requested = (requested or "auto").strip().lower()

    if requested in ("bash", "auto"):
        b = find_bash()
        if b == "wsl:bash":
            return ["wsl", "-e", "bash", "-c"], "bash (WSL)", None, "bash"
        if b:
            return [b, "-c"], "bash", None, "bash"
        if requested == "bash":
            return None, "", (
                "未找到 bash。请安装 Git for Windows（提供 bash.exe），"
                "或改用 shell='powershell'。"
            ), "bash"

    if os.name == "nt":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-Command"], "powershell", None, "powershell"
        return None, "", "未找到 powershell/pwsh。", "powershell"

    sh = shutil.which("sh")
    if sh:
        return [sh, "-c"], "sh", None, "sh"
    return None, "", "未找到可用的 shell。", "sh"


_PS_ALIAS_MAP = {
    "ls": "Get-ChildItem",
    "cat": "Get-Content",
    "rm": "Remove-Item",
    "cp": "Copy-Item",
    "mv": "Move-Item",
    "mkdir": "New-Item -ItemType Directory",
    "touch": "New-Item -ItemType File",
    "pwd": "Get-Location",
    "cd": "Set-Location",
    "grep": "Select-String",
    "find": "Get-ChildItem -Recurse",
    "which": "Get-Command",
    "ps": "Get-Process",
    "kill": "Stop-Process",
    "df": "Get-PSDrive",
    "du": "Get-ChildItem -Recurse | Measure-Object Length -Sum",
    "head": "Select-Object -First",
    "tail": "Select-Object -Last",
    "wc": "(Get-Content ...).Count",
    "sort": "Sort-Object",
    "uniq": "Select-Object -Unique",
    "awk": "<Use PowerShell property access>",
    "sed": "<Use PowerShell -replace operator>",
}


def _translate_bash_to_powershell(command: str) -> str:
    """
    把常见 bash 风格命令翻译成 PowerShell 语法。

    策略：覆盖最常被 AI 生成的 bash 命令（ls -la / cat / rm -rf / grep），
    不追求完整覆盖 —— 复杂 bash 命令本来就不该在 PowerShell 下跑。
    翻译失败的命令原样交给 PowerShell，由它自己报错。
    """
    stripped = command.strip()
    first_word = re.split(r"[|\s;]", stripped)[0]

    # ls → Get-ChildItem
    if first_word in ("ls", "/bin/ls"):
        rest = stripped[len(first_word):].strip()
        flag_tokens = [t for t in rest.split() if t.startswith("-")]
        flag_chars = "".join(flag_tokens).lower()
        # a = 显示隐藏文件, l = 长格式, h = 人类可读大小 → 都映射到 -Force
        has_force = any(c in flag_chars for c in ("a",)) or "la" in flag_chars or "al" in flag_chars
        has_recurse = "r" in flag_chars
        path_tokens = [t for t in rest.split() if not t.startswith("-")]
        path = " ".join(path_tokens)

        parts = ["Get-ChildItem"]
        if has_recurse:
            parts.append("-Recurse")
        if has_force:
            parts.append("-Force")
        if path:
            parts.append(path)
        return " ".join(parts)

    # cat → Get-Content
    if first_word == "cat":
        rest = stripped[3:].strip()
        return f"Get-Content {rest}" if rest else "Get-Content"

    # rm -rf / rm -r / rm -f
    if first_word == "rm":
        rest = stripped[2:].strip()
        flag_tokens = [t for t in rest.split() if t.startswith("-")]
        flag_chars = "".join(flag_tokens)
        recurse = "r" in flag_chars.lower()
        force = "f" in flag_chars.lower()
        target_tokens = [t for t in rest.split() if not t.startswith("-")]
        target = " ".join(target_tokens)
        parts = ["Remove-Item"]
        if recurse:
            parts.append("-Recurse")
        if force:
            parts.append("-Force")
        if target:
            parts.append(target)
        return " ".join(parts)

    # grep
    if first_word == "grep":
        rest = stripped[4:].strip()
        return f"Select-String {rest}" if rest else "Select-String"

    # cp
    if first_word == "cp":
        rest = stripped[2:].strip()
        flag_tokens = [t for t in rest.split() if t.startswith("-")]
        flag_chars = "".join(flag_tokens).lower()
        recurse = "r" in flag_chars
        target_tokens = [t for t in rest.split() if not t.startswith("-")]
        target = " ".join(target_tokens)
        parts = ["Copy-Item"]
        if recurse:
            parts.append("-Recurse")
        if target:
            parts.append(target)
        return " ".join(parts)

    # mv
    if first_word == "mv":
        rest = stripped[2:].strip()
        return f"Move-Item {rest}" if rest else "Move-Item"

    # mkdir -p
    if first_word == "mkdir":
        rest = stripped[6:].strip()
        flag_tokens = [t for t in rest.split() if t.startswith("-")]
        flag_chars = "".join(flag_tokens).lower()
        path_tokens = [t for t in rest.split() if not t.startswith("-")]
        path = " ".join(path_tokens)
        parts = ["New-Item", "-ItemType", "Directory"]
        if "p" in flag_chars:
            parts.append("-Force")
        if path:
            parts.append(path)
        return " ".join(parts)

    # pwd
    if first_word == "pwd":
        return "Get-Location"

    # which
    if first_word == "which":
        rest = stripped[6:].strip()
        return f"Get-Command {rest}" if rest else "Get-Command"

    return command


def _powershell_hint(command: str, stderr: str) -> str:
    """
    PowerShell 报错后，给 AI 生成针对性的替代命令建议。
    """
    stripped = command.strip()
    first_word = re.split(r"[|\s;]", stripped)[0]
    low = stderr.lower()

    tips = []

    # 找不到参数（典型：ls -la 里的 -la）
    if "找不到与参数名称" in stderr or "找不到接受实际参数" in stderr:
        if first_word in _PS_ALIAS_MAP:
            ps_cmd = _PS_ALIAS_MAP[first_word]
            tips.append(
                f"你在 PowerShell 下用了 bash 参数。请改用 PowerShell 语法："
                f"`{ps_cmd}`（完整对照表见工具文档）。"
            )

    # 命令不存在（可能是 bash 独有命令）
    if "not recognized" in low or "不是内部或外部命令" in low or "command not found" in low:
        if first_word in _PS_ALIAS_MAP:
            tips.append(
                f"`{first_word}` 是 bash 命令，PowerShell 下请用 `{_PS_ALIAS_MAP[first_word]}`。"
            )
        else:
            tips.append(
                f"命令 `{first_word}` 在此环境不可用。如果是 bash 命令请改用 PowerShell 等价写法。"
            )

    if tips:
        tips.append(
            "【PowerShell 常见对照表】\n"
            "  ls -la           → Get-ChildItem -Force\n"
            "  cat file         → Get-Content file\n"
            "  rm -rf dir       → Remove-Item -Recurse -Force dir\n"
            "  cp -r src dst    → Copy-Item -Recurse src dst\n"
            "  grep pat file    → Select-String pat file\n"
            "  find . -name 'x' → Get-ChildItem -Recurse -Filter 'x'\n"
            "  which cmd        → Get-Command cmd\n"
            "  pwd              → Get-Location\n"
        )

    return "\n\n" + "\n".join(tips) if tips else ""


def _decode(raw: bytes) -> str:
    """Windows 下命令输出可能是 GBK，逐级尝试解码。"""
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return (text[:half]
            + f"\n\n... (输出过长，已截断 {len(text) - limit} 字符) ...\n\n"
            + text[-half:])


def _resolve_cwd(cwd: str) -> Tuple[Optional[Path], Optional[str]]:
    """把 cwd 参数解析为真实目录（支持别名与工作区内的相对路径）。"""
    aliases = _dir_aliases()
    key = (cwd or "").strip().replace("\\", "/")
    if key in aliases:
        target = aliases[key]
    else:
        p = Path(key)
        target = p if p.is_absolute() else (get_base_dir() / p)
    try:
        target = target.resolve()
    except OSError:
        return None, f"无法解析工作目录: {cwd}"
    if not target.is_dir():
        return None, f"工作目录不存在: {target}"
    return target, None


def run_command(command: str, cwd: str = "", shell: str = "auto") -> str:
    """执行一条命令并返回格式化后的结果文本。"""
    if not command or not command.strip():
        return "错误：命令为空。"

    argv_prefix, shell_name, err, shell_family = _resolve_shell(shell)
    if argv_prefix is None:
        return f"错误：{err}"

    workdir, err = _resolve_cwd(cwd)
    if workdir is None:
        return f"错误：{err}"

    # PowerShell 环境下自动翻译 bash 风格命令（只翻译顶层命令行，不深入管道内部）
    display_command = command
    if shell_family == "powershell":
        translated = _translate_bash_to_powershell(command)
        if translated != command:
            display_command = translated
            logger.info("bash→powershell 翻译: %s → %s", command, translated)

    try:
        proc = subprocess.run(
            argv_prefix + [display_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workdir),
            env=_build_env(),
            timeout=SHELL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (f"命令超时（{SHELL_TIMEOUT} 秒），已强制终止。"
                f"\n请检查是否存在阻塞等待、死循环或需要交互输入的命令。")
    except FileNotFoundError as e:
        return f"错误：无法启动 shell - {e}"
    except Exception as e:
        logger.exception("shell 命令执行失败")
        return f"错误：执行失败 - {type(e).__name__}: {e}"

    stdout = _decode(proc.stdout or b"")
    stderr = _decode(proc.stderr or b"")

    parts = [f"[{shell_name}] 退出码: {proc.returncode}",
             f"工作目录: {workdir}"]

    if display_command != command:
        parts.append(f"（已自动翻译 bash 语法 → PowerShell）")

    if stdout.strip():
        parts.append("\n--- 标准输出 ---\n" + _truncate(stdout.rstrip()))
    if stderr.strip():
        parts.append("\n--- 标准错误 ---\n" + _truncate(stderr.rstrip()))
    if not stdout.strip() and not stderr.strip():
        parts.append("\n（无输出）")

    result = "\n".join(parts)

    # 错误提示：PowerShell 下给出具体语法建议
    if proc.returncode != 0:
        if shell_family == "powershell":
            result += _powershell_hint(command, stderr)
        else:
            low = (stdout + stderr).lower()
            if "not found" in low or "不是内部或外部命令" in low or "command not found" in low:
                result += "\n\n【提示】命令或程序不存在。可用 bash 的 `command -v <名字>` 或 PowerShell 的 `Get-Command <名字>` 确认是否安装。"
            elif "permission denied" in low or "拒绝访问" in low:
                result += "\n\n【提示】权限不足，可能是文件被占用或需要更高权限。"

    return result


@tool
def run_shell(command: str, cwd: str = "", shell: str = "auto") -> str:
    """执行命令行指令（bash / PowerShell），用于运行脚本、处理文件、调用 CLI 工具。

    适用场景：
    - 运行脚本、编译代码、执行 git / pip / npm 等命令行工具
    - 文件与目录操作（列目录、查找、统计行数、批量改名）
    - 系统信息查询（磁盘、进程、环境）

    参数：
    - command: 要执行的完整命令，例如 "ls -la uploads/" 或 "git status"
    - cwd: 工作目录，可省略。支持别名：uploads / pang / knowledge_base / tasks，
           也可以是项目内的相对路径
    - shell: "auto"（默认，优先 bash，无 bash 时降级 PowerShell）、
             "bash"（强制 bash）、"powershell"（强制 PowerShell）

    兼容性：
    - 在只有 PowerShell 的 Windows 环境下，常见 bash 命令会被自动翻译：
      ls -la → Get-ChildItem -Force | cat → Get-Content | rm -rf → Remove-Item -Recurse -Force
      grep → Select-String | pwd → Get-Location | mkdir -p → New-Item -ItemType Directory -Force
    - 复杂 bash 管道/高级特性不保证兼容，此时请直接写 PowerShell 语法

    注意：
    - 不要执行需要交互输入的命令（如不带参数的 vim、ssh、pip 的确认提示），
      会等不到输入而超时；必要时用 -y / --yes 之类的非交互参数
    - 单条命令超时 60 秒
    - 环境变量中的密钥不会传入子进程，读取密钥会失败
    - 命令执行结果包含退出码，非 0 表示失败，请据此判断并处理错误
    """
    return run_command(command, cwd=cwd, shell=shell)