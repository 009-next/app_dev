export type UsageEntry = { eventId: string; inputTokens: number | null; outputTokens: number | null; costUsd: number | null;
  costSource?: "reported" | "unknown" };

export function normalizeUsageCost(reportedCostUsd: number | undefined) {
  if (typeof reportedCostUsd === "number" && Number.isFinite(reportedCostUsd) && reportedCostUsd >= 0) {
    return { costUsd: reportedCostUsd, costSource: "reported" as const };
  }
  return { costUsd: null, costSource: "unknown" as const };
}

export function checkBudget(entries: UsageEntry[], cap: number): void {
  if (!Number.isFinite(cap) || cap < 0) throw new Error("予算設定が不正です。");
  if (entries.length >= 100) throw new Error("この会話のモデル呼出し上限です。新しい任務として確認してください。");
  if (entries.some(e => e.costUsd === null || !Number.isFinite(e.costUsd) || e.costUsd < 0)) throw new Error("モデル費用が取得できないため追加生成を停止しました。ローカル分析画面を利用してください。");
  if (entries.reduce((sum, e) => sum + e.costUsd!, 0) >= cap) throw new Error("任務の予算に達したため追加生成を停止しました。ローカル分析結果を確認してください。");
}
