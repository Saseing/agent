from hello_agents import ReActAgent, HelloAgentsLLM, ToolRegistry
from hello_agents.tools import CalculatorTool, SearchTool
from dotenv import load_dotenv
import os

# 加载环境变量
load_dotenv()

def basic_chat_example():
    """基础对话示例"""
    print("=" * 50)
    print("基础对话示例")
    print("=" * 50)
    
    # 创建LLM实例
    llm = HelloAgentsLLM()
    
    # 创建工具注册表（可以为空）
    tool_registry = ToolRegistry()
    
    # 创建ReActAgent
    agent = ReActAgent(
        name="AI助手",
        llm=llm,
        tool_registry=tool_registry,
        system_prompt="你是一个有用的AI助手，能够回答各种问题。"
    )
    
    # 进行对话
    response = agent.run("你好！请介绍一下自己")
    print(f"AI回复: {response}\n")
    
    response = agent.run("你能帮我做什么？")
    print(f"AI回复: {response}\n")
    
    # 查看历史消息
    print(f"历史消息数: {len(agent.get_history())}")
    print()

def calculator_example():
    """计算器工具示例"""
    print("=" * 50)
    print("计算器工具示例")
    print("=" * 50)
    
    # 创建LLM实例
    llm = HelloAgentsLLM()
    
    # 创建工具注册表并添加计算器工具
    tool_registry = ToolRegistry()
    calculator = CalculatorTool()
    tool_registry.register_tool(calculator)
    
    # 创建ReActAgent
    agent = ReActAgent(
        name="数学助手",
        llm=llm,
        tool_registry=tool_registry,
        system_prompt="你是一个擅长数学计算的AI助手。"
    )
    
    # 进行数学计算
    questions = [
        "请帮我计算 2 + 3 * 4",
        "计算 (10 + 5) / 3",
        "2的10次方是多少？"
    ]
    
    for question in questions:
        print(f"问题: {question}")
        response = agent.run(question)
        print(f"答案: {response}\n")

def search_example():
    """搜索工具示例（如果可用）"""
    print("=" * 50)
    print("搜索工具示例")
    print("=" * 50)
    
    try:
        # 创建LLM实例
        llm = HelloAgentsLLM()
        
        # 创建工具注册表
        tool_registry = ToolRegistry()
        
        # 尝试添加搜索工具（需要配置Tavily API密钥）
        if os.getenv("TAVILY_API_KEY"):
            search_tool = SearchTool()
            tool_registry.register_tool(search_tool)
            
            # 创建ReActAgent
            agent = ReActAgent(
                name="搜索助手",
                llm=llm,
                tool_registry=tool_registry,
                system_prompt="你是一个能够提供最新信息的AI助手。"
            )
            
            # 进行搜索查询
            questions = [
                "今天北京的天气怎么样？",
                "最近有什么重要的科技新闻？"
            ]
            
            for question in questions:
                print(f"问题: {question}")
                response = agent.run(question)
                print(f"答案: {response}\n")
        else:
            print("未配置TAVILY_API_KEY，跳过搜索工具示例")
            print("如需使用搜索功能，请在.env文件中添加TAVILY_API_KEY")
    except Exception as e:
        print(f"搜索工具示例出错: {e}")
    print()

def multi_turn_conversation():
    """多轮对话示例"""
    print("=" * 50)
    print("多轮对话示例")
    print("=" * 50)
    
    # 创建LLM实例
    llm = HelloAgentsLLM()
    
    # 创建工具注册表（可以为空）
    tool_registry = ToolRegistry()
    
    # 创建ReActAgent
    agent = ReActAgent(
        name="聊天伙伴",
        llm=llm,
        tool_registry=tool_registry,
        system_prompt="你是一个友好的聊天伙伴，喜欢与用户进行有趣的对话。"
    )
    
    # 多轮对话
    conversation = [
        "你喜欢什么类型的音乐？",
        "能推荐几首好听的歌曲吗？",
        "这些歌曲在哪里可以听到？"
    ]
    
    for message in conversation:
        print(f"你: {message}")
        response = agent.run(message)
        print(f"AI: {response}\n")
    
    # 查看完整的历史记录
    print("完整历史记录:")
    history = agent.get_history()
    for i, msg in enumerate(history):
        print(f"{i+1}. {msg['role']}: {msg['content'][:50]}...")

if __name__ == "__main__":
    # 运行所有示例
    basic_chat_example()
    calculator_example()
    search_example()
    multi_turn_conversation()
    
    print("所有示例运行完成！")
