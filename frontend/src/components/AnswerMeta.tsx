/**
 * 答案元信息组件
 * 为不同回答模式（数据查询/文档问答/跨源综合/通用回复）打上统一识别徽标，
 * 文档问答附带引用页码。让四种答案形态在对话流里一眼可辨。
 */
import { Database, FileText, Layers, MessageSquareText } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { cn } from "../lib/format";
import type { AnswerMode } from "../types/agent";

type ModeMeta = {
  label: string;
  icon: LucideIcon;
  className: string;
};

const MODE_META: Record<AnswerMode, ModeMeta> = {
  sql: {
    label: "数据查询",
    icon: Database,
    className: "border-moss/30 bg-moss/10 text-moss",
  },
  doc: {
    label: "文档问答",
    icon: FileText,
    className: "border-brass/35 bg-brass/10 text-brass",
  },
  hybrid: {
    label: "跨源综合",
    icon: Layers,
    className: "border-ink/20 bg-ink/5 text-ink",
  },
  chat: {
    label: "通用回复",
    icon: MessageSquareText,
    className: "border-ink/15 bg-ink/5 text-ink/60",
  },
};

export function AnswerMeta({
  mode,
  citedPages = [],
}: {
  mode: AnswerMode;
  citedPages?: string[];
}) {
  const meta = MODE_META[mode] ?? MODE_META.chat;
  const Icon = meta.icon;

  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5">
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
          meta.className,
        )}
      >
        <Icon className="h-3 w-3" aria-hidden="true" />
        {meta.label}
      </span>

      {mode === "doc" && citedPages.length > 0 && (
        <>
          <span className="text-xs text-ink/45">引用自文档</span>
          {citedPages.map((page) => (
            <span
              key={page}
              title={`答案证据来源：文档第 ${page} 页`}
              className="rounded-full border border-moss/30 bg-moss/10 px-2 py-0.5 text-xs text-moss"
            >
              第 {page} 页
            </span>
          ))}
        </>
      )}
    </div>
  );
}
