"""
表/指标信息过滤节点

合并后的候选表结构和候选指标会在这一步做裁剪——让大模型只"选择"哪些保留，
真正的结构裁剪仍由程序完成，避免模型重写复杂结构出错。两个节点关系紧密，
都走"候选转 YAML → LLM 选择 → 按选择裁剪"的同一套流程，故聚到一个文件：
  filter_table   裁剪候选表及表内字段（表名 → 字段名列表）
  filter_metric  裁剪候选指标（指标名称数组）
"""

import yaml
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.llm_utils import retry_async
from app.agent.state import DataAgentState, MetricInfoState, TableInfoState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


# 表信息过滤节点：候选表转 YAML 交 LLM 选择(表→字段)，再按结果裁剪表结构上下文；参数 state=含 query 与候选 table_infos
async def filter_table(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """根据用户问题裁剪候选表结构上下文"""

    writer = runtime.stream_writer
    step = "过滤表信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]
        table_infos: list[TableInfoState] = state["table_infos"]

        # table_infos 是嵌套结构，转成 YAML 后更适合放进提示词，也保留中文字段说明
        prompt = PromptTemplate(
            template=load_prompt("filter_table_info"),
            input_variables=["query", "table_infos"],
        )
        # filter_table_info prompt 要求模型只输出 JSON 对象：表名 -> 字段名列表
        output_parser = JsonOutputParser()
        # LCEL 管道：填充提示词 -> 调用模型 -> 解析 JSON
        chain = prompt | llm | output_parser

        result = await retry_async(
            chain.ainvoke,
            {
                "query": query,
                "table_infos": yaml.dump(
                    table_infos, allow_unicode=True, sort_keys=False
                ),
            },
            node_name="filter_table",
        )
        # 模型只负责选择，程序根据选择结果从原始 TableInfoState 中裁剪，避免模型重写复杂结构出错
        filtered_table_infos: list[TableInfoState] = []
        for table_info in table_infos:
            if table_info["name"] in result:
                table_info["columns"] = [
                    column_info
                    for column_info in table_info["columns"]
                    if column_info["name"] in result[table_info["name"]]
                ]
                filtered_table_infos.append(table_info)

        logger.info(
            f"过滤后的表信息：{[filtered_table_info['name'] for filtered_table_info in filtered_table_infos]}"
        )
        writer({"type": "progress", "step": step, "status": "success"})
        return {"table_infos": filtered_table_infos}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


# 指标过滤节点：把候选指标转 YAML 交 LLM 选择，按返回名称裁剪出问题真正需要的指标；参数 state=含 query 与候选 metric_infos
async def filter_metric(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """根据用户问题裁剪候选指标上下文"""

    writer = runtime.stream_writer
    step = "过滤指标信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]
        metric_infos: list[MetricInfoState] = state["metric_infos"]

        # metric_infos 转成 YAML 后作为候选项交给模型，模型只需要返回被选中的指标名称
        prompt = PromptTemplate(
            template=load_prompt("filter_metric_info"),
            input_variables=["query", "metric_infos"],
        )
        # filter_metric_info prompt 要求模型只输出 JSON 数组
        output_parser = JsonOutputParser()
        # LCEL 管道：填充提示词 -> 调用模型 -> 解析 JSON
        chain = prompt | llm | output_parser

        result = await retry_async(
            chain.ainvoke,
            {
                "query": query,
                "metric_infos": yaml.dump(
                    metric_infos, allow_unicode=True, sort_keys=False
                ),
            },
            node_name="filter_metric",
        )
        # 用模型返回的指标名称过滤原始结构，保留描述 依赖字段 别名等完整上下文
        filtered_metric_infos = [
            metric_info for metric_info in metric_infos if metric_info["name"] in result
        ]

        logger.info(
            f"过滤后的指标信息：{[filtered_metric_info['name'] for filtered_metric_info in filtered_metric_infos]}"
        )

        writer({"type": "progress", "step": step, "status": "success"})
        return {"metric_infos": filtered_metric_infos}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
