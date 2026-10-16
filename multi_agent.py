"""
Multi-Agent 闭环执行系统 - Planner / Judge 能力模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本模块提供 Planner Agent 和 Judge Agent 的运行能力。
完整的 Planner → Executor → Judge 闭环 orchestration 由 backend.py 的 event_generator 负责。

为什么这样设计：
  backend.py 才真正掌握 session messages、工具执行轨迹、最终回答等关键信息，
  只有它才能在 Judge prompt 里注入真实的执行结果。
  本模块只关心"Planner 怎么产出计划"和"Judge 怎么打分"，是纯能力提供者。
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import Optional, AsyncGenerator

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

logger = logging.getLogger("NewAgent.multi_agent")

MAX_JUDGE_RETRIES = int(os.getenv("MAX_JUDGE_RETRIES", "3"))
JUDGE_PASS_SCORE = int(os.getenv("JUDGE_PASS_SCORE", "60"))
ENABLE_MULTI_AGENT = os.getenv("ENABLE_MULTI_AGENT", "true").lower() in ("1", "true", "yes", "on")

PLANNER_SYSTEM = """你是一个专业的 AI 任务规划专家。
你的职责是分析用户的需求，输出结构化的执行计划。

【你的输出必须是合法 JSON，严格遵循以下 schema】：
```json
{{
  "goal": "一句话总结用户要达成的目标",
  "plan": ["步骤1", "步骤2", "步骤3"],
  "key_questions": ["需要澄清的问题，若无需澄清则留空数组"],
  "success_criteria": ["判断任务完成的标准1", "标准2"]
}}
```

【原则】
1. plan 要具体、可执行，每个步骤应对应 AI 可以直接做的事情（读文件、跑代码、搜索、写文件等）
2. 如果用户的需求可以一步完成，plan 就写一步，不要硬拆分
3. success_criteria 是 Judge Agent 评估 Executor 结果是否合格的依据，务必写清楚可验证的标准
4. 不要编造文件路径或数据来源
5. 直接输出 JSON，不要用 markdown 代码块包裹，不要加任何解释性文字
"""

PLANNER_SYSTEM_REPLAN = PLANNER_SYSTEM + """

【重规划指令】
上一轮执行没有通过 Judge 评审。请**重新制定或修订**执行计划，解决 Judge 指出的问题。
不要照搬上一轮的计划，要针对反馈做出调整。
"""

JUDGE_SYSTEM = """你是一个严苛、专业的 AI 结果评审专家。
你的任务是评估另一个 AI Agent 的执行结果是否满足用户的原始需求。

【你的输出必须是合法 JSON，严格遵循以下 schema】：
```json
{{
  "score": 0-100 的整数,
  "passed": true 或 false,
  "strengths": ["做得好的地方"],
  "issues": ["存在的问题或缺失"],
  "feedback_for_replan": "给 Planner 的反馈，解释哪里没做到位、下一步该怎么做。passed=true 时留空字符串。"
}}
```

【评分原则】
- 90-100：完美满足需求，答案清晰完整，有数据支撑或图表佐证
- 70-89：基本满足需求，有小瑕疵但不影响核心目标
- 60-69：部分满足需求，但有明显遗漏或深度不够
- 40-59：严重偏离或不完整，需要较大幅度重做
- 0-39：完全没解决问题或答非所问

