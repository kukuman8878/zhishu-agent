"""
召回信息合并节点

负责把字段 字段取值和指标三路召回结果聚合成统一上下文
这一层会补齐指标依赖字段 字段真实取值 主外键字段和表信息
后续过滤节点不再关心信息来自哪个检索分支，只处理合并后的表上下文和指标上下文
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import (
    ColumnInfoState,
    DataAgentState,
    MetricInfoState,
    TableInfoState,
)
from app.core.log import logger
from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo


# 召回合并节点：按字段/表组织三路召回，补齐指标依赖字段、取值样例与主外键，产出表与指标上下文；参数 state=含三路召回结果，runtime=提供 Meta 仓储补齐元数据
async def merge_retrieved_info(
    state: DataAgentState, runtime: Runtime[DataAgentContext]
):
    """合并召回结果，并输出 SQL 生成前的候选表信息和指标信息"""

    writer = runtime.stream_writer
    step = "合并召回信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        retrieved_column_infos: list[ColumnInfo] = state["retrieved_column_infos"]
        retrieved_metric_infos: list[MetricInfo] = state["retrieved_metric_infos"]
        retrieved_value_infos: list[ValueInfo] = state["retrieved_value_infos"]

        meta_mysql_repository = runtime.context["meta_mysql_repository"]

        # 本节点的主线是：
        # 字段召回 + 指标依赖字段 + 字段真实取值 + 主外键补齐
        # -> table_infos / metric_infos，交给后续过滤和 SQL 生成节点继续使用。

        # 1. 以 column_id 为 key 合并字段信息
        retrieved_column_infos_map: dict[str, ColumnInfo] = {
            retrieved_column_info.id: retrieved_column_info
            for retrieved_column_info in retrieved_column_infos
        }

        # 2. 批量补齐指标依赖字段（解决 N+1 查询）
        needed_column_ids: set[str] = set()
        for retrieved_metric_info in retrieved_metric_infos:
            for relevant_column in retrieved_metric_info.relevant_columns:
                if relevant_column not in retrieved_column_infos_map:
                    needed_column_ids.add(relevant_column)

        # 批量查出所有缺失的指标依赖字段，一次性补齐
        if needed_column_ids:
            batch_columns = await meta_mysql_repository.get_column_infos_by_ids(
                list(needed_column_ids)
            )
            for col_id, col_info in batch_columns.items():
                if col_info is not None:
                    retrieved_column_infos_map[col_id] = col_info

        # 3. 把字段取值合并回字段 examples（同时收集缺失的 column_id 做批量补齐）
        value_needed_ids: set[str] = set()
        for retrieved_value_info in retrieved_value_infos:
            column_id = retrieved_value_info.column_id
            if column_id not in retrieved_column_infos_map:
                value_needed_ids.add(column_id)

        if value_needed_ids:
            batch_value_columns = await meta_mysql_repository.get_column_infos_by_ids(
                list(value_needed_ids)
            )
            for col_id, col_info in batch_value_columns.items():
                if col_info is not None:
                    retrieved_column_infos_map[col_id] = col_info

        for retrieved_value_info in retrieved_value_infos:
            value = retrieved_value_info.value
            column_id = retrieved_value_info.column_id
            if (
                column_id in retrieved_column_infos_map
                and value not in retrieved_column_infos_map[column_id].examples
            ):
                retrieved_column_infos_map[column_id].examples.append(value)

        # 4. 按表组织字段上下文
        table_to_columns_map: dict[str, list[ColumnInfo]] = {}
        for column_info in retrieved_column_infos_map.values():
            table_id = column_info.table_id
            if table_id not in table_to_columns_map:
                table_to_columns_map[table_id] = []
            table_to_columns_map[table_id].append(column_info)

        # 5. 补齐主外键字段（每张表一次查询，已是批量粒度）
        for table_id in table_to_columns_map.keys():
            key_columns: list[
                ColumnInfo
            ] = await meta_mysql_repository.get_key_columns_by_table_id(table_id)
            column_ids = [
                column_info.id for column_info in table_to_columns_map[table_id]
            ]
            for key_column in key_columns:
                if key_column.id not in column_ids:
                    table_to_columns_map[table_id].append(key_column)

        # 6. 批量查出所有候选表的元数据，一次性补齐
        table_ids = list(table_to_columns_map.keys())
        batch_tables = await meta_mysql_repository.get_table_infos_by_ids(table_ids)

        # 7. 生成表结构上下文
        table_infos: list[TableInfoState] = []
        for table_id, column_infos in table_to_columns_map.items():
            table_info = batch_tables.get(table_id)
            if table_info is None:
                continue
            columns = [
                ColumnInfoState(
                    name=column_info.name,
                    type=column_info.type,
                    role=column_info.role,
                    examples=column_info.examples,
                    description=column_info.description,
                    alias=column_info.alias,
                )
                for column_info in column_infos
            ]
            table_info_state = TableInfoState(
                name=table_info.name,
                role=table_info.role,
                description=table_info.description,
                columns=columns,
            )
            table_infos.append(table_info_state)

        # 8. 生成指标上下文
        metric_infos: list[MetricInfoState] = [
            MetricInfoState(
                name=retrieved_metric_info.name,
                description=retrieved_metric_info.description,
                relevant_columns=retrieved_metric_info.relevant_columns,
                alias=retrieved_metric_info.alias,
            )
            for retrieved_metric_info in retrieved_metric_infos
        ]

        logger.info(
            f"合并后的表信息：{[table_info['name'] for table_info in table_infos]}"
        )
        logger.info(
            f"合并后的指标信息：{[metric_info['name'] for metric_info in metric_infos]}"
        )

        writer({"type": "progress", "step": step, "status": "success"})
        return {
            "table_infos": table_infos,
            "metric_infos": metric_infos,
        }
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
