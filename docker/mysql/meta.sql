SET NAMES utf8mb4;
CREATE DATABASE meta DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON meta.* TO 'didilili'@'%';

USE meta;

DROP TABLE IF EXISTS table_info;
CREATE TABLE table_info
(
    id          VARCHAR(64) PRIMARY KEY COMMENT '表编号',
    name        VARCHAR(128) COMMENT '表名称',
    role        VARCHAR(32) COMMENT '表类型(fact/dim)',
    description TEXT COMMENT '表描述'
);



DROP TABLE IF EXISTS column_info;
CREATE TABLE column_info
(
    id          VARCHAR(64) PRIMARY KEY COMMENT '列编号',
    name        VARCHAR(128) COMMENT '列名称',
    type        VARCHAR(64) COMMENT '数据类型',
    role        VARCHAR(32) COMMENT '列类型(primary_key,foreign_key,measure,dimension)',
    examples    JSON COMMENT '数据示例',
    description TEXT COMMENT '列描述',
    alias       JSON COMMENT '列别名',
    table_id    VARCHAR(64) COMMENT '所属表编号'
);

DROP TABLE IF EXISTS metric_info;
CREATE TABLE metric_info
(
    id               VARCHAR(64) PRIMARY KEY COMMENT '指标编码',
    name             VARCHAR(128) COMMENT '指标名称',
    description      TEXT COMMENT '指标描述',
    relevant_columns JSON COMMENT '关联的列',
    alias            JSON COMMENT '指标别名'
);


DROP TABLE IF EXISTS column_metric;
CREATE TABLE column_metric
(
    column_id VARCHAR(64) COMMENT '列编号',
    metric_id VARCHAR(64) COMMENT '指标编号',
    PRIMARY KEY (column_id, metric_id)
);


DROP TABLE IF EXISTS knowledge_item;
CREATE TABLE knowledge_item
(
    id         VARCHAR(64) PRIMARY KEY COMMENT '知识条目编号',
    question   TEXT COMMENT '用户原始问题',
    answer     MEDIUMTEXT COMMENT '沉淀答案(结果/综合/文档答案)',
    `sql`      MEDIUMTEXT COMMENT '生成SQL(仅sql路由,供后续参考复用)',
    route      VARCHAR(16) COMMENT '来源路由(sql/doc/hybrid)',
    hit_count  INT NOT NULL DEFAULT 0 COMMENT '知识复用命中次数',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
);


DROP TABLE IF EXISTS query_trace;
CREATE TABLE query_trace
(
    id                VARCHAR(64) PRIMARY KEY COMMENT '轨迹编号',
    session_id        VARCHAR(128) COMMENT '会话标识(多轮追问共享)',
    query             TEXT COMMENT '用户问题',
    route             VARCHAR(16) COMMENT '最终路由(sql/doc/hybrid/chat/knowledge)',
    knowledge_item_id VARCHAR(64) COMMENT '命中复用的知识条目id(未命中为空)',
    `sql`             MEDIUMTEXT COMMENT '最终执行的SQL(仅sql路由)',
    sql_attempts      INT NOT NULL DEFAULT 0 COMMENT 'SQL修正次数',
    row_count         INT COMMENT '返回行数(仅sql路由)',
    duration_ms       INT COMMENT '整体耗时(毫秒)',
    status            VARCHAR(16) COMMENT '执行状态(success/error)',
    error             TEXT COMMENT '失败原因(成功为空)',
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
);


DROP TABLE IF EXISTS query_eval;
CREATE TABLE query_eval
(
    id          VARCHAR(64) PRIMARY KEY COMMENT '评估编号',
    session_id  VARCHAR(128) COMMENT '会话标识(多轮追问共享)',
    query       TEXT COMMENT '用户问题',
    route       VARCHAR(16) COMMENT '最终路由',
    judge       VARCHAR(64) COMMENT '判定器名称(如 llm+rule)',
    score       FLOAT COMMENT '综合分0~1',
    passed      TINYINT(1) COMMENT '是否通过',
    dimensions  TEXT COMMENT '各维度得分JSON',
    reason      TEXT COMMENT '判定理由',
    answer      TEXT COMMENT '被评估答案快照',
    duration_ms INT COMMENT '评估耗时(毫秒)',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
);


DROP TABLE IF EXISTS agent_memory;
CREATE TABLE agent_memory
(
    id          VARCHAR(64) PRIMARY KEY COMMENT '记忆编号',
    memory_type VARCHAR(16) NOT NULL COMMENT '记忆类型(summary/user)',
    scope_id    VARCHAR(128) NOT NULL COMMENT '作用域(session_id/user_id)',
    content     TEXT COMMENT '记忆内容(摘要正文/用户偏好事实)',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_memory_type_scope (memory_type, scope_id)
);
