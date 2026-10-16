from hello_agents import ReActAgent, HelloAgentsLLM, ToolRegistry
from hello_agents.tools import CalculatorTool
from dotenv import load_dotenv
# 加载环境变量
load_dotenv()

llm = HelloAgentsLLM()

# 创建工具注册表并添加计算器工具
tool_registry = ToolRegistry()
calculator = CalculatorTool()
tool_registry.register_tool(calculator)

# 使用ReActAgent支持工具调用
agent = ReActAgent(
    name="AI助手",
    llm=llm,
    tool_registry=tool_registry,
    system_prompt="你是一个有用的AI助手"
)

response = agent.run("你好！请介绍一下自己")
print(response)

response = agent.run("请帮我计算 2 + 3 * 4")
print(response)

print(f"历史消息数: {len(agent.get_history())}")