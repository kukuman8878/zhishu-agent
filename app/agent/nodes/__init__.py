"""
智数 Agent 节点包

每个模块对应 LangGraph 图中的一个或一组处理步骤。相关节点按主题聚合以减少散碎文件：
  sql_chain       SQL 生成-校验-修正-执行四个环节
  recall         字段/指标/取值三路召回
  filter         表/指标信息过滤
  classify_route 入口意图路由
  extract_keywords 关键词抽取
  ...            （其余独立节点）

节点之间通过 DataAgentState 传递中间状态，通过 Runtime 读取上下文和写出流式进度
"""
