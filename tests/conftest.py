"""
pytest 共享配置

本仓库的后端模块在 import 时可能触发配置加载（app_config 读 .env / 环境变量）
或初始化外部客户端。纯逻辑单元测试不应依赖真实密钥或外部服务，因此在收集
测试前注入最小可用的假环境变量，保证只测函数逻辑、不触发真实网络调用。
"""

import os

# 在 import 任何 app.* 模块前注入假密钥：app_config 用 ${oc.env:...} 解析，
# 缺这些变量会抛异常；此处给假值即可让配置加载通过，测试不真正联网。
os.environ.setdefault("LLM_API_KEY", "test-llm-key")
os.environ.setdefault("SILICONFLOW_API_KEY", "test-siliconflow-key")
os.environ.setdefault("EMBEDDING_BINDING_HOST", "http://localhost:9999")
os.environ.setdefault("RERANK_BINDING_HOST", "http://localhost:9999")
os.environ.setdefault("LLM_BINDING_HOST", "http://localhost:9999")
