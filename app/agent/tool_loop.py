"""
通用有界工具调用循环

从 answer_general 中抽取，供各子 Agent（chat/sql/doc/knowledge）复用：
给定一个支持 bind_tools 的模型、初始消息列表与工具列表，循环执行
"模型请求工具 → 执行工具 → 回喂结果"，直到模型不再请求工具或达到轮数上限，
返回最终文本。

两种用法：
  - stream=True（chat）：逐 token 发 delta 事件，直接作为用户可见答案；
  - stream=False（sql/doc/knowledge 的工具预处理）：静默跑循环，只取模型产出的
    事实要点，作为"补充信息"拼进各自原生链路的输入，不污染用户答案气泡。
"""

import asyncio
from typing import Any

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from app.conf.app_config import app_config
from app.core.log import logger


# 把 MCP 工具返回值规整成可回喂给模型的纯文本；参数 result=tool.ainvoke 的返回值
def stringify_tool_result(result: Any) -> str:
    """将工具返回值（字符串 / 内容块列表）统一转成文本"""

    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        # MCP 工具常见返回 [{"type": "text", "text": "..."}] 形式的内容块
        for item in result:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(result)


# 执行一次工具调用（含超时与异常兜底），返回可回喂给模型的文本；参数 tool_map=工具名到工具的映射，call=模型给出的 tool_call
async def run_tool_call(tool_map: dict, call: dict) -> str:
    """执行单个工具调用，超时/异常都转成错误文本而不中断整轮回答"""

    name = call.get("name", "")
    tool = tool_map.get(name)
    if tool is None:
        return f"工具 {name} 不存在，请基于已有信息作答。"
    try:
        coro = tool.ainvoke(call.get("args", {}))
        result = await asyncio.wait_for(coro, timeout=app_config.mcp.timeout)
        return stringify_tool_result(result)
    except asyncio.TimeoutError:
        logger.warning(f"MCP 工具 {name} 调用超时（>{app_config.mcp.timeout}s）")
        return f"工具 {name} 调用超时，请基于已有信息作答或告知用户稍后再试。"
    except Exception as e:
        logger.warning(f"MCP 工具 {name} 调用失败：{e}")
        return f"工具 {name} 调用失败：{e}"


# 流式跑一次模型调用并聚合消息块；参数 model=带/不带工具的模型，messages=消息列表，pieces=文本累加器，writer=SSE 写器，reset_sent=首个 delta 是否已发，stream=是否向 writer 发 delta
async def _stream_once(
    model,
    messages: list,
    pieces: list[str],
    writer,
    reset_sent: bool,
    stream: bool,
) -> tuple[Any, bool]:
    """模型流式调用一次，按需发 delta，返回 (聚合消息块, reset_sent)"""

    gathered = None
    async for chunk in model.astream(messages):
        if not isinstance(chunk, AIMessageChunk):
            continue
        # 逐块累加：工具调用的参数也会以 tool_call_chunks 形式出现，聚合后得到完整 tool_calls
        gathered = chunk if gathered is None else gathered + chunk
        content = chunk.content
        if stream and isinstance(content, str) and content:
            pieces.append(content)
            writer({"type": "delta", "content": content, "reset": not reset_sent})
            reset_sent = True
        elif not stream and isinstance(content, str) and content:
            # 非流式仍收集文本，作为最终返回值
            pieces.append(content)
    return gathered, reset_sent


# 有界工具调用循环：模型请求工具→执行→回喂，直到不再请求工具或达到轮数上限；返回最终文本；
# 参数 model=支持 bind_tools 的模型，messages=初始消息列表，tools=工具列表，writer=SSE 写器(stream=False 时可不传)，max_rounds=轮数上限(默认取配置)，stream=是否流式
async def run_tool_loop(
    model,
    messages: list,
    tools: list,
    writer=None,
    max_rounds: int | None = None,
    stream: bool = True,
) -> str:
    """执行有界工具调用循环并返回最终文本"""

    rounds = max_rounds or app_config.mcp.max_tool_rounds
    llm_with_tools = model.bind_tools(tools)
    tool_map = {tool.name: tool for tool in tools}
    pieces: list[str] = []
    reset_sent = False
    answered = False

    for _ in range(rounds):
        gathered, reset_sent = await _stream_once(
            llm_with_tools, messages, pieces, writer, reset_sent, stream
        )
        tool_calls = getattr(gathered, "tool_calls", None) or []
        if not tool_calls:
            answered = True
            break
        # 回喂本轮助手消息（含 tool_calls），再逐个执行工具并追加 ToolMessage
        messages.append(gathered)
        for call in tool_calls:
            result = await run_tool_call(tool_map, call)
            messages.append(
                ToolMessage(
                    content=result,
                    tool_call_id=call.get("id", ""),
                    name=call.get("name", ""),
                )
            )
        logger.info(f"工具调用：calls={[c.get('name') for c in tool_calls]}")

    if not answered:
        # 达到轮数上限仍在请求工具：去掉工具强制模型基于已有信息收口
        logger.warning(f"工具轮数达到上限 {rounds}，强制收口")
        await _stream_once(model, messages, pieces, writer, reset_sent, stream)

    return "".join(pieces).strip()


# 用主模型跑一次静默工具循环，产出供原生链路使用的"补充信息"要点；
# 参数 question=用户问题，agent_name=子 Agent 名称，tools=该 Agent 专属工具，返回要点文本(无有用信息则为空)
async def collect_tool_supplement(question: str, agent_name: str, tools: list) -> str:
    """静默调用该 Agent 专属工具，返回一段简洁事实要点（供拼进原生链路输入）"""

    # 延迟导入，避免与 nodes/prompt 在模块加载期形成耦合
    from langchain_core.prompts import PromptTemplate

    from app.agent.llm import llm
    from app.prompt.prompt_loader import load_prompt

    try:
        prompt = PromptTemplate(
            template=load_prompt("agent_tool_supplement"),
            input_variables=["agent", "query"],
        )
        messages = [
            HumanMessage(content=prompt.format(agent=agent_name, query=question))
        ]
        text = await run_tool_loop(
            llm,
            messages,
            tools,
            writer=None,
            max_rounds=app_config.mcp.max_tool_rounds,
            stream=False,
        )
        # 模型判定无需工具时会输出"无"，统一归一化为空串
        if text.strip() in ("", "无", "无。", "None", "null"):
            return ""
        return text.strip()
    except Exception as e:
        logger.warning(f"{agent_name} 子 Agent 工具预处理失败，忽略补充信息：{e}")
        return ""
