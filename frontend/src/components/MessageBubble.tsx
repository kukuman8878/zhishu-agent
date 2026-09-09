/**
 * 聊天消息气泡组件
 * 组合展示用户问题、智能体回复、执行流程和结果表格。
 * 按 mode 决定展示形态：sql→表格，doc→文本+引用页码，hybrid→分块综合，chat→纯文本。
 */
import { Bot, Copy, UserRound } from "lucide-react";
import { AnswerMeta } from "./AnswerMeta";
import { HybridBlocks } from "./HybridBlocks";
import { ResultTable } from "./ResultTable";
import { StepRail } from "./StepRail";
import { parseHybridSections } from "../lib/hybrid";
import { cn, formatTime, toClipboardText } from "../lib/format";
import type { ChatMessage } from "../types/agent";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const mode = message.mode ?? "chat";
  const hybridSections =
    mode === "hybrid" ? parseHybridSections(message.content) : [];

  const copy = async () => {
    const text = message.result ? toClipboardText(message.result) : message.content;
    await navigator.clipboard.writeText(text);
  };

  return (
    <article className={cn("group flex gap-3", isUser && "justify-end")}>
      {!isUser && (
        <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-ink text-parchment">
          <Bot className="h-4 w-4" aria-hidden="true" />
        </div>
      )}

      <div className={cn("max-w-[920px] flex-1", isUser && "flex max-w-[760px] justify-end")}>
        <div
          className={cn(
            "relative border px-5 py-4 shadow-line",
            isUser
              ? "border-ink/80 bg-ink text-parchment"
              : "border-ink/10 bg-[#fffaf1]/78 text-ink backdrop-blur",
          )}
        >
          <div className="flex items-start justify-between gap-3">
            {/* hybrid 分块渲染；其余模式保留原文换行 */}
            {!isUser && hybridSections.length > 0 ? (
              <div className="w-full">
                <HybridBlocks sections={hybridSections} />
              </div>
            ) : (
              <p
                className={cn(
                  "min-w-0 whitespace-pre-wrap break-words text-[15px] leading-7 [overflow-wrap:anywhere]",
                  !isUser && "text-ink",
                )}
              >
                {message.content}
              </p>
            )}

            {!isUser && message.status !== "streaming" && (
              <button
                type="button"
                onClick={copy}
                className="shrink-0 rounded-full p-1.5 text-ink/45 opacity-0 outline-none transition hover:bg-ink/5 hover:text-ink focus:opacity-100 focus:ring-2 focus:ring-moss/40 group-hover:opacity-100"
                title="复制"
                aria-label="复制"
              >
                <Copy className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>

          {message.error && (
            <div className="mt-3 border border-tomato/30 bg-tomato/10 px-3 py-2 text-sm text-tomato">
              {message.error}
            </div>
          )}

          {!isUser && message.note && (
            <div className="mt-3 border border-moss/30 bg-moss/10 px-3 py-2 text-sm text-ink/80">
              {message.note}
            </div>
          )}

          {/* 仅数据分析消息展示 LangGraph 流程图；闲聊(plainText)只保留文字气泡 */}
          {!isUser && !message.plainText && <StepRail steps={message.steps} />}
          {!isUser && message.result !== undefined && <ResultTable data={message.result} />}

          {/* 答案模式徽标 + 文档引用页码 */}
          {!isUser && (
            <AnswerMeta mode={mode} citedPages={message.citedPages} />
          )}

          <div
            className={cn(
              "mt-3 text-xs",
              isUser ? "text-parchment/55" : "text-ink/45",
            )}
          >
            {formatTime(message.createdAt)}
          </div>
        </div>
      </div>

      {isUser && (
        <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-moss text-white">
          <UserRound className="h-4 w-4" aria-hidden="true" />
        </div>
      )}
    </article>
  );
}
