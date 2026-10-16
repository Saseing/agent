import dotenv
from langchain_openai import ChatOpenAI
import json
import os
import sys
import logging
from pathlib import Path
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

logger = logging.getLogger("NewAgent.LCtext2")

# 确保脚本所在目录在 Python 路径中（解决 runpy.run_path 等场景找不到模块的问题）
sys.path.insert(0, str(Path(__file__).parent))
from code_executor_tool import execute_code, reset_code_environment
from file_tool import write_file, edit_file
from page_tool import create_page, edit_page, list_pages
from shell_tool import run_shell
from utils import is_path_allowed, extract_text as _shared_extract_text, compress_tool_result, build_conversation_memory, extract_tool_context, TASKS_DIR, maybe_compress_agent_context

dotenv.load_dotenv()


def build_llm(*, api_key: str = None, base_url: str = None, model: str = None,
              temperature: float = None, timeout: float = None,
              max_retries: int = None) -> ChatOpenAI:
    """按当前环境变量（可被参数临时覆盖）构造一个 ChatOpenAI 客户端。

    【为什么单独抽成函数】模块级的 llm 是常量，进程起来之后就固定了。
    「运行时切换 API」需要按新的 key / base_url 再造成一个实例，所以构造逻辑
    必须可复用；顺带也方便做"先用给定密钥试一次连通性"这种临时探测。
    """
    extra = {}
    if timeout is not None:
        extra["timeout"] = timeout
    if max_retries is not None:
        extra["max_retries"] = max_retries
    return ChatOpenAI(
        base_url=base_url or os.getenv("LLM_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
        model=model or os.getenv("LLM_MODEL", "qwen-turbo"),
        temperature=float(temperature if temperature is not None else os.getenv("LLM_TEMPERATURE", "0.7")),
        **extra,
    )


llm = build_llm()


# -- TodoManager: 结构化任务状态管理 --
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("最多允许 20 个待办事项")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"项目 {item_id}: 需要 text 字段")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"项目 {item_id}: 无效状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("同时只能有一个任务处于 in_progress 状态")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "暂无待办事项。"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n（已完成 {done}/{len(self.items)}）")
        return "\n".join(lines)


TODO = TodoManager()


# -- TaskManager: 持久化任务管理，支持依赖图 --


class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(self, task_id: int, status: str = None,
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text(encoding="utf-8"))
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        tasks = []
        files = sorted(
            self.dir.glob("task_*.json"),
            key=lambda f: int(f.stem.split("_")[1])
        )
        for f in files:
            tasks.append(json.loads(f.read_text(encoding="utf-8")))
        if not tasks:
            return "暂无任务。"
        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        return "\n".join(lines)


TASKS = TaskManager(TASKS_DIR)


@tool
def todo(items: list) -> str:
    """更新任务列表，用于规划和跟踪多步骤任务的进度。
    每个任务需包含 id、text、status 三个字段。
    status 可选值：pending（待办）、in_progress（进行中）、completed（已完成）。
    示例: items=[{"id": "1", "text": "读取数据文件", "status": "in_progress"}]"""
    return TODO.update(items)


@tool
def task_create(subject: str, description: str = "") -> str:
    """创建一个新的持久化任务。任务保存在 .tasks/ 目录下，支持依赖关系。
    subject: 任务标题
    description: 任务详细描述（可选）"""
    return TASKS.create(subject, description)


@tool
def task_list() -> str:
    """列出所有任务及其状态摘要，包括阻塞依赖关系。"""
    return TASKS.list_all()


@tool
def task_get(task_id: int) -> str:
    """获取指定任务的完整详情信息。
    task_id: 任务 ID"""
    return TASKS.get(task_id)


@tool
def task_update(task_id: int, status: str = None,
                addBlockedBy: list = None, removeBlockedBy: list = None) -> str:
    """更新任务状态或依赖关系。
    task_id: 任务 ID
    status: 新状态（pending/in_progress/completed）
    addBlockedBy: 添加阻塞依赖的任务 ID 列表
    removeBlockedBy: 移除阻塞依赖的任务 ID 列表
    当任务标记为 completed 时，会自动解除其他任务对它的依赖。"""
    return TASKS.update(task_id, status, addBlockedBy, removeBlockedBy)


