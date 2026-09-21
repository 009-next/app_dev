import { createHash } from "node:crypto";
import { readdir, realpath, stat, open } from "node:fs/promises";
import { relative, resolve, sep, isAbsolute } from "node:path";
import { cardDraftRoot, vaultRoot, resolveInside } from "../paths.ts";
import { objectIdSchema, type Evidence, type EvidenceSet } from "./types.ts";

export const digest = (value: string) => createHash("sha256").update(value).digest("hex");
export const safetyRisk = (text: string): boolean => /火災|発煙|感電|人身|爆発|漏電|安全装置|緊急停止|fire|explosion/i.test(text);
export function suspicious(text: string): boolean {
  return /ignore\s+(all\s+)?(previous|prior)|system\s*prompt|API[_ -]?KEY|sk-[a-zA-Z0-9]{12,}|指示.{0,8}(無視|上書き)|秘密.{0,8}(送信|表示)|外部.{0,8}送信|承認.{0,8}(不要|省略|無視)/i.test(text);
}
export function dateOnly(value: string | undefined): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? value : null;
}
// Filesystem boundary is checked against canonical paths, including directory junctions.
export async function canonicalInside(root: string, path: string): Promise<string> {
  const base = await realpath(root);
  const target = await realpath(path);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) throw new Error("許可範囲外のリンクです");
  return target;
}
export async function boundedRead(root: string, path: string, maxBytes = 1_000_000): Promise<string> {
  const canonical = await canonicalInside(root, path);
  const handle = await open(canonical, "r");
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > maxBytes) throw new Error("読取サイズ上限です");
    // Fixed-size read also bounds files which grow after stat.
    const bytes = Buffer.alloc(maxBytes + 1);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead > maxBytes) throw new Error("読取サイズ上限です");
    return bytes.subarray(0, bytesRead).toString("utf8");
  } finally { await handle.close(); }
}
export function makeEvidence(path: string, line: number, text: string, source: Evidence["source"]): Evidence {
  const quarantined = suspicious(text);
  return {
    id: `${source}:${path}:${line}`, path, line, digest: digest(text), source, quarantined, safetyRisk: safetyRisk(text),
    text: quarantined ? "[資料中の命令または秘密情報候補を隔離しました]" : text.slice(0, 600),
    date: quarantined ? null : dateOnly(text.match(/\b\d{4}-\d{2}-\d{2}\b/)?.[0]),
    event: quarantined ? null : text.match(/\bevent:([a-z_]+)/)?.[1] ?? null,
    status: quarantined ? null : text.match(/\bstatus:([a-z_]+)/)?.[1] ?? null,
    due: quarantined ? null : dateOnly(text.match(/\bdue:(\d{4}-\d{2}-\d{2})/)?.[1]),
  };
}

export function demoEvidence(objectId: string, now: Date): EvidenceSet {
  const day = (ago: number) => new Date(now.getTime() - ago * 86_400_000).toISOString().slice(0, 10);
  const lines = [
    `${day(21)} | ${objectId} | event:inspection | status:normal | 合成デモ: 定期点検では異音なし。`,
    `${day(12)} | ${objectId} | event:failure | status:abnormal | 合成デモ: 駆動部で異音を記録。原因は未確認。`,
    `${day(2)} | ${objectId} | event:failure | status:abnormal | 合成デモ: 同じ箇所で異音が再発。`,
    `${day(1)} | ${objectId} | event:task | status:open | due:${day(1)} | 合成デモ: 保全担当による点検待ち。`,
  ];
  return { records: lines.map((line, i) => makeEvidence("synthetic-equipment.md", i + 1, line, "demo")),
    incomplete: false, scannedFiles: 1, notices: ["合成デモです。実設備の記録ではありません。"] };
}

export async function collectEvidence(objectId: string, options: { vault?: string; cards?: string; signal?: AbortSignal } = {}): Promise<EvidenceSet> {
  objectIdSchema.parse(objectId);
  const result: EvidenceSet = { records: [], incomplete: false, scannedFiles: 0, notices: [] };
  const roots: [string, Evidence["source"], string][] = [[options.vault ?? vaultRoot(), "vault", ".md"], [options.cards ?? cardDraftRoot(), "card", ".json"]];
  const idMatch = new RegExp(`(^|[^a-zA-Z0-9_-])${objectId}($|[^a-zA-Z0-9_-])`);
  const started = Date.now();
  let totalBytes = 0;
  let directories = 0;
  const skipped = (reason: string) => { result.incomplete = true; if (!result.notices.includes(reason)) result.notices.push(reason); };
  for (const [root, source, extension] of roots) {
    try { await stat(root); } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        if (source === "vault") skipped("Vaultが見つかりません。");
        continue;
      }
      skipped("記録の一部を開けません。"); continue;
    }
    const pending = [root];
    while (pending.length) {
      options.signal?.throwIfAborted();
      if (Date.now() - started > 4000 || result.scannedFiles >= 500 || directories++ >= 200 || totalBytes > 10_000_000) {
        skipped("検索上限に達しました。全記録を確認できていません。"); break;
      }
      const directory = pending.shift()!;
      try {
        await canonicalInside(root, directory);
        const entries = (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name));
        for (const entry of entries) {
          options.signal?.throwIfAborted();
          if (entry.name.startsWith(".")) continue;
          if (entry.isSymbolicLink()) { skipped("リンク経由の記録を除外しました。"); continue; }
          const path = resolveInside(root, relative(root, resolve(directory, entry.name)));
          if (entry.isDirectory()) { pending.push(path); continue; }
          if (!entry.isFile() || !entry.name.toLowerCase().endsWith(extension)) continue;
          if (result.scannedFiles >= 500 || Date.now() - started > 4000 || totalBytes > 10_000_000) { skipped("検索上限に達しました。全記録を確認できていません。"); break; }
          result.scannedFiles++;
          try {
            const content = await boundedRead(root, path);
            totalBytes += Buffer.byteLength(content);
            const file = relative(root, path).replaceAll("\\", "/");
            if (source === "card") {
              const card = JSON.parse(content) as Record<string, unknown>;
              if (card.objectId !== objectId) continue;
              // Local dossiers stay private; public/team data is not merged implicitly.
              if (card.visibility !== "private") { skipped("公開範囲が異なるカードを除外しました。"); continue; }
              const fields = [card.createdAt, objectId, card.title, card.summary].filter(v => typeof v === "string").join(" | ");
              result.records.push(makeEvidence(file, 1, fields, source));
            } else {
              content.split(/\r?\n/).forEach((line, index) => {
                if (idMatch.test(line) && result.records.length < 101) result.records.push(makeEvidence(file, index + 1, line, source));
              });
            }
            if (result.records.length >= 100) { skipped("記録件数上限に達しました。"); break; }
          } catch { skipped("読めない・大きすぎる・形式不正の記録があります。"); }
        }
      } catch { skipped("アクセスできないディレクトリがあります。"); }
      if (result.records.length >= 100) break;
    }
  }
  result.records = result.records.slice(0, 100).sort((a, b) => (a.date ?? "9999").localeCompare(b.date ?? "9999") || a.id.localeCompare(b.id));
  return result;
}

export function evidenceDigest(set: Pick<EvidenceSet, "records" | "incomplete">): string {
  return digest(JSON.stringify({ incomplete: set.incomplete, records: set.records.map(r => [r.id, r.digest]).sort((a, b) => a[0].localeCompare(b[0])) }));
}
