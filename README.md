<div align='center'>
  <h1 style="margin-top: 15px;">「智数」智能数据分析 Agent</h1>
  <h4><b>zhishu-agent</b></h4>
  <p><em>一个可以照着一步步跑起来、也能把原理讲透的 LangGraph 智能问数工程：自然语言问数仓、混合检索元数据、SQL 生成与自愈执行、SSE 流式交付，并内置进程内文档问答、跨源综合、多智能体编排、MCP 工具、四类记忆与结果评估。</em></p>
</div>

<div align='center'>

![AI](https://img.shields.io/badge/AI-Agent-00c853?style=flat)
![Python](https://img.shields.io/badge/Python-3.14-3776AB.svg?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-Agentic%20Workflow-1C3C3C.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi&logoColor=white)
![Stars](https://img.shields.io/github/stars/kukuman8878/zhishu-agent?logo=github&style=flat)

</div>

**一句话说清楚**：你对着聊天框用中文提问，`智数` 先判断你想「查数」「查文档」「两样都要」还是「只是闲聊」，然后要么去数据仓库真的跑一条 SQL 把结果表格给你，要么翻开 PDF/图表找出答案并标注页码，要么两边同时开工再综合成结论——全过程像流水线一样逐步点亮，答案会像打字机一样一个字一个字流出来。

**它适合谁？**

- 完全没接触过 `LangGraph`/`RAG`，想找一个能跑起来、能读懂、能改的完整项目练手的人；
- 写过几次「让大模型直接吐 SQL」的玩具，想搞清楚真正可用需要解决哪些工程问题的人；
- 想把 `MySQL` + `Qdrant` + `Elasticsearch` + 大模型放进同一个业务场景里理解「混合检索 + 智能体编排」的人；
- 想把它写进简历，并能把数据层、检索层、智能体层、服务层、前端层分别讲明白的人。

> 说明：项目围绕**电商教学数仓问数**场景展开，其中文档问答引擎为进程内实现，不再需要外接一套独立部署的应用服务；但整体仍依赖 MySQL/Qdrant/Elasticsearch 等 Docker 基础服务与云端大模型 API，且**不做生产级治理**（登录、权限、限流、审计等），它的定位是「把智能问数最关键、最值得学的工程链路讲清楚、跑起来」。

![智数前端首页：样例问题、自然语言输入和智能数据分析 Agent 界面](docs/images/zhishu-home.jpg)

## 目录

- [核心概念扫盲](#核心概念扫盲)
- [它能回答什么问题](#它能回答什么问题)
- [系统架构总览](#系统架构总览)
- [一次问数的完整旅程](#一次问数的完整旅程)
- [五条路由详解](#五条路由详解)
- [可选增强能力](#可选增强能力)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [SSE 事件类型](#sse-事件类型)
- [项目结构导览](#项目结构导览)
- [代码阅读学习路径](#代码阅读学习路径)
- [常见问题 FAQ](#常见问题-faq)
- [能力边界与可扩展方向](#能力边界与可扩展方向)

## 核心概念扫盲

如果你对下面这些词还很陌生，先花 10 分钟把这一节读完，后面会顺很多。每个词都用大白话解释，并告诉你「在本项目里对应哪块代码」。

| 概念 | 一句话解释 | 在本项目里对应什么 |
| ---- | ---------- | ------------------ |
| **NL2SQL** | Natural Language to SQL，把「统计华北销售额」这样的自然语言翻译成可执行 SQL。难点不是翻译本身，而是让模型知道「华北」是哪个字段、「销售额」是哪张表的哪一列。 | 整条 `sql` 路由就是在做这件事：`app/agent/nodes/sql_chain.py` 的 `generate_sql`。 |
| **元数据（Metadata）** | 「描述数据的数据」。比如表名、字段名、字段含义、别名、指标口径、字段里都有哪些真实取值。模型只有拿到元数据，写 SQL 时才知道有什么可写。 | 表/字段/指标存在 MySQL（`meta` 库），配置来自 `conf/meta_config.yaml`。 |
| **混合检索** | 不同信息用不同检索方式，最后合并。字段/指标这类「语义相近但字面不同」的内容适合向量语义检索；字段取值这类「精确词语」适合全文检索。 | 字段、指标走 Qdrant 向量召回，字段取值走 Elasticsearch 全文召回，见 `app/agent/nodes/recall.py`。 |
| **向量 / Embedding** | 把一段文字变成一串数字（向量），语义越接近的文字，向量在空间里越靠近。这样才能实现「销售额」也能搜到「订单金额」。 | 用本地 TEI 服务跑 `BAAI/bge-large-zh-v1.5`，见 `app/clients/embedding_client_manager.py`。 |
| **重排 / reranker** | 向量召回是「粗筛」（快但不够准），reranker 是 cross-encoder「精排」：把问题和每个候选放一起打分，从中挑最相关的几条。 | 召回后统一交给 `app/agent/rerank.py`，模型 `BAAI/bge-reranker-base`，端口 8082。 |
| **Qdrant** | 一个向量数据库，负责存向量、按相似度检索。 | 字段向量、指标向量、知识沉淀向量都存在 Qdrant，见 `conf/app_config.yaml` 的 `qdrant` 段。 |
| **Elasticsearch** | 全文检索引擎，擅长关键词匹配和值域检索。 | 存字段的真实取值（如「华北」「华东」），见 `app/repositories/es/value_es_repository.py`。 |
| **LangGraph** | 把 AI 流程画成一张「状态机图」的框架。下面单独拆开讲。 | 主图 `app/agent/graph.py`，SQL 子图 `app/agent/sql_subgraph.py`。 |
| **RAG** | Retrieval-Augmented Generation，检索增强生成：先检索到可靠资料，再让模型「照着资料答」，而不是凭记忆瞎编。 | 文档问答引擎 `app/rag_engine/`，以及问数前的元数据召回，本质都是 RAG。 |
| **SSE** | Server-Sent Events，服务器不断往浏览器「推消息」的单向流式协议。因为一次问数要跑十几步、好几秒，用 SSE 可以让前端实时看到进度。 | `POST /api/query`，见 `app/services/query_service.py` 与 `frontend/src/lib/agentApi.ts`。 |
| **LLM-as-judge** | 用大模型当「裁判」，给另一次问答的质量打分（忠实性、相关性、完整性），用于质量监控。 | 可选的结果评估：`app/services/evaluation_service.py` + `app/agent/judges/`。 |
| **MCP** | Model Context Protocol，一套让大模型调用外部工具（查天气、算数、联网搜索）的开放协议。 | 内置时间/计算器/单位换算工具，见 `app/mcp_servers/builtin_server.py`。 |

### 重点补课：LangGraph 的六个关键词

- **StateGraph（状态图）**：一张图就是一套流程。图的「共享内存」叫 **State**（本项目是 `app/agent/state.py` 里的 `DataAgentState`），每个环节读写它，顺着图往下传。
- **节点（Node）**：图里的一个环节，本质是一个函数。比如「抽取关键词」「生成 SQL」各是一个节点。
- **边（Edge）**：节点之间的固定连线，规定「做完 A 紧接着做 B」。
- **条件路由（conditional_edges）**：不是所有流程都一条直线。根据 State 里的值决定下一步去哪，比如查到 `route == "sql"` 就走数据链，`route == "doc"` 就走文档链。入口的 `classify_route` 就是干这个的。
- **子图（Subgraph）**：把一段流程单独编译成一张小图，可被主图或其它节点反复调用。本项目把「SQL 分析链路」编译成 `sql_graph`，供跨源综合 v2 复用（LangGraph 不允许在图节点内部递归调用同一张正在执行的主图，所以必须单独编译）。
- **checkpointer（检查点）**：给图配一个「存档器」，把每一轮的状态存下来，用 `thread_id` 区分会话。本项目默认存到 SQLite 文件 `data/memory/checkpoints.db`，所以**重启后端对话历史也不丢**。

> 除此之外还有两个概念你会在代码里看到：
> - **State 放业务数据，Context 放外部依赖**。状态里只放会被节点读写的数据；数据库连接、Qdrant 客户端这类「工具」放进 `app/agent/context.py` 的 `DataAgentContext`，通过 `runtime.context` 取用。这样状态干净、可序列化，方便存档。
> - **流式写器 `runtime.stream_writer`**：节点用它往 SSE 里写进度/结果事件，前端才能实时点亮流程图。

## 它能回答什么问题

打开前端聊天框，可以直接问下面这些。**你不用关心走的是哪条链**——入口的 `classify_route` 会自动判断。

| 类型 | 示例问题 | 走哪条链 | 你会看到什么 |
| ---- | -------- | -------- | ------------ |
| 数据查询 | `统计华北地区的销售总额` | `sql` | 流程图自上而下点亮 + 结果表格 |
| 文档问答 | `手册里怎么配置 XX 参数` | `doc` | 文档答案 + 引用页码 |
| 跨源综合 | `结合数仓数据和产品文档，分析华东销售下滑原因` | `hybrid` | 【数据结论】【文档说明】【综合判断】三段式 |
| 闲聊 / 域外 | `你是谁？会做什么` | `chat` | 一句自然语言回复，不显示流程图 |
| 复用 | 把上次的**完整问题**再问一遍 | `knowledge` | 命中沉淀知识，秒回 |
| 省略式追问 | `那按月再看呢` | 原链路（`sql`/`doc`/`hybrid`） | 靠工作记忆带上文，重新走一遍链路 |

几点补充：

- `chat` **开箱即可体验**：只需一个可用的模型 Key。
- `sql` 需完整启动基础服务并完成元数据知识库构建（第 5-7 步）后才能体验，**仅有 MySQL 不够**（还要 Qdrant、Elasticsearch、Embedding/重排服务）。
- `doc` / `hybrid` 依赖文档问答索引 `data/doc_index`（约 402M，不随 git 仓库分发）。**没有准备好索引时，doc 链会自动降级为一句提示消息，hybrid 仍会给出 SQL 侧结论**，不会整个报错。
- 知识复用（`knowledge`）由 `conf/app_config.yaml` 的 `knowledge.enabled` 控制，默认开启。

## 系统架构总览

![智数系统架构图：前端通过 FastAPI 和 SSE 连接后端，LangGraph 问数智能体基于 Jieba、MySQL、Qdrant、Elasticsearch 和 LLM 完成召回、SQL 生成校验执行与结果返回](docs/images/zhishu-system-architecture.svg)

项目围绕**两条主线**展开：

| 主线 | 什么时候发生 | 做什么 | 涉及模块 |
| ---- | ------------ | ------ | -------- |
| **① 元数据知识库构建**（离线、跑一次） | 首次启动、改了 `conf/meta_config.yaml` 之后 | 读取教学数仓的表结构，把表/字段/指标写入 MySQL，把字段/指标向量写入 Qdrant，把字段真实取值写入 Elasticsearch | `app/scripts/build_meta_knowledge.py`、`app/services/meta_knowledge_service.py`、`app/repositories/*` |
| **② 自然语言问数**（在线、每次提问） | 用户提问时 | 判断路由 → 召回相关元数据 → 组织上下文 → 生成/校验/执行 SQL（或查文档）→ SSE 流式返回 | `app/agent/*`、`app/services/query_service.py`、`app/api/*` |

数据在两条主线之间是这样流动的：

```text
                 ┌─────────────── ① 元数据知识库构建（离线）───────────────┐
教学数仓(dw) ──► 抽取表/字段/指标 ──┬──► MySQL meta（权威结构元数据）
                                   ├──► Qdrant（字段向量 / 指标向量）
                                   └──► Elasticsearch（字段真实取值）
                 └────────────────────────────────────────────────────────┘
                                            │ 供检索
                                            ▼
用户问题 ──► classify_route（路由闸门）──► ② 召回 + 过滤 + 生成/校验/执行 ──► SSE ──► 前端
```

![智数查询结果页：LangGraph 执行流程、SQL 校验执行和查询结果表格](docs/images/zhishu-query-result.jpg)

## 一次问数的完整旅程

下面以 `统计华北地区的销售总额` 为例，把 `sql` 路由的每一步「在做什么、为什么要这么做」讲清楚。这张图由 `app/agent/graph.py` 定义，SQL 链路的边定义抽在 `app/agent/sql_subgraph.py` 的 `build_sql_chain()` 里，主图和子图共享同一份拓扑。

> **节点数量**：SQL 链路一共注册了 **13 个节点**（`sql_subgraph.py` 的 `_register_sql_nodes()`），包括最后的结果自检 `verify_result`。入口的 `classify_route` 和收口的 `synthesize` 不在其中。

```text
START
  └─ classify_route ───────────────── 意图路由：判定走哪条链
       └─ (route=sql) extract_keywords ─ 抽取关键词（jieba + 原始问题兜底）
            ├─ recall_column ───────── 字段召回（Embedding → Qdrant 向量）
            ├─ recall_metric ───────── 指标召回（Embedding → Qdrant 向量）
            └─ recall_value ────────── 字段取值召回（Elasticsearch 全文）
                 └─ merge_retrieved_info ─ 合并三路，按 id 补齐结构元数据
                      ├─ filter_table ──── 过滤候选表（LLM 只选择，程序裁剪）
                      └─ filter_metric ─── 过滤候选指标
                           └─ add_extra_context ─ 补当前日期 / 数据库方言版本
                                └─ generate_sql ── 主模型生成 SQL
                                     └─ validate_sql ─ EXPLAIN 校验
                                          ├─(有错) correct_sql ─► validate_sql（自愈环）
                                          └─(无错/重试耗尽) run_sql ─ 执行并发 result
                                               └─ verify_result ─ 结果自检（空/可疑发 note）
                                                    └─ synthesize ─ 收口（sql 路由透传）
```

逐节点说明：

| 顺序 | 节点 | 在做什么 | 为什么这么做 |
| ---- | ---- | -------- | ------------ |
| 0 | `classify_route` | 判断问题属于五类中的哪一类，写入 `state["route"]`；同时把用户消息写入会话历史。 | 不同问题成本差异很大，先分流能避免闲聊消耗昂贵的 SQL 链路。 |
| 1 | `extract_keywords` | 用 jieba 按词性抽关键词，并把原始问题也加进去作为兜底。 | 关键词是后续三路召回的入口；保留整句可避免分词不准时丢语义。 |
| 2a | `recall_column` | 用辅助模型把问题扩成「字段语义」词，向量化后在 Qdrant 召回字段，再重排。 | 用户说「销售额」，字段可能叫 `order_amount`，需要语义匹配。 |
| 2b | `recall_metric` | 同上，但召回的是业务指标（如 GMV、AOV）。 | 指标是业务口径，必须由元数据提供，不能靠模型猜公式。 |
| 2c | `recall_value` | 把问题扩成可能出现在字段值里的词，去 Elasticsearch 做全文检索。 | 「华北」「华东」是字段的真实取值，全文匹配更准。 |
| 3 | `merge_retrieved_info` | 合并三路结果；补齐指标依赖字段、字段取值样例、主外键、表信息。 | 让后续节点只面对统一的「表上下文」，不用关心信息来自哪一路。 |
| 4a | `filter_table` | 把候选表交辅助模型选「保留哪些表、哪些字段」，程序再按选择裁剪原结构。 | 候选太多会干扰模型；让模型只做选择、不重写结构，更稳。 |
| 4b | `filter_metric` | 同上，裁剪候选指标。 | 同 4a。其中 4a/4b 并行执行，都完成后才进下一步。 |
| 5 | `add_extra_context` | 补上「今天的日期/星期/季度」和「数据库方言/版本」。 | 让模型能正确处理「本月」「最近 30 天」，并写出符合该数据库语法的 SQL。 |
| 6 | `generate_sql` | **主模型**依据过滤后的表/指标上下文、日期、方言和历史，生成候选 SQL。 | 这是全链路质量最关键的环节，只用主模型，temperature=0 保证稳定。 |
| 7 | `validate_sql` | 对 SQL 执行 `EXPLAIN`，只关心数据库能否解析、计划是否可行。 | 用真实数据库校验，比模型自查靠谱；错误写回状态而不是抛异常。 |
| 8 | `correct_sql` | 带着完整上下文和数据库报错，让模型做**最小必要修正**。每次修正 `sql_attempts + 1`。 | 修错时不能只改语法把业务语义改丢，且要计数防止无限循环。 |
| 9 | `run_sql` | 真正执行 SQL：成功发 `result` 事件并写回行数据；失败则把报错写回 `run_error`，路由回 `correct_sql` 再试。 | 这就是「自愈」：运行时错误（EXPLAIN 查不出）也能让模型看着报错改。 |
| 10 | `verify_result` | 结果为空就发确定性 `note` 提示；结果非空用廉价模型质检，可疑时发 `note`。仅 `sql` 路由触发。 | 结果可疑时提醒用户核对，但**只提醒不阻断**，fail-open。 |
| 11 | `synthesize` | 收口节点。`sql` 路由直接透传；只有 `hybrid` 路由才会在这里合并数据与文档。 | 统一收口，保证单路由的既有事件流零回归。 |

关于自愈的两个上限（`app/agent/nodes/sql_chain.py`）：

- `MAX_SQL_RETRIES = 2`：无论是 `EXPLAIN` 校验失败还是运行时执行失败，修正次数都用 `sql_attempts` 计数；达到上限仍失败才真正抛出，由上层包装成 SSE `error`。
- `validate_sql` 在「重试耗尽仍不合语法」时会放行到 `run_sql`，让执行失败来收口，避免死循环。

## 五条路由详解

路由入口在 `app/agent/nodes/classify_route.py`。它的判断顺序是「**知识复用 → 本地特征词快判 → LLM 语义兜底**」，并且任何一步异常都 **fail-open 到 `hybrid`**（宁可多跑链路，也不要把真查询误拦在门外）。

### 1. `sql` 路由：查数据

- **触发**：问题命中 `SQL_HINTS`（销售、订单、GMV、地区、统计、排名、退款、库存……）且没有文档词；或 LLM 判别为 `sql`。
- **流程**：走上面「完整旅程」里的 13 个节点，真实执行 SQL 并返回结果表格。
- **特点**：会完整展示数据分析流程图（13 个节点自上而下点亮）；`doc`/`hybrid` 也会展示各自进度（`hybrid` 复用 SQL 链节点），只有 `chat`/`knowledge` 不展示流程图。成本上 `hybrid`（SQL + 文档 + 综合）才是最高的一条链路（v1 下 SQL 生成 + 综合各一次主模型调用，另有多次辅助模型调用；v2 另有计划拆解一次主模型调用）。

### 2. `doc` 路由：查文档

- **触发**：问题命中 `DOC_HINTS`（文档、手册、报告、规格、参数、怎么配置、架构图、第几页……）。
- **流程**：直接进入 `doc_query` 节点（`app/agent/nodes/doc_query.py`），把问题交给**进程内**文档问答引擎 `app/rag_engine/`，拿回答案与引用页码，发 `doc` 事件。
- **RAG 强约束（可溯源）**，三道门：
    1. 所有作答类提示词都追加 `TRACEABILITY_RULES`（`app/rag_engine/agent/prompts.py`），要求「仅依证据作答、必须给引用页、无法溯源输出 `NOT_FOUND`」；
    2. **重排分门** `_rerank_gate`（`app/rag_engine/agent/agent.py`）：证据里最高 bge-reranker 分低于 `.env` 的 `RERANK_MIN_SCORE`（默认 0.30）直接拒答；
    3. **可溯源强制门**（`app/clients/doc_engine_client_manager.py`）：模型给出实质答案却拿不到任何引用页时，改判 `NOT_FOUND`（`.env` 的 `REQUIRE_CITATION=1` 控制）。
- **降级**：`doc_engine.enabled=false`、索引未准备或引擎异常时，降级为 `message` 提示，不影响其它链路。

### 3. `hybrid` 路由：数据和文档一起上

- **触发**：同时命中数据词与文档词，或出现跨域词（结合、对照）加上任一业务词。
- **两档实现**，由 `conf/app_config.yaml` 的 `hybrid.v2_enabled` 决定：
    - **v1（默认，`v2_enabled=false`）**：`hybrid_split` 是一个空操作「扇出闸门」，靠 `graph.py` 里它的两条出边，并行把**整句问题**同时送进 SQL 链和文档链；两条都完成后由 `synthesize` 合并。
    - **v2（`v2_enabled=true`）**：`hybrid_v2` 单节点内完成「计划 → 执行 → 综合」。先用主模型把问题拆成带引擎标记的子任务（`sql`/`doc`，最多 3 个），doc 子任务并行、sql 子任务复用**独立编译**的 `sql_graph`；支持「文档→SQL 桥接」（把依赖的文档答案拼进 SQL 子问句，让取值召回命中真实枚举）和失败互转（sql 失败可尝试 doc，反之亦然）。
- **收口**：最终都产出三段式文本「【数据结论】【文档说明】【综合判断】」，发 `hybrid` 事件；组织文本时还会计 `delta` 逐字流式。

> **为什么要有「双分支完成栅栏」？** LangGraph 对不同深度的并行分支不做 join，`synthesize` 可能在单边完成时就被提前触发。所以 `synthesize` 只有在 `sql_rows` 和 `doc_answer` 两个 key 都写入（用 `is not None` 判断）后才真正产出，先到的一次静默返回（`app/agent/nodes/synthesize.py`）。

### 4. `chat` 路由：闲聊 / 域外问题

- **触发**：命中 `CHAT_HINTS`（你好、谢谢、你是谁、天气、搜索、翻译……）且没有业务词。
- **流程**：`answer_general` 节点（`app/agent/nodes/answer_general.py`）用廉价的 `chat_llm` 直接回答，逐字发 `delta`，最后发 `message`。**刻意不发 `progress`**，前端因此不会显示流程图。
- **MCP 工具**：如果给 `chat` 配了 MCP 工具（默认内置时间/计算器/单位换算），会先跑一轮有界工具调用，让模型能回答「现在几点」「算一下」「换算单位」这类问题。

### 5. `knowledge` 路由：命中沉淀知识，秒回

- **触发**：`knowledge.enabled=true` 时，入口先把问题向量化，去 Qdrant 知识库检索最相似的沉淀问题。相似度达到 `knowledge.recall_score_threshold`（默认 0.78）即复用。
- **口径守卫**（`app/core/text_utils.py`，很重要）：即使相似度很高，以下情况也**不复用**，避免答错：
    1. 问句含未来年份（如 2030）；
    2. 数字序列不一致（年份/季度/TopN 变化，中文序数已归一化）；
    3. 指标词冲突（「会员数量」vs「订单数量」）；
    4. 地区不一致（「华中」vs「华南」）。
- **沉淀**：每次 `sql`/`doc`/`hybrid` 问答结束后，`QueryService` 会把有价值的结论写入 MySQL `knowledge_item` 表 + Qdrant（`knowledge.deposit_enabled` 控制），相似问题按 `dedup_score_threshold`（默认 0.80）合并刷新。命中复用时会累加 `hit_count`。
- **省略式追问**：`那按月呢` 这类问题靠的是**工作记忆**（会话历史 + 摘要）注入提示词，而不是知识库命中——知识库只复用「完整问句」的结论。

## 可选增强能力

这些都通过 `conf/app_config.yaml` 开关控制，**改动后需要重启后端**。默认值如下，全部可以一键回退。

| 能力 | 开关（默认值） | 开了有什么变化 | 代码位置 |
| ---- | -------------- | -------------- | -------- |
| **多智能体编排** | `orchestrator.enabled`（默认 `false`） | 入口直接交给主 Agent：先做知识复用门，再跑 `classify_route._fast_route` 静态快判，命中 `sql/doc/hybrid/chat` 时直接确定子 Agent 组合、免 planner，只有模棱两可才用主模型拆成 `sql/doc/chat` 子任务（doc 子任务并行、sql 子任务顺序执行以支持文档→SQL 桥接），最后按成功结果组合终端事件。关闭时完全走原五类路由。 | `app/agent/nodes/orchestrator.py`、`app/agent/agents/` |
| **MCP 外部工具** | `mcp.enabled`（默认 `true`） | 给子 Agent 绑定专属 MCP 工具。内置工具（当前时间/日期、算术计算器、单位换算）无需 Key 开箱即用；可扩展 Tavily 搜索、OpenWeather 天气。每个 server 用 `agents: [chat/sql/doc/knowledge]` 声明归属。连接失败一律 fail-open。 | `app/clients/mcp_client_manager.py`、`app/agent/tool_loop.py`、`app/mcp_servers/builtin_server.py` |
| **四类记忆** | `memory.enabled`（默认 `true`） | ① 工作记忆：会话消息，持久化到 SQLite（`data/memory/checkpoints.db`，重启不丢）；② 摘要记忆：消息数达 `summary_trigger_messages`（12）时滚动摘要；③ 用户记忆：从对话抽取稳定偏好，落 MySQL `agent_memory` 表；④ 知识记忆：复用 `knowledge_item` + Qdrant。 | `app/agent/memory/manager.py`、`app/agent/memory/working.py` |
| **结果评估** | `eval.enabled`（默认 `false`） | 每次问答的终端答案交给「LLM 裁判 + 规则检查」加权打分（权重 `llm_weight`），写入 `query_eval` 表；低于 `pass_threshold` 且 `emit_note=true` 时补发一条 `note`（不覆盖答案）。评估异常只记日志，不影响用户答案。 | `app/services/evaluation_service.py`、`app/agent/judges/` |
| **任务分级控成本** | `llm.aux_enabled`（默认 `true`） | 把非关键任务下放廉价模型：主模型只做 SQL 生成/修正、跨源综合与跨源/编排的计划拆解；意图判别/闲聊/自检/记忆/评估用 `chat_model_name`；召回关键词扩展、表/指标过滤用 `aux_model_name`。关闭后全部回退主模型（行为与旧版一致，便于 A/B）。 | `app/agent/llm.py` |
| **查询轨迹** | `trace.enabled`（默认 `true`） | 每次问答结束写一条结构化快照到 `query_trace` 表（路由、SQL、修正次数、行数、耗时、状态等），失败也留档，供复盘。 | `app/repositories/mysql/meta/trace_mysql_repository.py` |
| **知识沉淀** | `knowledge.enabled` / `deposit_enabled`（默认 `true`） | 见「五条路由详解 → knowledge」。 | `app/services/knowledge_service.py` |

## 快速开始

> 目标：从零把项目在自己电脑上跑起来。全程约 20~40 分钟，大头在下载模型和拉 Docker 镜像。**不想折腾前端**的话，跑到第 8 步用 `curl` 就能体验问答。
>
> 下面命令都在**项目根目录**执行。新开终端如果找不到 `uv`/`pnpm`，先执行：
> ```bash
> export PATH="$HOME/.local/bin:$HOME/.local/node22/bin:$PATH"
> ```

### 步骤 1：准备 4 个工具

前 3 个「跑起来必需」，前端（Node + pnpm）可后装。**已装就跳过**：

| 工具 | 是什么 | 怎么装 | 验证 |
| ---- | ------ | ------ | ---- |
| **Python 3.14** | 后端语言 | https://www.python.org/downloads/ | `python3 --version` |
| **uv** | Python 包/环境管理器（快） | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `uv --version` |
| **Docker** | 跑 MySQL / Qdrant / ES 等基础服务 | https://www.docker.com/products/docker-desktop/ | `docker --version` |
| **Node + pnpm** | 前端（可选） | 先装 Node https://nodejs.org，再 `npm i -g pnpm` | `node --version` / `pnpm --version` |

> **在做什么**：检查「语言运行时 + 依赖管理器 + 容器引擎」是否齐备。
> **常见问题**：`uv: command not found` → 重开终端，或 `export PATH="$HOME/.local/bin:$PATH"`。

### 步骤 2：克隆项目

```bash
git clone https://github.com/kukuman8878/zhishu-agent.git
cd zhishu-agent
```

> **验证成功**：能看到 `pyproject.toml`、`main.py`、`app/`、`frontend/` 等。

### 步骤 3：安装后端依赖

```bash
uv sync
```

> **在做什么**：按 `pyproject.toml` + `uv.lock` 创建 `.venv` 并装好全部依赖。
> **验证成功**：输出里有 `Installed ... packages`。
> **常见报错**：网络慢可设置国内 PyPI 镜像后重试。

### 步骤 4：配置大模型 API Key

```bash
cp .env.example .env
```

然后编辑 `.env`，至少填两个真实 Key（本项目默认对接**硅基流动** SiliconFlow，国内可直连、注册送额度）：

```bash
# 智数主链路（问数、意图判别、闲聊）
LLM_API_KEY=你的硅基流动Key
# 进程内文档问答引擎（doc/hybrid 链路）
SILICONFLOW_API_KEY=你的硅基流动Key
```

模型名在 `conf/app_config.yaml` 的 `llm` 段配置，默认值如下：

```yaml
llm:
  model_name: Pro/zai-org/GLM-5.1            # 主模型：SQL 生成/修正 + 跨源综合
  chat_model_name: deepseek-ai/DeepSeek-V3   # 意图判别/闲聊/自检/记忆/评估
  aux_enabled: true                          # 辅助任务是否下放廉价模型
  aux_model_name: deepseek-ai/DeepSeek-V3    # 召回扩展/过滤（计划拆解走主模型）
  base_url: https://api.siliconflow.cn/v1
```

> **新手最容易卡的一步**：`Pro/zai-org/GLM-5.1` 是特定模型，如果你的账号没开通它，请把 `model_name` 改成 `deepseek-ai/DeepSeek-V3`（硅基流动免费额度即可用）。改完**重启后端**生效。
>
> **想换平台**（DeepSeek 官方、OpenAI、本地 vLLM 等）：改 `conf/app_config.yaml` 的 `model_name` + `base_url` 即可，只要兼容 OpenAI 协议。
>
> **可选：开启 API Key 鉴权**。在 `.env` 里设置 `API_KEY=你的自定义口令` 后，`/api/query` 会校验请求头 `X-API-Key`；不设置时跳过鉴权（本地开发模式）。`/health`、`/docs`、`/openapi.json` 和 `OPTIONS` 预检请求始终放行。

### 步骤 5：下载 Embedding / 重排模型

智数用**本地向量模型**把中文转成向量。模型较大、不随仓库分发，需要下载到 Docker 挂载目录：

```bash
# 词向量模型（约 1.3GB，用于召回）
uv run hf download BAAI/bge-large-zh-v1.5 --local-dir docker/embedding/bge-large-zh-v1.5

# 语义重排模型（约 1.1GB，用于精排，可选但推荐）
uv run hf download BAAI/bge-reranker-base --local-dir docker/embedding/bge-reranker-base
```

> **国内网络注意**：HuggingFace 下载慢/失败时，加镜像环境变量重跑：
> ```bash
> HF_ENDPOINT=https://hf-mirror.com uv run hf download BAAI/bge-large-zh-v1.5 --local-dir docker/embedding/bge-large-zh-v1.5
> ```
> **在做什么**：把模型放到 `docker/embedding/` 下，容器启动时会挂载它们。
> **验证成功**：两个目录里出现 `config.json`、模型权重文件等。

### 步骤 6：启动 Docker 基础服务

```bash
docker compose -f docker/docker-compose.yaml up -d
docker compose -f docker/docker-compose.yaml ps
```

会启动 **6 个容器**：

| 容器 | 端口 | 作用 |
| ---- | ---- | ---- |
| `mysql` | 3306 | `meta`（元数据）+ `dw`（教学数仓） |
| `elasticsearch` | 9200 | 字段取值全文检索（内置 IK 中文分词） |
| `kibana` | 5601 | ES 可视化（可选，查看数据用） |
| `qdrant` | 6333（HTTP）/ 6334（gRPC） | 字段/指标/知识向量库 |
| `embedding` | 8081 | TEI 跑 `bge-large-zh-v1.5` |
| `rerank` | 8082 | TEI 跑 `bge-reranker-base` |

> **在做什么**：一次性拉起所有基础依赖。
> **验证成功**：`ps` 里 6 个容器都显示 `Up`。
> **首次运行**：MySQL 容器会自动执行 `docker/mysql/*.sql`，建好 `meta`、`dw` 两个库及样例数据。首次要拉镜像，耐心等。
> **国内网络**：拉 Docker 镜像慢时，可配置 `/etc/docker/daemon.json` 的 `registry-mirrors` 加速。
> **常见报错**：Embedding/Rerank 首次要加载模型，可能反复重启几十秒，等 1~2 分钟；用 `curl http://localhost:8081/health` 返回 `ok` 再继续。

### 步骤 7：构建元数据知识库

```bash
uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml
```

> **在做什么**：把教学数仓的表/字段/指标写入 MySQL，并生成向量写入 Qdrant、取值写入 ES。脚本是**幂等**的，改了 `conf/meta_config.yaml` 后可以重跑。
> **验证成功**：日志里能看到写入的表/字段/指标数量，没有异常。
> **常见报错**：报连接拒绝 → Docker 服务还没就绪（尤其 Embedding 还在加载模型），等 1~2 分钟再跑。

### 步骤 8：启动后端

```bash
uv run fastapi dev main.py --port 8000
```

> **注意**：必须在**项目根目录**运行，否则可能报缺 `fastapi[standard]`。
> **验证成功**：看到 `Uvicorn running on http://127.0.0.1:8000`。另开一个终端：
> ```bash
> curl http://127.0.0.1:8000/health
> ```
> 期望返回 `{"status":"healthy","services":{...}}`，`services` 里 5 项探测分别为 `meta_mysql`、`dw_mysql`、`qdrant`、`elasticsearch`、`embedding`，全为 `true` 即健康。

问数接口：

```text
POST http://127.0.0.1:8000/api/query
```

请求体（`session_id` 用于多轮会话共享历史，`user_id` 用于跨会话用户偏好记忆，都可选；前端目前只传 `session_id`，要跨会话用 `user_id` 需自行带上）：

```json
{
  "query": "统计华北地区的销售总额",
  "session_id": "demo-session",
  "user_id": "demo-user"
}
```

先用命令行验证（能看到一行行 `data: {...}` 的 SSE 推送即成功）：

```bash
curl -N -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"query":"统计华北地区的销售总额"}'
```

> 如果 `.env` 里设了 `API_KEY`，记得加请求头：`-H "X-API-Key: 你的口令"`。

### 步骤 9：启动前端（可选）

```bash
cd frontend
pnpm install
pnpm dev
```

> **验证成功**：看到 `Local: http://localhost:5173/`，浏览器打开 http://localhost:5173，点一个样例问题试试。
> 前端通过 Vite 代理把 `/api` 转发到后端，默认 `http://127.0.0.1:8000`。如需修改：
> ```bash
> cd frontend
> cp .env.example .env
> # 编辑 .env：VITE_DEV_PROXY_TARGET=http://127.0.0.1:8000
> ```
> 改完重启 `pnpm dev`。

### 跑通了，接下来可以试这些

| 问题 | 期待看到 |
| ---- | -------- |
| `统计华北地区的销售总额` | 流程图自上而下点亮 → 结果表格 |
| `华东地区订单数量是多少` | 数据结果 |
| `你好，你是谁` | 闲聊回复（不带流程图） |
| 同一问题再问一次 | `knowledge` 路由直接复用沉淀答案（秒回） |
| `结合数仓数据和产品文档，分析华东销售下滑原因` | 三段式综合（需准备文档索引，否则文档侧降级） |

## 配置说明

### `.env`：密钥与文档引擎超参

`.env` 从 `.env.example` 复制而来。**改动 `.env` 需要重启后端**。

智数主链路只读 `LLM_API_KEY`；文档问答引擎（`app/rag_engine/`）从同一份 `.env` 读取自己的模型与检索配置：

| 变量 | 作用 |
| ---- | ---- |
| `LLM_API_KEY` | 智数主链路大模型 Key（意图判别、闲聊、SQL 生成、综合） |
| `SILICONFLOW_API_KEY` | 文档引擎的 embedding / rerank / VLM 调用 Key |
| `EMBEDDING_BINDING_HOST` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | 文档引擎向量模型（默认 `BAAI/bge-m3`，维度 1024） |
| `RERANK_BINDING_HOST` / `RERANK_MODEL` | 文档引擎重排模型（默认 `BAAI/bge-reranker-v2-m3`） |
| `LLM_BINDING_HOST` / `LLM_MODEL` / `LLM_MODEL_VISUAL` | 文档引擎的问答模型与视觉模型 |
| `INDEX_DIR` | 文档索引目录（默认 `./data/doc_index`） |
| `RERANK_MIN_SCORE` | 文档重排分门阈值（默认 0.30） |
| `REQUIRE_CITATION` | 是否强制可溯源（`1` 开启，默认开） |
| `API_KEY` | 后端 API 鉴权口令（为空则跳过鉴权） |

> 文档引擎与智数主链路的模型**相互独立**：主链路用本地 TEI 的 bge 模型，文档引擎走硅基流动的云端模型。

### `conf/app_config.yaml`：运行配置与开关

| 段 | 关键字段 | 默认值 / 说明 |
| -- | -------- | ------------- |
| `db_meta` / `db_dw` | host/port/user/password/database | 本地 `didilili` / `dili123`，库 `meta` / `dw` |
| `qdrant` | host/port/embedding_size | `localhost:6333`，向量维度 1024 |
| `embedding` | host/port/model | `localhost:8081`，`BAAI/bge-large-zh-v1.5` |
| `rerank` | host/port/model/recall_limit/top_k/max_batch | `localhost:8082`，先召回 30、精排取 10 |
| `es` | host/port/index_name | `localhost:9200`（字段取值索引名在代码里硬编码为 `value_index`，配置里的 `index_name` 不生效） |
| `llm` | model_name/chat_model_name/aux_* | 见快速开始第 4 步 |
| `hybrid` | v2_enabled | 默认 `false`（走 v1） |
| `doc_engine` | enabled/timeout | 默认 `true` / 180 秒 |
| `knowledge` | enabled/deposit_enabled/recall_score_threshold/dedup_score_threshold | 默认 `true` / `true` / 0.78 / 0.80 |
| `trace` | enabled | 默认 `true` |
| `eval` | enabled/pass_threshold/emit_note/llm_weight | 默认 `false` / 0.6 / `true` / 0.7 |
| `memory` | enabled/sqlite_path/summary_trigger_messages/user_memory_* | 默认 `true`，路径 `data/memory/checkpoints.db`，阈值 12 |
| `orchestrator` | enabled/max_tasks | 默认 `false` / 3 |
| `mcp` | enabled/timeout/max_tool_rounds/servers | 默认 `true` / 20 秒 / 3，内置 `builtin` server |
| `api` | api_key | 默认空（跳过鉴权），来自 `.env` 的 `API_KEY` |

### `.prompt` 即时生效 vs `.env`/`conf` 需重启

- `prompts/*.prompt`：**每次请求现读**，改完立即生效。但注意 `.prompt` 会被原样喂给模型，**不要加注释**；PromptTemplate 把单个 `{var}` 当占位符，JSON 示例里的花括号必须转义成 `{{...}}`，否则会报 "missing variables"。
- `.env` / `conf/*.yaml`：启动时读取一次，**改动后需要重启后端**。
- `.py` 代码：`fastapi dev` 自带热重载，改完自动生效。

## SSE 事件类型

`/api/query` 用 SSE 不断推送 JSON 事件。前端 `frontend/src/lib/agentApi.ts` 按 `type` 字段解析（类型定义在 `frontend/src/types/agent.ts`）。

| 类型 | 含义 | 前端表现 |
| ---- | ---- | -------- |
| `progress` | 节点执行进度（`step` + `running/success/error`） | 点亮/变红流程图上的某一步 |
| `result` | SQL 查询结果行数据 | 渲染结果表格 |
| `message` | 纯文本终态：闲聊、知识复用、doc 降级提示 | 只显示文字气泡，隐藏流程图 |
| `doc` | 文档问答答案 + `cited_pages` 引用页码 + `rounds` | 渲染答案与引用页码 |
| `hybrid` | 跨源综合三段式最终文本 | 渲染分块文本 |
| `delta` | 逐字流式增量（`content` + `reset`） | 打字机效果，`reset=true` 表示从头覆盖占位 |
| `note` | 结果自检 / 评估提醒 | 结果旁的提醒条，**不覆盖**答案 |
| `error` | 全局异常消息 | 显示错误 |

> 注意：入口 `classify_route` 和闲聊 `answer_general` **刻意不发 `progress`**，所以闲聊不会出现流程图。

## 项目结构导览

```text
zhishu-agent/
├── app/
│   ├── agent/                    # 智能体核心：图、状态、上下文、节点
│   │   ├── graph.py              # 主图：classify_route 五类路由 + SQL 链 + 收口
│   │   ├── sql_subgraph.py       # 独立编译的 SQL 子图（13 节点，主图/hybrid/编排复用）
│   │   ├── state.py              # DataAgentState：图里流动的业务数据
│   │   ├── context.py            # DataAgentContext：外部依赖（仓储/客户端）
│   │   ├── llm.py                # 任务分级：llm / chat_llm / aux_llm
│   │   ├── history.py            # 会话历史 + 长期记忆渲染进提示词
│   │   ├── rerank.py             # 召回候选的统一 cross-encoder 精排
│   │   ├── tool_loop.py          # 有界 MCP 工具调用循环
│   │   ├── nodes/                # 各节点：classify_route / recall / filter / sql_chain / doc_query / hybrid_* / synthesize / verify_result / orchestrator
│   │   ├── agents/               # 子 Agent：sql / doc / chat / knowledge + AgentResult 契约
│   │   ├── judges/               # 可插拔评估判定器：LLMJudge + RuleJudge
│   │   └── memory/               # 四类记忆：working（SQLite checkpointer）+ manager（摘要/用户）
│   ├── rag_engine/               # 进程内多模态文档问答引擎（检索 + VLM + 引用页码）
│   ├── mcp_servers/              # 内置本地 MCP 工具服务（时间/计算器/单位换算）
│   ├── api/                      # FastAPI：lifespan 生命周期、路由、依赖注入、请求体
│   ├── clients/                  # 各外部服务客户端管理器（MySQL/Qdrant/ES/Embedding/Rerank/文档引擎/MCP）
│   ├── conf/                     # 配置 dataclass 与加载逻辑（读取 conf/*.yaml + .env）
│   ├── core/                     # 日志、指标、口径守卫（text_utils）、request_id
│   ├── entities/                 # 业务实体（ColumnInfo / MetricInfo / ValueInfo / QueryTrace 等）
│   ├── models/                   # SQLAlchemy ORM 模型
│   ├── prompt/                   # Prompt 模板加载器
│   ├── repositories/             # 数据访问层：mysql/{meta,dw}、qdrant、es
│   ├── scripts/                  # build_meta_knowledge：元数据知识库构建入口
│   └── services/                 # query_service（SSE 编排）+ knowledge/evaluation/meta_knowledge 服务
├── conf/                         # app_config.yaml（运行配置）、meta_config.yaml（数仓元数据定义）
├── data/                         # 运行时数据（不入 git）：doc_index/ 文档索引、memory/ 工作记忆、vlm_cache/
├── docker/                       # docker-compose.yaml、mysql/*.sql、elasticsearch/、embedding/ 模型挂载
├── docs/images/                  # README 配图（首页截图、查询结果、架构图）
├── examples/                     # qdrant_quickstart_demo.py：Qdrant 最小示例
├── frontend/                     # React + Vite + Tailwind 前端
│   └── src/
│       ├── App.tsx               # 页面级 session_id、消息流
│       ├── lib/agentApi.ts       # fetch + ReadableStream 解析 SSE
│       └── types/agent.ts        # SSE 事件与消息类型
├── prompts/                      # 各环节 Prompt 模板（.prompt）
├── tests/                        # 纯逻辑单测（路由判别/口径守卫/记忆/评估/RAG 门等）
├── main.py                       # FastAPI 入口 + 中间件（CORS/API Key/request_id）
├── pyproject.toml                # 依赖、ruff、pytest 配置
└── AGENTS.md                     # 面向 AI 编码助手的架构速查
```

## 代码阅读学习路径

如果你是新手上手，建议按下面的顺序读，每一步都能独立看懂一小块：

1. **`main.py`**：应用入口。看三个中间件（CORS、API Key 鉴权、request_id）和路由注册，建立「请求怎么进来」的概念。
2. **`app/api/lifespan.py`**：启动时初始化了哪些客户端、装载了文档索引、把工作记忆 checkpointer 挂到图上。
3. **`app/agent/graph.py`**：主图拓扑。看 `classify_route` 的条件路由 `_route_target`，理解五类路由怎么分流。
4. **`app/agent/state.py` + `app/agent/context.py`**：分清「业务状态」和「外部依赖」，这是读懂后续所有节点的基础。
5. **`app/agent/nodes/classify_route.py`**：路由逻辑。「本地特征词快判 + LLM 兜底 + fail-open」，以及知识召回与口径守卫。
6. **`app/agent/sql_subgraph.py`**：SQL 链路的边定义 `build_sql_chain`，先看拓扑再逐个看节点。
7. **SQL 链各节点**：`extract_keywords.py` → `recall.py` → `merge_retrieved_info.py` → `filter.py` → `add_extra_context.py` → `sql_chain.py` → `verify_result.py`。
8. **`app/repositories/`**：数据访问层。`mysql/meta` 补元数据、`mysql/dw` 跑 SQL 和 EXPLAIN、`qdrant/` 向量、`es/` 全文。
9. **`app/clients/`**：各服务的客户端管理器，理解「外部依赖怎么被初始化和注入」。
10. **`hybrid_split.py` + `synthesize.py` + `doc_query.py`**：理解并行扇出和双分支完成栅栏，以及文档降级策略。
11. **`app/rag_engine/`**：文档问答引擎本体。重点看 `app/rag_engine/agent/prompts.py` 的 `TRACEABILITY_RULES`、`app/rag_engine/agent/agent.py` 的 `_rerank_gate`、`app/clients/doc_engine_client_manager.py` 的可溯源强制门。
12. **`app/services/query_service.py`**：一次问答的最终编排。看 `stream_mode=["custom","values"]` 怎么同时收事件和终态，以及知识沉淀、轨迹、记忆、评估的位置。
13. **进阶（可选）**：`app/agent/memory/`、`app/agent/judges/`、`app/agent/nodes/orchestrator.py`、`app/agent/agents/`、`app/clients/mcp_client_manager.py`、`app/agent/tool_loop.py`。
14. **配置与提示词**：`conf/app_config.yaml`、`conf/meta_config.yaml`、`prompts/*.prompt`。

## 常见问题 FAQ

**Q1：`uv` 或 `pnpm` 提示 command not found。**
安装后需重开终端，或用绝对路径。uv 默认装到 `~/.local/bin`，可执行 `export PATH="$HOME/.local/bin:$PATH"`。

**Q2：跑 SQL 时报「召回字段信息 failed」，后端日志有 embedding 连接错误。**
`docker/embedding` 模型没下载或 TEI 未就绪。确认第 5 步下载完成、第 6 步容器 `Up`，并等 TEI 加载完模型（首次 1~2 分钟）。可用 `curl http://localhost:8081/health` 确认。

**Q3：`fastapi dev main.py` 报缺模块 / ModuleNotFoundError。**
多半是没在**项目根目录**运行，或依赖没装全。回到项目根目录重新 `uv sync`。

**Q4：问数据类问题却返回「文档问答引擎暂不可用」或「未找到足够依据」。**
这是 `doc` 路由的降级提示——SQL 查询本身是好的。`doc`/`hybrid` 需要 `data/doc_index` 文档索引与有效的文档引擎 Key；且引擎遵循「可溯源」强约束，重排相关度过低时会主动拒答。纯数据问题请走 `sql` 路由，或确认提问不含「文档/手册」等词。想让文档链彻底不加载，可把 `doc_engine.enabled` 设为 `false`。

**Q5：MySQL 连不上 / 密码错误。**
本地库账号默认 `didilili`/`dili123`（见 `conf/app_config.yaml`）。若之前跑过旧容器，数据可能残留，可 `docker compose -f docker/docker-compose.yaml down -v` 清空重建（会清掉已建知识库，需重跑第 7 步）。

**Q6：想让 SQL 更稳 / 想换更聪明的模型。**
改 `conf/app_config.yaml` 的 `model_name`；辅助任务（召回扩展/过滤）由 `aux_model_name` 控制，可单独换便宜模型；跨源/编排的计划拆解走主模型 `model_name`。改完重启后端。多轮追问会自动带上下文，无需额外配置。

**Q7：会话记忆存哪里？重启会丢吗？**
工作记忆（会话消息）持久化在 `data/memory/checkpoints.db`（SQLite），**重启不丢**；摘要与用户画像记忆落 MySQL `agent_memory` 表。想清空所有会话历史，删掉这个 db 文件即可（`data/memory/` 整个目录不入 git）。

**Q8：为什么单次问数只调 1 次主模型？**
任务分级：只有 `generate_sql`/`correct_sql`、跨源综合以及跨源/编排的计划拆解用主模型，其余（召回关键词扩展、表/指标过滤）走廉价 `aux_model_name`，并带 fail-open 兜底。设 `aux_enabled: false` 可让辅助任务回退主模型做对比。

**Q9：请求返回 401「无效的 API Key」。**
说明 `.env` 里设置了 `API_KEY`，但请求没带或带错了 `X-API-Key` 请求头。本地开发可把 `API_KEY` 留空以跳过鉴权。

**Q10：前端不显示流程图。**
可能是走了 `chat`/`knowledge` 路由（这两条刻意不发 `progress`），或提问被判定为闲聊。换一个明确的数据类问题试试。

**Q11：提问后一直转圈 / 文档问题很慢。**
文档引擎含多轮校验和视觉调用，单次可能几十秒（`doc_engine.timeout` 默认 180 秒）。SQL 链路慢通常是在等模型或 Embedding 服务响应，可看后端日志定位卡在哪个节点。

**Q12：想体验元数据「混合检索」的检索层，有单独示例吗？**
有。`examples/qdrant_quickstart_demo.py` 演示了 Qdrant 最小化的集合创建、写入向量、相似度查询，需要 Qdrant 容器已启动。

## 能力边界与可扩展方向

`智数` 主要面向**学习与实战演练**，刻意不覆盖生产治理。以下能力目前**没有实现**：

- 用户登录、角色权限、数据权限控制、多租户隔离；
- SQL 安全审计、执行白名单、查询限流与缓存；
- 监控告警、分布式链路追踪平台、灰度发布；
- 更复杂的追问改写与主动澄清；
- 离线评测集、自动化回归、评测看板（在线 LLM-as-judge 已有，但离线体系仍待补齐）。

适合继续扩展的点：

- **接入更多数据源**：把 SQL 执行扩展到达梦、StarRocks、PostgreSQL 等，只需在 `app/repositories/mysql/dw/` 旁新增仓储实现。
- **扩展 MCP 工具**：在 `conf/app_config.yaml` 的 `mcp.servers` 下按注释示例添加 Tavily 搜索 / OpenWeather 天气，并用 `agents` 指定归属。
- **替换或本地化模型**：主链路模型、文档引擎模型都可换；也可把 TEI 换成自建推理服务。
- **构建自己的文档索引**：仓库提供引擎源码与 `IndexStore` 装载器（读取 `data/doc_index` 下的 JSON 索引文件），但索引本身不随 git 分发，需要你按自己的语料准备。
- **开多智能体编排**：把 `orchestrator.enabled` 设为 `true`，对比「主 Agent 计划分发」与「固定路由」的差异。
- **做局部评测**：以 `query_trace` 表为数据源，离线跑一批问题，统计准确率、修正次数、耗时等。

> 本项目基于尚硅谷「大模型智能体掌柜问数」项目，并在此基础上整理、重构、扩展而来。
