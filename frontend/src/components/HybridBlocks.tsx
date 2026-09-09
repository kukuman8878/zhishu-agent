/**
 * 跨源综合结果分块展示
 * 把三段式回答（数据结论/文档说明/综合判断）按语义渲染成带色块与图标的区块，
 * 替代一坨纯文本，降低长答案的扫读成本。
 */
import { ChartNoAxesCombined, FileText, Scale, TextQuote } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { HybridSection } from "../lib/hybrid";

const SECTION_STYLE: Record<
  HybridSection["kind"],
  { icon: LucideIcon; labelClass: string; blockClass: string }
> = {
  data: {
    icon: ChartNoAxesCombined,
    labelClass: "text-moss",
    blockClass: "border-l-2 border-moss/50 bg-moss/5",
  },
  doc: {
    icon: FileText,
    labelClass: "text-brass",
    blockClass: "border-l-2 border-brass/50 bg-brass/5",
  },
  verdict: {
    icon: Scale,
    labelClass: "text-ink",
    blockClass: "border-l-2 border-ink/30 bg-ink/[0.03]",
  },
  other: {
    icon: TextQuote,
    labelClass: "text-ink/55",
    blockClass: "border-l-2 border-ink/15",
  },
};

export function HybridBlocks({ sections }: { sections: HybridSection[] }) {
  return (
    <div className="mt-2 space-y-2.5">
      {sections.map((section, index) => {
        const style = SECTION_STYLE[section.kind];
        const Icon = style.icon;
        return (
          <div key={index} className={`px-3 py-2.5 ${style.blockClass}`}>
            {section.heading && (
              <div
                className={`mb-1 flex items-center gap-1.5 text-[13px] font-semibold ${style.labelClass}`}
              >
                <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                {section.heading}
              </div>
            )}
            <p className="whitespace-pre-wrap break-words text-[14px] leading-6 text-ink/85 [overflow-wrap:anywhere]">
              {section.body}
            </p>
          </div>
        );
      })}
    </div>
  );
}
