/**
 * 智能体类型定义
 * 定义问数智能体前端使用的 SSE 事件、流程步骤和聊天消息类型
 */
export type ProgressStatus = "running" | "success" | "error";

export type ProgressEvent = {
  type: "progress";
  step: string;
  status: ProgressStatus;
};

export type ResultEvent = {
  type: "result";
  data: unknown;
};

// 纯文本回复事件：闲聊/域外问题等不走数据分析链路的场景，由后端直接返回一句话
export type MessageEvent = {
  type: "message";
  content: string;
};

// 结果自检提醒事件：SQL 结果可疑/为空时附在结果旁的提示，不覆盖结果本体
export type NoteEvent = {
  type: "note";
  content: string;
};

// 流式文本增量事件：打字机效果。reset=true 表示本轮内容从头开始（清掉"正在…"占位）
export type DeltaEvent = {
  type: "delta";
  content: string;
  reset: boolean;
};

// 文档问答终态事件：doc 路由由外接文档引擎回答，含引用页码
export type DocEvent = {
  type: "doc";
  data: {
    answer: string;
    cited_pages: number[];
    rounds: number;
    route?: Record<string, unknown>;
  };
};

// 跨源综合终态事件：hybrid 路由合并 SQL 结果与文档答案后的分块文本
export type HybridEvent = {
  type: "hybrid";
  data: {
    content: string;
    sections?: Array<{
      engine: "sql" | "doc";
      data: unknown;
    }>;
  };
};

export type ErrorEvent = {
  type: "error";
  message: string;
};

// 五类事件：progress=执行进度，result=查询结果表格，delta=流式文本增量，
// message=闲聊纯文本，doc=文档问答结果，hybrid=跨源综合结果，note=结果自检提醒，error=异常
export type AgentEvent =
  | ProgressEvent
  | ResultEvent
  | MessageEvent
  | DeltaEvent
  | DocEvent
  | HybridEvent
  | NoteEvent
  | ErrorEvent;

export type StepState = {
  step: string;
  status: ProgressStatus;
  updatedAt: number;
};

// 回答模式：决定气泡按表格(sql)、纯文本+引用(doc)、分块文本(hybrid)还是闲聊纯文本(chat)渲染
export type AnswerMode = "sql" | "doc" | "hybrid" | "chat";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status?: "streaming" | "done" | "error";
  steps?: StepState[];
  result?: unknown;
  error?: string;
  // 结果自检提醒：附在结果旁的提示文本（空结果/可疑结果时后端发 note 事件写入）
  note?: string;
  mode?: AnswerMode;
  citedPages?: string[];
  // 纯文本闲聊标记：为 true 时前端只展示文字气泡，不渲染 LangGraph 流程图
  plainText?: boolean;
};
