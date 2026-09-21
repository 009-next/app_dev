import { appendFile, readFile } from "node:fs/promises";
import { defineTool } from "eve/tools";
import { always } from "eve/tools/approval";
import { z } from "zod";
import { resolveVaultMarkdown, safeOperationId } from "../../../lib/paths.ts";

export default defineTool({
  description: "承認後、20_Actions.mdへ重複防止ID付きのToDoを追記する。既存内容は変更しない。",
  inputSchema: z.object({
    operationId: z.string().min(8).max(64),
    task: z.string().min(1).max(500),
    owner: z.string().min(1).max(100).default("自分"),
    due: z.string().max(100).default("未定"),
    related: z.array(z.string().min(1).max(200)).max(20).default([]),
    acceptanceCriteria: z.string().min(1).max(1_000),
  }),
  approval: always(),
  label: { start: ({ task }) => `ToDoを追記: ${task.slice(0, 40)}` },
  async execute(input) {
    const operationId = safeOperationId(input.operationId);
    const target = resolveVaultMarkdown("20_Actions.md");
    const marker = `<!-- agent-op:${operationId} -->`;
    const existing = await readFile(target, "utf8");
    if (existing.includes(marker)) return { appended: false, deduplicated: true, path: "20_Actions.md" };
    const related = input.related.length === 0 ? "なし" : input.related.join(", ");
    const block = `\n${marker}\n- [ ] ${input.task}\n  - 担当: ${input.owner}\n  - 期限: ${input.due}\n  - 関連: ${related}\n  - 完了条件: ${input.acceptanceCriteria}\n`;
    await appendFile(target, block, "utf8");
    return { appended: true, deduplicated: false, path: "20_Actions.md" };
  },
});
