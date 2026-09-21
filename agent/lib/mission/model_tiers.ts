import type { MissionReport } from "./types.ts";

export type MissionModelTier = {
  id: "tier1-orca-low-cost" | "tier2-claude-haiku" | "tier3-claude-sonnet";
  model: string;
  useWhen: string;
  enabled: boolean;
};

/**
 * A model plan is explanatory and has no authority to expand Mission tools.
 * Paid tiers remain disabled until both an explicit opt-in and a server-side key exist.
 */
export function missionModelPlan(report: Pick<MissionReport, "route" | "voice">): MissionModelTier[] {
  const orcaEnabled = Boolean(process.env.ORCAROUTER_API_KEY?.trim());
  const paidEnabled = process.env.CONNECT_FORCE_ENABLE_PAID_FALLBACK === "yes";
  const sonnetEnabled = paidEnabled && process.env.CONNECT_FORCE_ENABLE_SONNET_ESCALATION === "yes";
  const complex = report.route.intent === "forecast" || report.voice.questionDetected || report.voice.matchedSignals.length >= 2;
  return [
    { id: "tier1-orca-low-cost", model: "orcarouter/free", enabled: orcaEnabled, useWhen: "匿名化済みメタデータの短い要約・通常の意図確認。価格未報告時は費用不明として停止。429や検証失敗時は安全停止。" },
    { id: "tier2-claude-haiku", model: "claude-haiku-4-5-20251001", enabled: paidEnabled, useWhen: "第1段階が失敗し、確認済みの質問・複数の業務シグナルを構造化する必要がある場合。" },
    { id: "tier3-claude-sonnet", model: "claude-sonnet-5", enabled: sonnetEnabled && complex, useWhen: "第2段階でも根拠の矛盾や複数シナリオを解消できず、明示的に有効化された場合だけ。" },
  ];
}
