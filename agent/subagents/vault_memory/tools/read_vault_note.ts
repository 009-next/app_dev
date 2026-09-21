import { readFile, stat } from "node:fs/promises";
import { relative } from "node:path";
import { defineTool } from "eve/tools";
import { z } from "zod";
import { resolveVaultMarkdown, vaultRoot } from "../../../lib/paths.ts";

export default defineTool({
  description: "Vault内の指定Markdownノートを安全に読み、行番号付きで返す。",
  inputSchema: z.object({ path: z.string().min(1).max(500) }),
  async execute({ path }) {
    const target = resolveVaultMarkdown(path);
    const info = await stat(target);
    if (!info.isFile() || info.size > 1_000_000) throw new Error("読取対象が大きすぎるか、ファイルではありません。");
    const content = await readFile(target, "utf8");
    const maxChars = 40_000;
    const bounded = content.slice(0, maxChars);
    return {
      path: relative(vaultRoot(), target).replaceAll("\\", "/"),
      content: bounded
        .split(/\r?\n/)
        .map((line, index) => `${index + 1}: ${line}`)
        .join("\n"),
      truncated: content.length > maxChars,
    };
  },
});
