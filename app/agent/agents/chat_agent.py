"""
闲聊/域外问答子 Agent

封装廉价 chat_llm（可绑定 MCP 外部工具）的通用问答能力，复用 answer_general
节点里的工具调用循环与提示词，返回纯文本。orchestrator 在纯闲聊计划下调用它。
"""

from langchain_core.messages import HumanMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.agents.base import AgentResult
from app.agent.context import DataAgentContext
from app.agent.llm import chat_llm
from app.agent.tool_loop import run_tool_loop
from app.clients.mcp_client_manager import mcp_client_manager
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


class ChatAgent:
    """闲聊子 Agent：用廉价 chat_llm（含 MCP 工具）生成纯文本回答"""

    name = "chat"

    # 生成闲聊/域外回答，返回 AgentResult(text)；参数 question=用户问题，runtime=运行上下文(stream_writer 用于流式 delta)，history=会话历史文本
    async def run(
        self, question: str, runtime: Runtime[DataAgentContext], history: str = ""
    ) -> AgentResult:
        # 延迟导入，避免 answer_general 与 agents 包在模块加载期形成耦合
        from app.agent.nodes.answer_general import FALLBACK_MESSAGE

        writer = runtime.stream_writer
        try:
            prompt = PromptTemplate(
                template=load_prompt("answer_general"),
                input_variables=["query", "history"],
            )
            # chat 子 Agent 专属工具（servers.*.agents 含 chat）
            tools = await mcp_client_manager.get_tools("chat")
            if tools:
                text = await run_tool_loop(
                    chat_llm,
                    [
                        HumanMessage(
                            content=prompt.format(query=question, history=history)
                        )
                    ],
                    tools,
                    writer,
                    max_rounds=app_config.mcp.max_tool_rounds,
                    stream=True,
                )
                text = text.strip() or FALLBACK_MESSAGE
            else:
                chain = prompt | chat_llm
                pieces: list[str] = []
                reset_sent = False
                async for chunk in chain.astream(
                    {"query": question, "history": history}
                ):
                    piece = chunk.content
                    if isinstance(piece, str) and piece:
                        pieces.append(piece)
                        writer(
                            {"type": "delta", "content": piece, "reset": not reset_sent}
                        )
                        reset_sent = True
                text = "".join(pieces).strip() or FALLBACK_MESSAGE
            return AgentResult(agent=self.name, ok=bool(text), payload={"text": text})
        except Exception as e:
            logger.error(f"闲聊子 Agent 执行失败：{e}")
            return AgentResult(
                agent=self.name, ok=False, payload={"text": ""}, error=str(e)[:200]
            )


# 模块级单例，供注册表复用
chat_agent = ChatAgent()
