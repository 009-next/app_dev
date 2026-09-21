import { appendFile, readFile } from "node:fs/promises";
import { defineTool } from "eve/tools";
import { always } from "eve/tools/approval";
import { z } from "zod";
import { resolveVaultMarkdown, safeOperationId } from "../../../lib/paths.ts";

export default defineTool({
  description: "承認後、AIエージェント判断ログへ根拠付きの決定を追記する。既存内容は変更しない。",
  inputSchema: z.object({
    operationId: z.string().min(8).max(64),
    date: z.string().date(),
    decision: z.string().min(1).max(1_000),
    reason: z.string().min(1).max(2_000),
    evidence: z.array(z.string().min(1).max(300)).min(1).max(30),
    impacts: z.array(z.string().min(1).max(500)).max(30).default([]),
  }),
  approval: always(),
  label: { start: ({ decision }) => `判断を記録: ${decision.slice(0, 40)}` },
  async execute(input) {
    const operationId = safeOperationId(input.operationId);
    const path = "30_Library/AIエージェント判断ログ.md";
    const target = resolveVaultMarkdown(path);
    const marker = `<!-- agent-op:${operationId} -->`;
    const existing = await readFile(target, "utf8");
    if (existing.includes(marker)) return { appended: false, deduplicated: true, path };
    const evidence = input.evidence.map((item) => `  - ${item}`).join("\n");
    const impacts = input.impacts.length === 0 ? "  - 未記載" : input.impacts.map((item) => `  - ${item}`).join("\n");
    const block = `\n## ${input.date}\n\n${marker}\n- 決定: ${input.decision}\n- 理由: ${input.reason}\n- 根拠:\n${evidence}\n- 影響:\n${impacts}\n`;
    await appendFile(target, block, "utf8");
    return { appended: true, deduplicated: false, path };
  },
});
