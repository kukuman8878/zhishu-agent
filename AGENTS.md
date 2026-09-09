# AGENTS.md

智数「zhishu-agent」：FastAPI + LangGraph 智能数据分析 Agent。自然语言 → 元数据混合检索（Qdrant 向量 / ES 全文 / MySQL 权威元数据）→ SQL 生成/校验/修正/执行 → SSE 流式返回前端。
项目位于 `~/agent/zhishu-agent`。文档问答引擎源码在 `app/rag_engine/`，检索索引在 `data/doc_index/`（约 402M），均内置本仓库、进程内运行，**无外接独立服务**。

## 环境与启动（本机已就绪）

- 工具路径：`uv` 在 `~/.local/bin`，Node22/pnpm 在 `~/.local/node22/bin`（已写入 `~/.bashrc`）。新 shell 若找不到命令先 `export PATH="$HOME/.local/bin:$HOME/.local/node22/bin:$PATH"`。
- 后端依赖在 `.venv`（Python 3.14），安装/更新用 `uv sync`。
- 配置需真 Key：`~/agent/zhishu-agent/.env` 的 `LLM_API_KEY`（智数主链路）+ `SILICONFLOW_API_KEY`（进程内文档引擎，两者均走硅基流动）。`.env`/`conf/*.yaml` 改动需**重启后端**才生效；`.prompt` 改动**即时生效**（每次请求现读）。

后台服务当前运行中，日志在 `/tmp/backend.log`、`/tmp/frontend.log`。

### 命令（都在仓库根目录）
```bash
# 后端（热重载，改 .py 自动生效；必须在项目根目录运行否则报缺 fastapi[standard]）
uv run fastapi dev main.py --port 8000

# 基础服务（MySQL/ES/Kibana/Qdrant/TEI 共 5 个容器）
docker compose -f docker/docker-compose.yaml up -d
docker compose -f docker/docker-compose.yaml ps

# 重建元数据知识库（改了 conf/meta_config.yaml 后重跑；幂等）
uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml

# 前端（必须先 cd frontend）
cd frontend && pnpm dev     # 类型检查：pnpm lint（=tsc --noEmit）
```
无测试套件。手动验证：`POST /api/query`（SSE），样例 `{"query":"统计华北地区的销售总额"}`。

doc/hybrid 链路**进程内**调用文档问答引擎（代码 `app/rag_engine/`）：
- 索引 402M 在 `data/doc_index/`（`.env` 的 `INDEX_DIR` 指向，装载约 0.5s），随智数 lifespan 启动装载；
- 引擎的 embedding/rerank/LLM 全部走硅基流动（`.env` 的 `EMBEDDING_*`/`RERANK_*`/`LLM_*`/`SILICONFLOW_API_KEY`），与智数主链路的 TEI/主模型独立；
- `doc_engine.enabled=false` 时跳过装载，doc_query 降级为 message，hybrid 仍返回 SQL 侧结论；
- 改动 `.env`/`conf/*.yaml` 后重启本后端；`.prompt` 改动即时生效。

## 架构要点（无法从文件名直接看出）

- 图定义在 `app/agent/graph.py`：`START → classify_route`（五类路由闸门）按 `state["route"]` 分流：
  `knowledge`→`answer_knowledge`（知识沉淀复用）；`sql`→`extract_keywords`（原数据分析 12 节点链路）；`doc`→`doc_query`（进程内文档引擎）；`hybrid`→`hybrid_split`（并行扇出 SQL 链与 doc 链）→`synthesize`（合并三段式）；`chat`→`answer_general`。
