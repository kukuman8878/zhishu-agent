"""
域外问题通用问答节点

用途：当入口意图路由（classify_route）判定用户问题不属于业务链路（sql/doc/hybrid）范畴时，
由本节点调用配置中的轻量 chat_llm 直接给出自然语言回答，而不是生硬地拒绝。

流量分工：
  - 数据分析问题 → 主链路（llm，展示 LangGraph 流程图 + 结果表）
  - 闲聊/其他问题 → 本节点（chat_llm，只返回一条干净的 message 文本）

MCP 外部工具：当 conf/app_config.yaml 为 chat 子 Agent 配置了 MCP 工具（servers.*.agents
含 chat）时，本节点会把工具绑定给 chat_llm，使其能回答天气、联网搜索等简单外部问题；
工具按需调用并有轮数上限，未配置（或连接失败）时行为与旧版完全一致（fail-open）。
工具调用循环复用 app/agent/tool_loop.py。

成本考虑：闲聊这类低价值请求不进入召回与 SQL 生成链路，也不占用主模型额度，
只消耗便宜的 chat_llm，从而同时节省延迟与成本。
"""

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.history import render_history
from app.agent.llm import chat_llm
from app.agent.state import DataAgentState
from app.agent.tool_loop import run_tool_loop
from app.clients.mcp_client_manager import mcp_client_manager
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 廉价模型异常时的兜底文案：避免把内部错误直接抛给用户，同时引导回数据分析话题
FALLBACK_MESSAGE = (
    "抱歉，我暂时没能正常回答这个问题。我只能可靠地处理电商数据查询与分析，"
    "例如按地区、客户、商品、时间维度统计销量、销售额（GMV）等，"
    "欢迎换个问法试试：统计华北地区的销售总额。"
)


# 闲聊/域外问答节点：用轻量 chat_llm 流式生成回复并以 message 事件返回，失败时降级为兜底文案；
# 若配置了 chat 专属 MCP 工具则先走有界工具调用循环再流式产出答案；参数 state=含用户问题，runtime=运行时(stream_writer 发 delta/message 事件)
async def answer_general(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """回答域外问题：优先用绑定 MCP 工具的 chat_llm，无工具时退化为原始纯文本问答

    支持流式：逐 token 发 delta 事件（打字机效果），结束再发一条完整 message 终态。
    注意：不向前端输出 progress 事件，闲聊路径只返回文本，避免展示数据分析流程图。
    """

    query = state["query"]
    writer = runtime.stream_writer
    # 注入会话历史，闲聊也保持上下文连贯（如用户先问"你是谁"再问"你会什么"）
    history = render_history(
        state.get("messages", []), memory=state.get("memory_text", "")
    )

    try:
        # 通用问答提示词：无工具时直接流式生成；有 chat 专属工具时先跑有界工具调用循环
        prompt = PromptTemplate(
            template=load_prompt("answer_general"),
            input_variables=["query", "history"],
        )
        tools = await mcp_client_manager.get_tools("chat")

        if tools:
            content = await run_tool_loop(
                chat_llm,
                [HumanMessage(content=prompt.format(query=query, history=history))],
                tools,
                writer,
                max_rounds=app_config.mcp.max_tool_rounds,
                stream=True,
            )
            content = content.strip() or FALLBACK_MESSAGE
        else:
            # 未配置 MCP 工具：保持旧版行为（PromptTemplate | chat_llm 流式）
            chain = prompt | chat_llm
            pieces: list[str] = []
            reset_sent = False
            async for chunk in chain.astream({"query": query, "history": history}):
                piece = chunk.content
                if isinstance(piece, str) and piece:
                    pieces.append(piece)
                    writer({"type": "delta", "content": piece, "reset": not reset_sent})
                    reset_sent = True
            content = "".join(pieces).strip() or FALLBACK_MESSAGE
            if not reset_sent:
                # 模型未产出任何 token：直接回兜底文案
                content = FALLBACK_MESSAGE

        logger.info(f"域外问题通用回答完成: {content[:80]}")
        writer({"type": "message", "content": content})
    except Exception as e:
        # 模型调用失败时不抛异常，退化为兜底文案
        logger.error(f"通用问答失败: {e}")
        content = FALLBACK_MESSAGE
        writer({"type": "message", "content": content})
    # 回复写回会话历史，供下一轮追问复用
    return {"messages": [AIMessage(content=content)]}
