"""
SQL 生成-校验-修正-执行节点

这里把 SQL 链路上关系最紧密的四个节点聚到一个文件里，减少散碎的单一函数文件：
  generate_sql  基于已检索和过滤的表/指标/日期上下文生成候选 SQL
  validate_sql  用数仓 explain 解析候选 SQL，把错误写入 error 供条件边判断
  correct_sql   校验失败后结合完整上下文与数据库报错做最小必要修正
  run_sql       执行最终 SQL 并产出问数结果，作为 SQL 闭环的结束节点

四个节点通过 state 里的 sql/error 字段串成一条线：
  generate_sql → validate_sql →(有错) correct_sql → run_sql
                                └(无错) run_sql
"""

import yaml
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.history import render_history
from app.agent.llm import llm
from app.agent.llm_utils import retry_async
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

# SQL 执行失败后的最大修正重试次数：超过则终止并把错误返回给用户
MAX_SQL_RETRIES = 2


# SQL 生成节点：把过滤后的表/指标结构与日期、数据库环境上下文交 LLM 生成候选 SQL；参数 state=含 query/table_infos/metric_infos/date_info/db_info
async def generate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """基于已检索和过滤的上下文生成 SQL"""

    writer = runtime.stream_writer
    step = "生成SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 这些上下文都由前置节点准备完成，模型只在给定表 字段 指标口径范围内生成 SQL
        table_infos = state["table_infos"]
        metric_infos = state["metric_infos"]
        date_info = state["date_info"]
        db_info = state["db_info"]
        query = state["query"]
        # 会话历史：省略式追问（"那按月呢"）需要结合上一轮问答还原完整查询意图
        history = render_history(state.get("messages", []))

        prompt = PromptTemplate(
            template=load_prompt("generate_sql"),
            input_variables=[
                "table_infos",
                "metric_infos",
                "date_info",
                "db_info",
                "history",
                "query",
            ],
        )
        # SQL 生成节点只需要纯文本 SQL，不能要求模型输出 JSON 或 Markdown 代码块
        output_parser = StrOutputParser()
        chain = prompt | llm | output_parser

        result = await retry_async(
            chain.ainvoke,
            {
                "table_infos": yaml.dump(
                    table_infos, allow_unicode=True, sort_keys=False
                ),
                "metric_infos": yaml.dump(
                    metric_infos, allow_unicode=True, sort_keys=False
                ),
                "date_info": yaml.dump(date_info, allow_unicode=True, sort_keys=False),
                "db_info": yaml.dump(db_info, allow_unicode=True, sort_keys=False),
                "history": history,
                "query": query,
            },
            node_name="generate_sql",
        )
        logger.info(f"生成的SQL：{result}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"sql": result}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


# SQL 校验节点：用数仓 explain 解析候选 SQL，把错误信息写入 error 字段供条件边判断是否进入修正；参数 state=含待校验 sql，runtime=提供 DW 仓储执行 explain
async def validate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """校验 SQL，并返回 error 字段控制后续条件分支"""

    writer = runtime.stream_writer
    step = "校验SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 读取 generate_sql 或 correct_sql 写入状态的候选 SQL
        sql = state["sql"]

        # SQL 可用性必须交给真实数仓判断，这里从运行时上下文取 DW Repository
        dw_mysql_repository: DWMySQLRepository = runtime.context["dw_mysql_repository"]

        try:
            # validate 内部使用 explain <sql>，只关心数据库能否成功解析这条 SQL
            await dw_mysql_repository.validate(sql)
            writer({"type": "progress", "step": step, "status": "success"})
            logger.info("SQL语法正确")
            return {"error": None}
        except Exception as e:
            # 不抛出异常中断图执行，而是把错误写入状态，供条件分支进入 correct_sql
            logger.info(f"SQL语法错误：{str(e)}")
            writer({"type": "progress", "step": step, "status": "success"})
            return {"error": str(e)}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


# SQL 修正节点：结合完整上下文与数据库报错让 LLM 最小化修正 SQL 并覆盖回 sql 状态；参数 state=含原 sql/error 及生成上下文
async def correct_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """根据校验错误修正 SQL"""

    writer = runtime.stream_writer
    step = "校正SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 校正 SQL 仍然需要完整上下文，避免模型只根据报错修语法却改丢业务语义
        table_infos = state["table_infos"]
        metric_infos = state["metric_infos"]
        date_info = state["date_info"]
        db_info = state["db_info"]
        query = state["query"]

        # sql 是待修正的候选 SQL，error 是数据库 explain 返回的具体错误信息
        sql = state["sql"]
        error = state["error"]

        prompt = PromptTemplate(
            template=load_prompt("correct_sql"),
            input_variables=[
                "table_infos",
                "metric_infos",
                "date_info",
                "db_info",
                "query",
                "sql",
                "error",
            ],
        )
        # 修正后的输出仍然是一条纯 SQL 文本，用来覆盖 state["sql"]
        output_parser = StrOutputParser()
        chain = prompt | llm | output_parser

        result = await retry_async(
            chain.ainvoke,
            {
                "table_infos": yaml.dump(
                    table_infos, allow_unicode=True, sort_keys=False
                ),
                "metric_infos": yaml.dump(
                    metric_infos, allow_unicode=True, sort_keys=False
                ),
                "date_info": yaml.dump(date_info, allow_unicode=True, sort_keys=False),
                "db_info": yaml.dump(db_info, allow_unicode=True, sort_keys=False),
                "query": query,
                "sql": sql,
                "error": error,
            },
            node_name="correct_sql",
        )

        logger.info(f"校正后的SQL：{result}")
        writer({"type": "progress", "step": step, "status": "success"})
        # 每修正一次计数 +1：校验/执行路由用它与 MAX_SQL_RETRIES 比较，
        # 防止模型持续产出无效 SQL 导致无限循环
        return {"sql": result, "sql_attempts": state.get("sql_attempts", 0) + 1}
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


# SQL 执行节点：执行最终 SQL；成功则发 result 事件并写回 sql_rows，失败则写回 run_error 触发修正重试环（超过上限才抛异常）；参数 state=含待执行 sql，runtime=提供 DW 仓储执行
async def run_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """执行 SQL 并产出最终问数结果；执行失败时支持"看错误改 SQL"的自愈重试"""

    writer = runtime.stream_writer
    step = "执行SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 这里拿到的可能是 generate_sql 直接通过校验的 SQL，也可能是 correct_sql 覆盖后的 SQL
        sql = state["sql"]
        dw_mysql_repository = runtime.context["dw_mysql_repository"]

        # 真实数据库访问统一封装在仓储层，节点只负责从状态取 SQL 并触发执行
        result = await dw_mysql_repository.run(sql)
        logger.info(f"SQL执行结果：{result}")
        writer({"type": "progress", "step": step, "status": "success"})
        writer({"type": "result", "data": result})
        # 把行数据写回 state：sql 路由无下游消费（无人读取），
        # hybrid 路由（P3）由综合节点读取 sql_rows 拼接数据结论
        updates: dict = {"sql_rows": result, "run_error": None}
        # 非 hybrid 路由把结果写进会话历史（hybrid 由 synthesize 统一写入综合结论）
        if state.get("route") != "hybrid":
            updates["messages"] = [AIMessage(content=str(result))]
        return updates

    except Exception as e:
        # 自愈闭环：执行失败不直接抛给用户，而是把错误写回状态，
        # 由条件边路由到 correct_sql（模型看错误做最小修复）后重新校验再执行
        attempts = state.get("sql_attempts", 0)
        logger.warning(
            f"{step} failed（已修正 {attempts} 次）：{e}，上限={MAX_SQL_RETRIES}"
        )
        writer({"type": "progress", "step": step, "status": "error"})
        if attempts < MAX_SQL_RETRIES:
            # run_error 非空 → 条件边进入 correct_sql（修正计数由 correct_sql 递增）
            return {"run_error": str(e), "error": str(e)}
        # 修正次数耗尽才抛出，由上层包装成 SSE error 事件返回给用户
        raise
