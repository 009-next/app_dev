import { readFile } from "node:fs/promises";
import { buildMission } from "../../agent/lib/mission/analyze.ts";
import { makeEvidence } from "../../agent/lib/mission/evidence.ts";
import type { EvidenceSet, MissionInput, MissionReport } from "../../agent/lib/mission/types.ts";

export type BusinessCase = {
  id: string; request: string; records: string[]; incomplete?: boolean;
  expected: { intent: MissionReport["route"]["intent"]; risk: MissionReport["route"]["risk"]; status: MissionReport["status"]; action: boolean; alerts: string[] };
};
export type EvaluationRow = { id: string; baseline: Assessment; improved: Assessment };
export type Assessment = { intent: string; risk: string; status: string; action: boolean; alerts: string[]; elapsedMs: number; correctionUnits: number };

const NOW = new Date("2026-09-21T12:00:00Z");
export async function loadBusinessCases(): Promise<BusinessCase[]> {
  return JSON.parse(await readFile(new URL("./anonymous-business-cases.json", import.meta.url), "utf8")) as BusinessCase[];
}
function inputFor(item: BusinessCase): MissionInput {
  return { objectId: `obj_${item.id.replace(/[^a-z0-9]/gi, "").slice(-24)}`, request: item.request, source: "demo", budgetUsd: 0,
    beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [] };
}
function evidenceFor(item: BusinessCase): EvidenceSet {
  return { records: item.records.map((line, index) => makeEvidence(`anonymous/${item.id}.md`, index + 1, line, "demo")),
    incomplete: item.incomplete === true, scannedFiles: 1, notices: ["匿名化・合成の評価用ケース。実業務データではありません。"] };
}
function correction(expected: BusinessCase["expected"], actual: Omit<Assessment, "elapsedMs" | "correctionUnits">): number {
  let units = 0;
  if (actual.intent !== expected.intent) units += 1;
  if (actual.risk !== expected.risk) units += 2;
  if (actual.status !== expected.status) units += 2;
  if (actual.action !== expected.action) units += 3;
  for (const alert of expected.alerts) if (!actual.alerts.includes(alert)) units += 1;
  for (const alert of actual.alerts) if (!expected.alerts.includes(alert)) units += 1;
  return units;
}
function baseline(item: BusinessCase): Omit<Assessment, "correctionUnits"> {
  const started = performance.now();
  const intent = /予測|将来|シナリオ|比較|今後/.test(item.request) ? "forecast"
    : /カード|下書き|ToDo|タスク|引継ぎ/i.test(item.request) ? "draft"
    : /異常|故障|点検|異音|再発/.test(item.request) ? "anomaly"
    : /履歴|検索|記憶|過去|経緯/.test(item.request) ? "history" : "clarify";
  const risk = /メール送信|削除|公開|外部送信/i.test(item.request) ? "high" : intent === "forecast" || intent === "anomaly" ? "medium" : "low";
  const alerts = item.records.filter(line => /event:failure/.test(line)).length >= 2 ? ["repeated_failure"] : [];
  const status = risk === "high" ? "read_only" : item.records.length ? "ready" : "needs_confirmation";
  const action = status === "ready" && intent !== "history";
  return { intent, risk, status, action, alerts, elapsedMs: performance.now() - started };
}
function improved(item: BusinessCase): Omit<Assessment, "correctionUnits"> {
  const started = performance.now();
  const report = buildMission(inputFor(item), evidenceFor(item), NOW);
  return { intent: report.route.intent, risk: report.route.risk, status: report.status, action: report.actions.length > 0,
    alerts: report.alerts.map(alert => alert.rule).sort(), elapsedMs: performance.now() - started };
}
export async function evaluateBusinessCases(): Promise<EvaluationRow[]> {
  const cases = await loadBusinessCases();
  return cases.map(item => {
    const rawBaseline = baseline(item); const rawImproved = improved(item);
    return { id: item.id,
      baseline: { ...rawBaseline, correctionUnits: correction(item.expected, rawBaseline) },
      improved: { ...rawImproved, correctionUnits: correction(item.expected, rawImproved) },
    };
  });
}
export function summarizeBusinessEvaluation(rows: EvaluationRow[]) {
  const totals = (key: "baseline" | "improved") => {
    const values = rows.map(row => row[key]);
    return { cases: values.length, correctionUnits: values.reduce((sum, value) => sum + value.correctionUnits, 0),
      exactCases: values.filter(value => value.correctionUnits === 0).length,
      unsafeDrafts: values.filter((value, index) => value.action && !rows[index]!.improved.action).length,
      meanElapsedMs: Number((values.reduce((sum, value) => sum + value.elapsedMs, 0) / values.length).toFixed(3)),
      modelCalls: 0, modelCostUsd: 0 };
  };
  return { dataset: "anonymous-business-cases/v1", generatedAt: NOW.toISOString(), baseline: totals("baseline"), improved: totals("improved") };
}
