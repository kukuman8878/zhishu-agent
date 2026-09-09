/**
 * 首页空状态组件
 * 展示产品入口信息和可点击的示例问数问题
 */
import { Database, FileText, Layers, LineChart, Sparkles } from "lucide-react";

type EmptyStateProps = {
  examples: string[];
  onUseExample: (example: string) => void;
};

const highlights = [
  { label: "数据查询", sub: "数仓 SQL · 实时取数", icon: Database },
  { label: "文档问答", sub: "PDF · 图 · 表格 · 引用", icon: FileText },
  { label: "跨源综合", sub: "数据与文档一次对齐", icon: Layers },
];

export function EmptyState({ examples, onUseExample }: EmptyStateProps) {
  return (
    <div className="mx-auto flex min-h-full max-w-5xl flex-col justify-center px-4 py-12">
      <div className="mb-10 max-w-3xl">
        <div className="mb-5 inline-flex items-center gap-2 border border-moss/25 bg-moss/10 px-3 py-1.5 text-sm font-semibold text-moss">
          <Sparkles className="h-4 w-4" aria-hidden="true" />
          智数 Agent
        </div>
        <h1 className="text-balance text-4xl font-semibold leading-tight text-ink sm:text-6xl">
          查数 · 查文档 · 综合问答
        </h1>
        <p className="mt-4 max-w-2xl text-base leading-7 text-ink/60">
          对结构化数仓提问走 SQL 精确取数；对 PDF/图/表提问走文档检索并附引用页码；
          需要两者结合的问题会自动跨源综合。
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {highlights.map((item) => {
          const Icon = item.icon;
          return (
            <div key={item.label} className="border border-ink/10 bg-white/55 px-4 py-4">
              <Icon className="mb-3 h-5 w-5 text-brass" aria-hidden="true" />
              <div className="text-sm font-semibold text-ink">{item.label}</div>
              <div className="mt-1 text-xs leading-5 text-ink/45">{item.sub}</div>
            </div>
          );
        })}
      </div>

      <div className="mt-6">
        <div className="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-ink/40">
          试一试
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {examples.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => onUseExample(example)}
              className="min-h-20 border border-ink/10 bg-[#fffaf1]/75 px-4 py-4 text-left text-[15px] leading-6 text-ink transition hover:-translate-y-0.5 hover:border-moss/35 hover:bg-white focus:outline-none focus:ring-2 focus:ring-moss/35"
            >
              {example}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