@tool
def read_file(file_path: str) -> str:
    """读取本地文件内容，支持 .txt/.md/.csv/.json/.yaml/.yml/.xml/.html/.pdf/.xlsx/.xls/.docx/.doc/.pptx/.ppt 等格式。
    参数 file_path 为文件的绝对路径或相对路径；**相对路径以当前工作区根目录为基准**。
    只能访问当前工作区以及应用的上传/知识库目录。"""
    # 注意：不能写成 `from utils import BASE_DIR` —— 那会把值缓存进本模块的
    # globals，工作区切换后拿到的是旧路径（且并发请求之间会互相污染）。
    from utils import get_base_dir
    base = get_base_dir()

    path = Path(file_path)
    # 相对路径优先按「当前工作区」解析 —— 用户说"读 README.md"时指的是
    # 他自己工作区里的那个，而不是应用自带的同名文件（实测会读错地方）。
    if not path.is_absolute():
        candidate = (base / file_path)
        if candidate.exists():
            path = candidate

    if not path.exists():
        return (f"错误：文件不存在 - {file_path}\n"
                f"（相对路径以当前工作区 `{base}` 为基准；"
                f"如需查看目录内容请用 run_shell 执行 ls / Get-ChildItem）")

    if not is_path_allowed(str(path)):
        return (f"错误：访问被拒绝 - 仅可访问当前工作区 `{base}` 及其子目录下的文件。\n"
                f"（如需读取其他位置，请先在左侧「工作区」里把那个文件夹加为工作区并切换过去）")

    result = _shared_extract_text(path)
    if result.startswith('['):
        return f"错误：{result}"
    return result


# 将工具绑定到模型
# 工具清单单独存成变量：运行时切换 API 后要用**同一份清单**重新 bind_tools，
# 否则新客户端身上没有工具 —— 模型只会纯文本回复，Agent 循环等于失效。
LC_TOOLS = [read_file, execute_code, reset_code_environment, write_file, edit_file,
            create_page, edit_page, list_pages, run_shell, todo,
            task_create, task_list, task_get, task_update]
llm_with_tools = llm.bind_tools(LC_TOOLS)

# 工具名称到函数的映射
TOOL_MAP = {
    "read_file": read_file, "execute_code": execute_code, "reset_code_environment": reset_code_environment,
    "write_file": write_file, "edit_file": edit_file,
    "create_page": create_page, "edit_page": edit_page, "list_pages": list_pages,
    "run_shell": run_shell,
    "todo": todo,
    "task_create": task_create, "task_list": task_list, "task_get": task_get, "task_update": task_update,
    "search_doc": None,
    "search_doc_fast": None,
    "search_doc_quality": None,
}


# ── 辅助函数：重建 Chain 时复用的工具绑定 ──────────────────────────────────────

def _extract_base_system(messages: list) -> str:
    """从消息列表中提取 SystemMessage 的内容作为基础系统提示词"""
    from langchain_core.messages import SystemMessage
    for m in messages:
        if isinstance(m, SystemMessage):
            return m.content
    return "你是一个智能AI助手。"


def _build_chain_with_system(system_content: str, llm_override=None):
    """
    构建 chain：把系统提示词作为**字面量 SystemMessage** 注入，而不是
    交给 ChatPromptTemplate 当模板渲染。

    【为什么必须这样】
    历史工具执行记录会被拼进系统提示词（见 utils.extract_tool_context），
    而工具返回值经常含花括号 —— JSON、Python dict、代码片段等。
    若用 ChatPromptTemplate 的 ("system", text) 形式，花括号会被当成模板变量：
      "{'query': 'x'}"  -> KeyError: missing variables
      "{query: 'x'"     -> ValueError: unmatched '{' in format spec
      "表格 { 数据"      -> ValueError: expected '}' before end of string
    结果就是「只要联网搜索/知识库检索返回过一次带花括号的内容，
    后续所有请求都会崩」，用户看到的正是那句 unmatched '{' in format spec。

    改用 SystemMessage 后，系统提示词不参与模板解析，任何内容都安全。
    """
    from langchain_core.messages import SystemMessage

    def prepend(payload, base_system: str):
        msgs = list(payload.get("messages", []))
        # 幂等：先剔除已有 SystemMessage，再前置唯一一条，避免重复注入
        msgs = [m for m in msgs if not isinstance(m, SystemMessage)]
        if base_system:
            msgs = [SystemMessage(content=base_system)] + msgs
        return msgs

    def _sync(payload):
        return prepend(payload, system_content)

    async def _async(payload):
        return prepend(payload, system_content)

    generator = RunnableLambda(_sync, afunc=_async)
    return generator | (llm_override if llm_override is not None else llm_with_tools)


def _rebuild_chain(system_content: str, llm_override=None):
    """根据系统提示词重建 chain。system_content 不会被当作模板渲染。"""
    return _build_chain_with_system(system_content, llm_override)


