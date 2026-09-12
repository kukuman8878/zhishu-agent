"""
会话历史渲染工具

把 LangGraph 状态里累积的 messages（HumanMessage/AIMessage 列表）渲染成
"用户：…\n助手：…"的纯文本，供 classify_route/generate_sql/answer_general
等提示词注入历史上下文，支撑省略式追问（如"那按月呢"）的意图还原。
"""

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage


# 把会话消息渲染成多轮对话文本，截取最近 limit 条并截断单条长度；
# 参数 messages=消息列表，limit=最多保留条数，memory=长期记忆片段（摘要+用户偏好，可空）
def render_history(messages: list[AnyMessage], limit: int = 6, memory: str = "") -> str:
    """渲染最近 limit 条对话为纯文本，可选在开头注入长期记忆片段"""

    lines: list[str] = []
    for message in (messages or [])[-limit:]:
        if isinstance(message, HumanMessage):
            lines.append(f"用户：{message.content}")
        elif isinstance(message, AIMessage):
            lines.append(f"助手：{str(message.content)[:200]}")

    history = "\n".join(lines) if lines else "（无历史对话）"
    if memory:
        # 长期记忆（摘要/偏好）置于历史之前，供模型优先参考
        return f"{memory}\n\n{history}"
    return history
