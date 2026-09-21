import type { MissionReport } from "./types.ts";

export type DecisionTrace = {
  at: string;
  stage: "route" | "evidence" | "media" | "voice" | "semantic" | "model-plan" | "safety" | "model" | "fallback" | "approval" | "periodic";
  options: string[];
  selected: string;
  tool: string | null;
  reason: string;
  evidenceIds: string[];
  model: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  costUsd: number | null;
  costSource: "reported" | "estimated" | "not-applicable" | "unknown";
  latencyMs: number | null;
};

export function initialDecisionTrace(report: Pick<MissionReport, "createdAt" | "route" | "timeline" | "alerts">): DecisionTrace[] {
  return [
    {
      at: report.createdAt,
      stage: "route",
      options: ["history", "anomaly", "forecast", "draft", "clarify"],
      selected: report.route.intent,
      tool: "routeRequest",
      reason: report.route.reasons[0] ?? "依頼分類",
      evidenceIds: [],
      model: null,
      inputTokens: null,
      outputTokens: null,
      costUsd: 0,
      costSource: "not-applicable",
      latencyMs: 0,
    },
    {
      at: report.createdAt,
      stage: "evidence",
      options: ["continue", "ask", "read-only"],
      selected: report.route.risk === "high" ? "read-only" : report.timeline.length ? "continue" : "ask",
      tool: "collectEvidence",
      reason: `${report.timeline.length}件の記録と${report.alerts.length}件の確認事項を検査`,
      evidenceIds: report.timeline.map(item => item.id),
      model: null,
      inputTokens: null,
      outputTokens: null,
      costUsd: 0,
      costSource: "not-applicable",
      latencyMs: 0,
    },
  ];
}

export function measuredCost(trace: DecisionTrace[]): number | null {
  const modelRows = trace.filter(row => row.stage === "model" || row.stage === "fallback");
  if (!modelRows.length || modelRows.some(row => row.costUsd === null || row.costSource === "unknown")) return null;
  return modelRows.reduce((sum, row) => sum + row.costUsd!, 0);
}
