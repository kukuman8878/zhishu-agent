/**
 * hybrid 综合文本解析
 * 把后端三段式回答（【数据结论】【文档说明】【综合判断】）拆成结构化区块，
 * 供前端按语义分块渲染（视觉结构即信息），并保留完整原文用于复制。
 */

export type HybridSectionKind = "data" | "doc" | "verdict" | "other";

export type HybridSection = {
  kind: HybridSectionKind;
  heading: string | null;
  body: string;
};

const HEADING_KIND: Record<string, HybridSectionKind> = {
  数据结论: "data",
  文档说明: "doc",
  综合判断: "verdict",
};

/**
 * 按 【xxx】 标题把整段文本切成区块。
 * 未识别标题的正文统一归为 other（一般是前言/尾注），保持原文顺序不丢内容。
 */
export function parseHybridSections(content: string): HybridSection[] {
  if (!content) return [];
  const parts = content.split(/(?=【[^】]+】)/);
  return parts
    .filter((part) => part.trim().length > 0)
    .map((part) => {
      const match = part.match(/^【([^】]+)】\s*\n?/);
      if (!match) {
        return { kind: "other" as const, heading: null, body: part.trim() };
      }
      const title = match[1];
      const kind = HEADING_KIND[title] ?? ("other" as const);
      return {
        kind,
        heading: title,
        body: part.slice(match[0].length).trim(),
      };
    });
}
