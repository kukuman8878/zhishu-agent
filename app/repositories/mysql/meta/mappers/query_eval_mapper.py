"""
QueryEval 映射器

负责在问答评估业务实体和 ORM 模型之间做双向转换，dimensions 以 JSON 文本落库。
"""

import json

from app.entities.query_eval import QueryEval
from app.models.query_eval import QueryEvalMySQL


class QueryEvalMapper:
    """负责 `QueryEval` 与 `QueryEvalMySQL` 之间的双向转换"""

    # 把评估 ORM 模型还原为业务实体；参数 model=评估 ORM 模型对象
    @staticmethod
    def to_entity(model: QueryEvalMySQL) -> QueryEval:
        """ORM 模型 → 业务实体"""
        dimensions = {}
        if model.dimensions:
            try:
                dimensions = json.loads(model.dimensions)
            except Exception:
                dimensions = {}
        return QueryEval(
            id=model.id,
            session_id=model.session_id,
            query=model.query or "",
            route=model.route or "",
            judge=model.judge or "",
            score=model.score,
            passed=model.passed,
            dimensions=dimensions,
            reason=model.reason or "",
            answer=model.answer,
            duration_ms=model.duration_ms,
        )

    # 把评估业务实体转成 ORM 模型；参数 query_eval=评估业务实体
    @staticmethod
    def to_model(query_eval: QueryEval) -> QueryEvalMySQL:
        """业务实体 → ORM 模型"""
        return QueryEvalMySQL(
            id=query_eval.id,
            session_id=query_eval.session_id,
            query=query_eval.query,
            route=query_eval.route,
            judge=query_eval.judge,
            score=query_eval.score,
            passed=query_eval.passed,
            dimensions=json.dumps(query_eval.dimensions, ensure_ascii=False),
            reason=query_eval.reason,
            answer=query_eval.answer,
            duration_ms=query_eval.duration_ms,
        )
