"""
四类记忆契约

智数的记忆不再只存进程内存，按用途分为四类：
  - WORKING   工作记忆（短期）：当前会话消息，持久化到 SQLite checkpointer；
  - SUMMARY   摘要记忆（中期）：长对话滚动摘要，落 Meta MySQL；
  - USER      用户记忆（长期）：跨会话画像/偏好，落 Meta MySQL；
  - KNOWLEDGE 知识记忆（长期）：已沉淀问答知识，复用 knowledge_item + Qdrant。

本包提供统一枚举与上下文对象，具体读写见 working.py 与 manager.py。
"""

from dataclasses import dataclass, field
from enum import Enum


class MemoryType(str, Enum):
    """记忆类型枚举（四类）"""

    WORKING = "working"
    SUMMARY = "summary"
    USER = "user"
    KNOWLEDGE = "knowledge"


@dataclass
class MemoryContext:
    """一次问答加载到的长期记忆上下文（摘要 + 用户偏好）"""

    summary: str = ""
    user_facts: list[str] = field(default_factory=list)

    # 渲染成可注入提示词的纯文本块；无内容时返回空串；参数：无
    def render(self) -> str:
        """把摘要与用户偏好渲染成提示词可用的记忆片段"""
        blocks: list[str] = []
        if self.summary:
            blocks.append(f"【会话摘要】\n{self.summary}")
        if self.user_facts:
            facts = "\n".join(f"- {fact}" for fact in self.user_facts)
            blocks.append(f"【用户偏好】\n{facts}")
        return "\n\n".join(blocks)


__all__ = ["MemoryType", "MemoryContext"]
