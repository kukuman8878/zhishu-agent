"""
智数 Agent 使用的大模型实例

用途：集中初始化两个 OpenAI 兼容的 Chat Model，供各节点或本地调试直接复用，
避免每个节点各自创建连接：

- llm：问数主链路（关键词判别/表指标过滤/SQL 生成/校验）使用。
  这类任务追求结果稳定，temperature=0，配置为 conf/app_config.yaml 的 model_name。
- chat_llm：通用闲聊/域外问题使用（classify_route 与 answer_general 两个节点）。
用配置里的 chat_model_name 指定轻量快速模型，temperature 略高让回复更自然，
  与主模型隔离，避免低价值请求占用主模型额度与成本。
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

if __name__ == "__main__":
    # 本地快速验证 LLM 配置是否能正常调用
    print(llm.invoke("你好").content)
