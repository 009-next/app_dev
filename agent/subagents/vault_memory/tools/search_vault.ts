import { readdir, readFile, stat } from "node:fs/promises";
import { relative } from "node:path";
import { defineTool } from "eve/tools";
import { z } from "zod";
import { resolveInside, vaultRoot } from "../../../lib/paths.ts";

async function markdownFiles(root: string, maxFiles = 500): Promise<string[]> {
  const found: string[] = [];
  const pending = [root];
  while (pending.length > 0 && found.length < maxFiles) {
    const current = pending.pop()!;
    for (const entry of await readdir(current, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue;
      const full = resolveInside(current, entry.name);
      if (entry.isDirectory()) pending.push(full);
      else if (entry.isFile() && entry.name.toLowerCase().endsWith(".md")) found.push(full);
      if (found.length >= maxFiles) break;
    }
  }
  return found;
}

export default defineTool({
  description: "VaultのMarkdownを文字列検索し、相対パス・行番号・短い抜粋を返す。",
  inputSchema: z.object({
    query: z.string().trim().min(1).max(100),
    directory: z.string().min(1).max(300).default("."),
    maxResults: z.number().int().min(1).max(50).default(20),
  }),
  async execute({ query, directory, maxResults }) {
    const root = vaultRoot();
    const searchRoot = resolveInside(root, directory);
    const rootInfo = await stat(searchRoot);
    if (!rootInfo.isDirectory()) throw new Error("検索範囲はディレクトリを指定してください。");
    const needle = query.toLocaleLowerCase("ja");
    const matches: Array<{ path: string; line: number; excerpt: string }> = [];

    for (const file of await markdownFiles(searchRoot)) {
      const info = await stat(file);
      if (info.size > 1_000_000) continue;
      const lines = (await readFile(file, "utf8")).split(/\r?\n/);
      for (let index = 0; index < lines.length; index += 1) {
        if (!lines[index].toLocaleLowerCase("ja").includes(needle)) continue;
        matches.push({
          path: relative(root, file).replaceAll("\\", "/"),
          line: index + 1,
          excerpt: lines[index].trim().slice(0, 300),
        });
        if (matches.length >= maxResults) return { matches, limited: true };
      }
    }
    return { matches, limited: false };
  },
});