- LLMWiki 式知识沉淀（`app/services/knowledge_service.py`）：`classify_route` 入口先向量检索知识库（Qdrant `knowledge_collection`，阈值 `conf/app_config.yaml` 的 `knowledge.recall_score_threshold`），命中即短路 `knowledge` 路由直接复用沉淀答案并 `hit_count+1`；未命中跑正常链路，`QueryService` 在 SSE 流结束后把 sql/doc/hybrid 的结论沉淀到 MySQL `knowledge_item` 表 + Qdrant（`knowledge.deposit_enabled` 开关，相似问题按 `dedup_score_threshold` 合并刷新）。`QueryService` 用 `stream_mode=["custom","values"]` 捕获终态。`knowledge.enabled=false` 完全旁路。**口径守卫**（`app/core/text_utils.py`）：①问句含未来年份（如 2030）跳过复用；②问句与沉淀问题的数字序列不一致（中文序数已归一化，如"第一/第二季度"）不复用；③指标词冲突（"会员数量" vs "订单数量"）不复用——bge 对数字/单业务词不敏感，仅靠相似度会答错；④省略式追问（"那按月呢"）不沉淀。沉淀去重同样受 ②③ 守卫。
- 结果自检（`app/agent/nodes/verify_result.py`，SQL 链 run_sql 成功后收口前）：仅主图 sql 路由触发（hybrid 分支/子图跳过）；空结果发确定性 `note` 事件，非空结果用 chat_llm 质检（prompt `verify_result.prompt`）可疑时发 `note`；fail-open 不影响结果。前端 `note` 事件渲染为结果旁的提醒条，不覆盖 result。
- 查询轨迹（`query_trace` 表 + `TraceMySQLRepository`）：`QueryService` 每次问答结束后写一条 {route,sql,sql_attempts,row_count,duration_ms,status,error,knowledge_item_id} 结构化快照（`trace.enabled` 开关），供复盘与评估；失败（SSE error）也留轨迹。
- `state`（TypedDict）放业务数据（含 `route`/`sql_rows`/`doc_*`/`hybrid_content`/`knowledge_*`/`run_error`/`sql_attempts`/`verify_note`），外部依赖（各 Repository/Client/DocEngineClient）放 `runtime.context`，节点经 `runtime.stream_writer` 发 SSE。**多轮会话下 `QueryService` 构造输入时把除 messages 外的业务字段重置为 None**，避免 checkpointer 残留上轮 sql/sql_rows 污染本轮；`synthesize` 栅栏用 `is not None` 判断。
- SSE 事件类型：`progress`/`result`/`message`/`doc`/`hybrid`/`note`/`error`。**`message`=闲聊纯文本**，前端打 `plainText` 标记隐藏流程图；**`doc`**=文档答案+引用页码；**`hybrid`**=三段式综合文本；**`note`**=结果自检提醒（附在结果旁）。`classify_route` 与 `answer_general` 刻意**不发 progress**。
- 双模型隔离（`app/agent/llm.py`）：`llm`=主链路（temperature 0，含 hybrid 综合）；`chat_llm`=意图判别+闲聊+结果自检，模型由 `app_config.yaml` 的 `llm.chat_model_name` 配置（当前 `deepseek-ai/DeepSeek-V3`）。
- `classify_route` 五级快判：知识召回（`knowledge` 阈值命中）→`SQL_HINTS`→sql；`DOC_HINTS`→doc；`HYBRID_HINTS`+业务词→hybrid；`CHAT_HINTS`→chat；均未命中才调 `chat_llm`（prompt `classify_route.prompt` 输出 JSON route）。**fail-open**：判别异常默认 `hybrid`（宁可多跑链路不误拦）。
- 多轮对话：`state["messages"]` 用 `add_messages` 归并 + graph 编译时挂 `InMemorySaver`（`thread_id`=前端 `session_id`，进程内有效，重启清空）。`classify_route` 写 HumanMessage，各终端节点写 AIMessage；`classify_route/generate_sql/answer_general` 的 prompt 注入 `{history}`（经 `app/agent/history.py` 渲染），支撑"那按月呢"这类省略式追问。前端 `App.tsx` 页面级生成 session_id，清空对话时重置。
- SQL 自愈闭环（`sql_subgraph.py` 拓扑）：`generate_sql→validate_sql→(correct_sql→validate_sql)*→run_sql`；`correct_sql` 每次修正 `sql_attempts+1`，`run_sql` 执行失败（非语法错，EXPLAIN 查不出的运行时错误）把报错写回 `run_error` 路由回 `correct_sql` 让模型看错误做最小修复，超过 `MAX_SQL_RETRIES=2` 才抛错。主图 run_sql 收口 `end_node="synthesize"`，子图收口 END（`build_sql_chain(builder, with_start, end_node)`）。
- `synthesize` 用**双分支完成栅栏**：仅在 `sql_rows` 与 `doc_answer` 两 key 都在 state 里才产出（LangGraph 对不同深度并行分支不做 join，synthesize 会被单边提前触发）；先到一次静默返回。
- hybrid 两档：`conf/app_config.yaml` 的 `hybrid.v2_enabled=false` 走 **v1**（`hybrid_split` 扇出整句到 SQL 链+doc 链→`synthesize`）；`true` 走 **v2**（`hybrid_v2` 单节点内 plan→execute→synthesize）。v2 的 sql 子任务复用独立编译的 `app/agent/sql_subgraph.py`（同 12 节点、单独 compile，`hybrid_v2` 用 `ainvoke` 逐子任务调用），doc 子任务并行调文档引擎，支持文档→SQL 桥接与失败互转。
- 新增 python 模块后注意 import 无副作用：`app/agent/sql_subgraph.py` 在 import 时 compile 一次子图，引用 `DataAgentState/DataAgentContext`，无网络调用。
- 文档引擎进程内化：外部方案的 agentic RAG 源码整体迁入 `app/rag_engine/`（agent/retrieval/vision/builders；不再保留独立 server/eval）。`app/clients/doc_engine_client_manager.py` 是**进程内引擎管理器**：lifespan 里 `IndexStore(INDEX_DIR)` 装载索引 + 建 `Agent`，`answer()` 返回归一化 dict（doc_query/hybrid_v2 消费逻辑稳定）。`rag_engine/config.py` 从智数根 `.env` 读配置（`PROJECT_ROOT=parents[2]`），索引内页面图路径为仓库内绝对路径。配置 `conf/app_config.yaml` 的 `doc_engine.{enabled,timeout}`；未启用/异常时 doc_query 降级为 `message` 事件并落空 `doc_*` 标记，保证 hybrid 仍能出 SQL 结论。
- ES 全文字段取值索引在代码里硬编码为 `value_index`（config 里的 `index_name: data_agent` 未实际使用，勿改它期望生效）。
- 仓储分层：`app/repositories/mysql/{meta,dw}`（dw 负责跑 SQL）、`qdrant`、`es`。SQL 执行/EXPLAIN 校验都集中在 `dw_mysql_repository.py`。
- 前端 `frontend/src/lib/agentApi.ts` 用 `fetch + ReadableStream` 解析 SSE；API 走 Vite 代理 `/api → VITE_DEV_PROXY_TARGET`（默认 8000）。