def agent_loop(chain, messages: list, response, max_rounds: int = 15, saved_system: str = None, llm_override=None):
    """核心 Agent 循环：不断执行工具调用，直到模型不再调用工具为止。
    包含 nag reminder 机制：若连续 3 轮未使用 todo 工具，自动注入提醒。
    当达到 max_rounds 轮后强制终止，防止无限循环。
    工具结果自动压缩，防止撑爆上下文窗口。
    每轮工具执行后重建 chain，注入最新工具上下文到系统提示词。
    saved_system: 保存的原始系统提示词（含对话历史），用于重建 chain 时保留上下文。
    llm_override: 调用方绑定过完整工具列表的 LLM。后端需要它来保证
                  tavily_search / search_doc* 在每一轮都可用；省略时退回模块级 LLM。"""
    rounds_since_todo = 0
    round_count = 0
    last_user_msg_idx = -1
    current_system = saved_system if saved_system else _extract_base_system(messages)

    while response.tool_calls:
        round_count += 1
        if round_count > max_rounds:
            messages.append(HumanMessage(
                content="<reminder>你已连续多轮调用工具，请直接给出最终答案总结，不要再调用新的工具。</reminder>"
            ))
            response = chain.invoke({"messages": messages})
            break

        messages.append(response)
        used_todo = False
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            logger.info("调用工具: %s(%s)", tool_name, tool_args)
            # 用 .get 而非下标：模型可能幻觉出不存在的工具名，
            # 下标访问会直接抛 KeyError 中断整个 agent 循环。
            tool_func = TOOL_MAP.get(tool_name)
            if tool_func is None:
                messages.append(ToolMessage(
                    content=f"错误：未知工具 '{tool_name}'。可用工具: {', '.join(sorted(k for k, v in TOOL_MAP.items() if v is not None))}",
                    tool_call_id=tool_id,
                ))
                continue
            try:
                tool_result = tool_func.invoke(tool_args)
            except Exception as e:
                logger.exception("工具 %s 执行失败", tool_name)
                messages.append(ToolMessage(
                    content=f"错误：工具 {tool_name} 执行失败 - {type(e).__name__}: {e}",
                    tool_call_id=tool_id,
                ))
                continue
            safe_result = compress_tool_result(str(tool_result))
            messages.append(ToolMessage(content=safe_result, tool_call_id=tool_id))
            if tool_name == "todo":
                used_todo = True
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            messages.append(HumanMessage(content="<reminder>请更新你的待办事项列表（使用 todo 工具）。</reminder>"))

        # 上下文窗口保护：使用共享压缩函数
        messages, current_system = maybe_compress_agent_context(messages, current_system)

        # 关键修复：使用保存的系统提示词重建 chain，保留完整对话历史上下文
        tool_context = extract_tool_context(messages)
        extra_system = current_system
        if tool_context:
            extra_system += tool_context
        current_system = extra_system
        new_chain = _rebuild_chain(extra_system, llm_override)
        response = new_chain.invoke({"messages": messages})
    return response


def get_answer():
    # 定义prompt模板
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一个智能AI助手。当用户提到阅读、分析文件或询问文件内容时，请使用 read_file 工具读取文件。"
                  "当用户要求制作图表、可视化数据、数据分析时，请使用 execute_code 工具执行 Python 代码。"
                  "你可以编写任意 matplotlib 代码来生成图表，例如："
                  "plt.figure(figsize=(10, 6)); plt.plot(x, y); plt.title('标题'); plt.grid(True)"
                  "图片会自动保存，无需手动调用 savefig。"
                  "已预导入的库: pandas, numpy, matplotlib.pyplot, os, json, pathlib, time"
                  "变量在多次调用之间保持，类似 Jupyter Notebook 环境。"
                  "代码执行有 30 秒超时限制，请勿编写死循环。"
                  "read_file 工具只能访问工作目录内的文件。"
                  "当用户要求创建新文件时，请使用 write_file 工具。"
                  "当用户要求修改已有文件中的特定内容时，请使用 edit_file 工具。"
                  "对于多步骤任务，请使用 todo 工具规划任务列表并跟踪进度。"
                  "开始执行前将任务标为 in_progress，完成后标为 completed。"
                  "对于需要跨会话持久化的任务，请使用 task_create/task_list/task_get/task_update 工具。"
                  "任务支持依赖关系（blockedBy），完成前置任务后会自动解除后续任务的阻塞。"
                  "【重要】请始终关注对话历史中的上下文，用户的追问通常与之前的对话相关"),
        MessagesPlaceholder(variable_name="messages"),
    ])

    # 创建处理链
    chain = prompt | llm_with_tools

    # 存储所有消息
    messages = []

    while True:
        user_input = input("请输入你的问题（输入'q'结束）：")
        if user_input == "q":
            break

        messages.append(HumanMessage(content=user_input))
        response = chain.invoke({"messages": messages})
        response = agent_loop(chain, messages, response)

        logger.info("AI回答: %s", response.content)
        messages.append(AIMessage(content=response.content))

    # 打印完整的对话历史
    logger.info("完整对话历史:")
    for msg in messages:
        logger.info("  %s: %s", type(msg).__name__, msg.content)

# get_answer()  # 交互模式入口，后端模式下由 backend.py 调用 agent 逻辑