【passed 判定】score >= {pass_score} 则 passed=true，否则 false。
直接输出 JSON，不要用 markdown 代码块包裹。"""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    json_str = text[start : end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            return json.loads(json_str.replace("'", '"'))
        except Exception:
            return None


def _build_llm(*, base_url, api_key, model, temperature):
    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
    )


class PlannerAgent:
    """只负责产出结构化执行计划的轻量 Agent。"""

    def __init__(self, llm_config: dict):
        self.llm = _build_llm(
            base_url=os.getenv("PLANNER_BASE_URL") or llm_config.get("base_url"),
            api_key=os.getenv("PLANNER_API_KEY") or llm_config.get("api_key"),
            model=os.getenv("PLANNER_MODEL") or llm_config.get("model"),
            temperature=0.3,
        )

    async def stream_plan(
        self,
        user_message: str,
        *,
        history: list = None,
        judge_feedback: str = None,
    ) -> AsyncGenerator[tuple[str, Optional[dict]], None]:
        """流式输出 Planner 思考文本 + 最终 JSON dict。

        yield: (text_chunk, None) during streaming
               (final_text, plan_dict) at the end
        """
        sys_prompt = PLANNER_SYSTEM_REPLAN if judge_feedback else PLANNER_SYSTEM
        if judge_feedback:
            sys_prompt += f"\n\n【上一轮 Judge 反馈】\n{judge_feedback}\n请根据以上反馈修订执行计划。"

        msgs = [SystemMessage(content=sys_prompt)]
        if history:
            msgs.extend(history[-4:])
        msgs.append(HumanMessage(content=user_message))

        full_text = ""
        try:
            async for chunk in self.llm.astream(msgs):
                text = getattr(chunk, "content", "") or ""
                if text:
                    full_text += text
                    yield (text, None)
        except Exception as e:
            logger.warning("Planner 流式失败，降级非流式: %s", e)
            try:
                resp = await self.llm.ainvoke(msgs)
                full_text = resp.content or ""
                yield (full_text, None)
            except Exception as e2:
                logger.error("Planner 彻底失败: %s", e2)
                yield (f"\n\n[Planner 失败：{e2}]", None)

        plan = _extract_json(full_text) or {
            "goal": user_message,
            "plan": ["直接执行用户需求"],
            "key_questions": [],
            "success_criteria": ["满足用户需求"],
            "_raw_fallback": True,
        }
        yield (full_text, plan)


class JudgeAgent:
    """强模型评审 Agent。输入执行轨迹 + 最终回答，输出打分 JSON。"""

    def __init__(self, llm_config: dict):
        self.llm = _build_llm(
            base_url=os.getenv("JUDGE_BASE_URL") or llm_config.get("base_url"),
            api_key=os.getenv("JUDGE_API_KEY") or llm_config.get("api_key"),
            model=os.getenv("JUDGE_MODEL") or llm_config.get("model"),
            temperature=0.1,
        )

    def build_judge_prompt(
        self,
        *,
        user_message: str,
        plan: dict,
        final_answer: str,
        tool_trace: str,
        attempt: int,
        judge_feedback: Optional[str],
    ) -> str:
        """拼接 Judge 要评估的完整上下文。"""
        parts = [
            f"## 用户原始问题\n{user_message}\n",
            f"## Planner 执行计划（第 {attempt} 轮）",
            f"**目标**: {plan.get('goal', '')}",
            f"**步骤**:",
        ]
        for i, step in enumerate(plan.get("plan", []), 1):
            parts.append(f"  {i}. {step}")
        if plan.get("success_criteria"):
            parts.append("**成功标准**:")
            for sc in plan.get("success_criteria"):
                parts.append(f"  - {sc}")
        if judge_feedback:
            parts.append(f"\n## 上一轮 Judge 反馈（第 {attempt - 1} 轮，需要解决的问题）\n{judge_feedback}")
        parts.append(f"\n## Executor 工具执行轨迹\n{tool_trace}")
        parts.append(f"\n## Executor 最终回答\n{final_answer}\n")
        return "\n".join(parts)

    async def stream_judge(
        self,
        prompt: str,
    ) -> AsyncGenerator[tuple[str, Optional[dict]], None]:
        """流式输出 Judge 思考文本 + 最终打分 JSON。"""
        msgs = [
            SystemMessage(content=JUDGE_SYSTEM.format(pass_score=JUDGE_PASS_SCORE)),
            HumanMessage(content=prompt),
        ]

        full_text = ""
        try:
            async for chunk in self.llm.astream(msgs):
                text = getattr(chunk, "content", "") or ""
                if text:
                    full_text += text
                    yield (text, None)
        except Exception as e:
            logger.warning("Judge 流式失败，降级非流式: %s", e)
            try:
                resp = await self.llm.ainvoke(msgs)
                full_text = resp.content or ""
                yield (full_text, None)
            except Exception as e2:
                logger.error("Judge 彻底失败: %s", e2)
                yield (f"\n\n[Judge 失败：{e2}]\n默认通过", None)

        result = _extract_json(full_text) or {
            "score": 100, "passed": True,
            "strengths": [], "issues": [],
            "feedback_for_replan": "",
            "fallback_pass": True,
        }
        # 确保 passed 和 score 一致
        score = int(result.get("score", 100))
        result["passed"] = bool(result.get("passed", score >= JUDGE_PASS_SCORE))
        result["score"] = score
        yield (full_text, result)


def build_llm_config_from_env() -> dict:
    return {
        "base_url": os.getenv("LLM_BASE_URL")
                    or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "api_key": os.getenv("OPENAI_API_KEY"),
        "model": os.getenv("LLM_MODEL", "qwen-turbo"),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.7")),
    }


def worth_multi_agent(user_message: str) -> bool:
    """快速判断是否值得跑多 agent 闭环。"""
    text = user_message.strip()
    if len(text) < 15:
        return False
    keywords = [
        "分析", "调研", "研究", "报告", "总结", "综合",
        "帮我", "请帮", "怎么做", "如何", "实现",
        "对比", "评估", "推荐", "方案", "步骤",
        "图表", "可视化", "画图", "绘制",
        "代码", "程序", "脚本", "函数",
        "数据", "统计", "计算", "建模",
        "文件", "整理", "批量", "处理",
        "项目", "开发", "构建", "设计",
    ]
    hit = sum(1 for k in keywords if k in text)
    return hit >= 1