## 易踩的坑（本仓库特有）

- **`.prompt` 文件会被原样喂给 LLM**：不要在里面加注释。PromptTemplate 把单个 `{var}` 当占位符，JSON 示例里的花括号必须转义成 `{{...}}`，否则运行时报 "missing variables"。
- 数据库账号：`didilili`/`dili123`，库 `meta`（元数据）、`dw`（教学数仓）。容器数据在 named volume，`docker compose down` 不删数据；删 volume 才会重跑 `docker/mysql/*.sql` 初始化。
- 全中文代码：docstring/注释用中文，新代码沿用；ruff line-length 88、target py314（`pre-commit` 本地钩子跑 `ruff check --fix` + `ruff format`）。
- 网络：本机 docker.io 直连不通，镜像走 `/etc/docker/daemon.json` 配置的国内加速器；HuggingFace 下载需 `HF_ENDPOINT=https://hf-mirror.com`。
- 注意**不要改**：README 声称的 `data_agent` ES 索引名（误导）、`.prompt` 里的 {{ }} 转义。

## 风格与约束

- 代码含完整中文注释/docstring，改文件沿用同等密度；前端同理。
- 修改后端后靠 `fastapi dev` 热重载即可，但 `.env`/`conf/*.yaml` 变更要重启进程。
- 提问前先想：该走哪条路由——纯数据问题走 sql（消耗召回/SQL 全链路）；纯文档问题走 doc（进程内文档引擎）；两者都要走 hybrid（成本最高）；闲聊走廉价模型即可，勿让域外问题消耗任何业务链路。
