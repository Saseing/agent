from dotenv import load_dotenv
import os

# 加载环境变量
load_dotenv()

print("=" * 50)
print("环境变量检查:")
print("=" * 50)
print(f"OPENAI_API_KEY: {os.getenv('OPENAI_API_KEY')[:10]}..." if os.getenv('OPENAI_API_KEY') else "OPENAI_API_KEY: 未设置")
print(f"LLM_MODEL: {os.getenv('LLM_MODEL')}")
print(f"LLM_TEMPERATURE: {os.getenv('LLM_TEMPERATURE')}")
print(f"LLM_BASE_URL: {os.getenv('LLM_BASE_URL')}")
print(f"OPENAI_API_BASE: {os.getenv('OPENAI_API_BASE')}")
print()

# 尝试创建 LLM 实例
try:
    from hello_agents import HelloAgentsLLM
    llm = HelloAgentsLLM()
    print("=" * 50)
    print("LLM 实例创建成功!")
    print("=" * 50)
    print(f"Provider: {llm.provider}")
    print(f"Model: {llm.model}")
    print(f"Base URL: {llm.base_url}")
except Exception as e:
    print("=" * 50)
    print(f"LLM 实例创建失败: {e}")
    print("=" * 50)
