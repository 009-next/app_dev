import type { MissionReport } from "./types.ts";

export type PeriodicDraft = {
  missionId: string;
  objectId: string;
  reasonRules: string[];
  title: string;
  body: string;
  evidenceIds: string[];
};

const PERIODIC_RULES = new Set(["overdue_task", "repeated_failure", "stale_evidence"]);

export function buildPeriodicDrafts(reports: MissionReport[], limit = 20): PeriodicDraft[] {
  if (!Number.isInteger(limit) || limit < 1 || limit > 20) throw new Error("定期確認の上限は1〜20件です。");
  const drafts: PeriodicDraft[] = [];
  for (const report of reports) {
    const alerts = report.alerts.filter(alert => PERIODIC_RULES.has(alert.rule));
    if (!alerts.length) continue;
    drafts.push({
      missionId: report.missionId,
      objectId: report.input.objectId,
      reasonRules: alerts.map(alert => alert.rule),
      title: `${report.input.objectId} の確認期限・再発兆候`,
      body: "期限超過、再発、または長期未更新の候補があります。通知は送信していません。担当者が根拠と現在状態を確認してください。",
      evidenceIds: [...new Set(alerts.flatMap(alert => alert.evidenceIds))],
    });
    if (drafts.length >= limit) break;
  }
  return drafts;
}
