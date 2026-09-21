import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { defineTool } from "eve/tools";
import { always } from "eve/tools/approval";
import { z } from "zod";
import { cardDraftRoot, safeOperationId } from "../../../lib/paths.ts";

const inputSchema = z.object({
  operationId: z.string().min(8).max(64),
  objectId: z.string().min(3).max(64),
  title: z.string().min(1).max(120),
  summary: z.string().min(1).max(2_000),
  changes: z.array(z.string().min(1).max(500)).min(1).max(30),
  evidenceRefs: z.array(z.string().min(1).max(200)).min(1).max(50),
  imageRefs: z.array(z.string().regex(/^masked:\/\//)).min(2).max(40),
  visibility: z.enum(["private", "team", "link-restricted"]),
  expiresAt: z.string().datetime().nullable(),
  maskingApproved: z.literal(true),
});

export default defineTool({
  description: "承認後、検査済みカードをローカルのJSON下書きとして保存する。公開や共有はしない。",
  inputSchema,
  approval: always(),
  label: { start: ({ objectId }) => `カード下書き ${objectId} を保存` },
  async execute(input) {
    const operationId = safeOperationId(input.operationId);
    const root = cardDraftRoot();
    await mkdir(root, { recursive: true });
    const target = resolve(root, `${operationId}.json`);
    const document = {
      schemaVersion: 1,
      status: "draft",
      createdAt: new Date().toISOString(),
      ...input,
    };
    const serialized = `${JSON.stringify(document, null, 2)}\n`;

    try {
      await writeFile(target, serialized, { encoding: "utf8", flag: "wx" });
      return { saved: true, deduplicated: false, path: `data/card-drafts/${operationId}.json` };
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw error;
      const existing = JSON.parse(await readFile(target, "utf8")) as Record<string, unknown>;
      const comparable = { ...existing };
      delete comparable.createdAt;
      const expected = { ...document } as Record<string, unknown>;
      delete expected.createdAt;
      if (JSON.stringify(comparable) !== JSON.stringify(expected)) {
        throw new Error("同じoperationIdで異なる内容は保存できません。");
      }
      return { saved: true, deduplicated: true, path: `data/card-drafts/${operationId}.json` };
    }
  },
});
