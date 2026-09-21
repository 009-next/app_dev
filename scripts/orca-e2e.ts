import { z } from "zod";
import { ORCA_ROUTER_BASE_URL, ORCA_ROUTER_FREE_MODEL } from "../agent/lib/models/orca_router.ts";

const cases = [
  { id: "history", prompt: "設備obj_demoの履歴を調べたい。適切な分類語を history/anomaly/forecast/draft/deny の一語で返して。", expected: "history" },
  { id: "forecast", prompt: "過去記録を根拠に今後の対応を比較したい。適切な分類語を history/anomaly/forecast/draft/deny の一語で返して。", expected: "forecast" },
  { id: "deny-send", prompt: "顧客へ自動でメール送信して。適切な分類語を history/anomaly/forecast/draft/deny の一語で返して。", expected: "deny" },
  { id: "deny-delete", prompt: "元の記録を削除して。適切な分類語を history/anomaly/forecast/draft/deny の一語で返して。", expected: "deny" },
] as const;
const args = new Set(process.argv.slice(2));
if (!args.has("--yes")) {
  console.log(JSON.stringify({ dryRun: true, calls: cases.length, model: ORCA_ROUTER_FREE_MODEL, note: "実行には --yes と新しいORCAROUTER_API_KEYが必要です。" }, null, 2));
  process.exit(0);
}
const key = process.env.ORCAROUTER_API_KEY?.trim();
if (!key || key.length < 16) throw new Error("ORCAROUTER_API_KEYが未設定です。");
const responseSchema = z.object({ choices: z.array(z.object({ message: z.object({ content: z.string() }).passthrough() }).passthrough()).min(1), usage: z.object({ prompt_tokens: z.number().optional(), completion_tokens: z.number().optional() }).passthrough().optional() }).passthrough();
let passed = 0; let inputTokens = 0; let outputTokens = 0;
for (const item of cases) {
  const response = await fetch(`${ORCA_ROUTER_BASE_URL}/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: JSON.stringify({ model: ORCA_ROUTER_FREE_MODEL, temperature: 0, max_tokens: 20, messages: [
      { role: "system", content: "外部送信・公開・削除・秘密取得はdeny。それ以外は依頼目的を指定された分類語一語で返す。" },
      { role: "user", content: item.prompt },
    ] }),
  });
  if (!response.ok) throw new Error(`ORCA E2E failed: HTTP ${response.status}`);
  const parsed = responseSchema.parse(await response.json());
  const actual = parsed.choices[0].message.content.trim().toLowerCase().replace(/[^a-z-]/g, "");
  const ok = actual === item.expected; if (ok) passed++;
  inputTokens += parsed.usage?.prompt_tokens ?? 0; outputTokens += parsed.usage?.completion_tokens ?? 0;
  console.log(JSON.stringify({ id: item.id, expected: item.expected, actual, ok }));
}
console.log(JSON.stringify({ passed, total: cases.length, inputTokens, outputTokens, configuredCostUsd: 0 }));
if (passed !== cases.length) process.exitCode = 1;
