"""
智数 Agent 使用的大模型实例

用途：按任务分三级集中初始化 OpenAI 兼容的 Chat Model，供各节点或本地调试直接复用，
避免每个节点各自创建连接（分级策略见 AGENTS.md「任务分级模型」）：

- llm：主模型，只保留质量关键环节（SQL 生成/修正、跨源综合），temperature=0，
  由 conf/app_config.yaml 的 model_name 配置。
- chat_llm：轻量模型，供意图判别/闲聊/结果自检/记忆/评估使用，由 chat_model_name 配置。
- aux_llm：辅助模型，供召回关键词扩展、表/指标过滤选择、编排计划拆解等结构化辅助任务；
  由 aux_enabled/aux_model_name 配置，关闭时回退主模型。
"""

from langchain.chat_models import init_chat_model

from app.conf.app_config import app_config

# 主链路模型：字段扩展、SQL 生成更看重稳定性，所以关闭随机发散（temperature=0）
llm = init_chat_model(
    model=app_config.llm.model_name,
    # 硅基流动等服务兼容 OpenAI 协议时，使用 openai provider 接入
    model_provider="openai",
    base_url=app_config.llm.base_url,
    api_key=app_config.llm.api_key,
    temperature=0,
)

# 通用闲聊模型：供意图判别/域外问答复用，独立于主模型便于分别配置与计量
chat_llm = init_chat_model(
    model=app_config.llm.chat_model_name,
    model_provider="openai",
    base_url=app_config.llm.base_url,
    api_key=app_config.llm.api_key,
    # 闲聊不需要严格一致，temperature 略高更自然
    temperature=0.7,
)

# 辅助任务模型：召回关键词扩展、表/指标过滤选择、编排计划拆解等结构化辅助任务。
# 结构稳定性要求 temperature=0；aux_enabled=false 或未配置名称时直接复用主模型，
# 保证"关掉分级 = 旧行为"，便于成本/质量 A/B 对比。
if app_config.llm.aux_enabled and app_config.llm.aux_model_name:
    aux_llm = init_chat_model(
        model=app_config.llm.aux_model_name,
        model_provider="openai",
        base_url=app_config.llm.base_url,
        api_key=app_config.llm.api_key,
        temperature=0,
    )
else:
    aux_llm = llm

if __name__ == "__main__":
    # 本地快速验证 LLM 配置是否能正常调用
    print(llm.invoke("你好").content)
