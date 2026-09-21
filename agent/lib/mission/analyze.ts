import { randomUUID } from "node:crypto";
import { missionInputSchema, type MissionInput, type MissionReport, type EvidenceSet, type Alert, type Scenario } from "./types.ts";
import { collectEvidence, demoEvidence, evidenceDigest, suspicious, safetyRisk } from "./evidence.ts";
import { initialDecisionTrace } from "./telemetry.ts";
import { assessVoiceInput, requestWithVoice } from "./voice.ts";
import { assessSemanticRelation } from "./semantic.ts";
import { missionModelPlan } from "./model_tiers.ts";

export function routeRequest(request: string): MissionReport["route"] {
  const highRisk = safetyRisk(request);
  const prohibited = /外部送信|社外送信|メール送信|削除|公開して|アップロード|delete|upload/i.test(request) || suspicious(request);
  const intent = /予測|将来|シナリオ|比較|今後/.test(request) ? "forecast"
    : /異常|故障|異音|再発/.test(request) ? "anomaly"
    : /カード|下書き|ToDo|タスク|引継ぎ/i.test(request) ? "draft"
    : /履歴|検索|記憶|過去|経緯/.test(request) ? "history" : "clarify";
  return {
    intent, risk: highRisk || prohibited ? "high" : intent === "anomaly" || intent === "forecast" ? "medium" : "low",
    reasons: [prohibited ? "許可されない操作または指示の書換え候補です。読取りに限定します。"
      : highRisk ? "安全に関わる語を検出しました。責任者による判断が必要です。"
      : intent === "clarify" ? "依頼を分類できません。目的の確認が必要です。" : `依頼を ${intent} に分類しました。`],
    selectedRoles: ["履歴検索", ...(intent !== "history" && intent !== "clarify" ? ["異常検査"] : []), ...(intent === "forecast" ? ["シナリオ比較"] : [])],
    allowedOperations: ["read_history", "inspect_signals", "view_map"],
    recommendedModel: intent === "draft" || intent === "forecast" ? "high-quality" : "low-cost",
  };
}

export function buildMission(input: MissionInput, evidence: EvidenceSet, now = new Date()): MissionReport {
  const voice = assessVoiceInput(input);
  const semantic = assessSemanticRelation(input, voice);
  const route = routeRequest(requestWithVoice(input));
  const today = now.toISOString().slice(0, 10);
  const alerts: Alert[] = [];
  const add = (rule: string, severity: Alert["severity"], message: string, ids: string[]) => alerts.push({ rule, severity, message, evidenceIds: ids });
  const valid = evidence.records.filter(r => !r.quarantined);
  const quarantined = evidence.records.filter(r => r.quarantined);
  const hazardous = evidence.records.filter(r => r.safetyRisk);
  if (hazardous.length) add("high_risk_evidence", "high", "履歴に安全上の危険語があります。過去の記録や否定表現でも自動判断せず、責任者の確認を求めます。", hazardous.map(r => r.id));
  if (quarantined.length) add("quarantined_instruction", "high", "資料内の命令・秘密情報候補を隔離しました。内容確認が必要です。", quarantined.map(r => r.id));
  if (!valid.length) add("no_evidence", "warning", "この設備IDの根拠が見つかりません。履歴は作りません。", []);
  if (evidence.incomplete) add("incomplete_scan", "warning", "検索範囲の一部を確認できていません。", []);
  const dated = valid.filter(r => r.date && r.date <= today);
  const undated = valid.filter(r => !r.date || r.date > today);
  if (undated.length) add("unknown_date", "warning", "日付不明・未来日付の記録があります。", undated.map(r => r.id));
  const stale = dated.filter(r => now.getTime() - Date.parse(`${r.date}T00:00:00Z`) > 90 * 86400000);
  if (stale.length) add("stale_evidence", "warning", "90日より古い情報です。現在の状態を再確認してください。", stale.map(r => r.id));
  const failures = dated.filter(r => r.event === "failure");
  const recentFailures = failures.filter(r => now.getTime() - Date.parse(`${r.date}T00:00:00Z`) <= 30 * 86400000);
  if (recentFailures.length >= 2) add("repeated_failure", "warning", "30日以内に故障記録が複数あります。同一原因とは断定できません。", recentFailures.map(r => r.id));
  const overdue = valid.filter(r => r.due && r.due < today && r.status === "open");
  if (overdue.length) add("overdue_task", "warning", "未完了の記録に期限超過があります。完了記録の有無を確認してください。", overdue.map(r => r.id));
  for (const date of new Set(dated.map(r => r.date))) {
    const rows = dated.filter(r => r.date === date);
    if (rows.some(r => r.status === "normal") && rows.some(r => r.status === "abnormal")) {
      add("conflicting_status", "high", "同日記録に正常・異常の両方があります。測定時刻と条件を確認してください。", rows.map(r => r.id));
    }
  }
  if (route.risk === "high") add("high_risk_request", "high", route.reasons[0], []);
  if (voice.state === "safety_hold") add("voice_safety_hold", "high", voice.reason, []);
  if (semantic.state === "conflict") add("semantic_conflict", "high", semantic.reason, []);
  if (alerts.some(a => a.severity === "high")) route.risk = "high";
  const latestDate = [...dated].sort((a, b) => b.date!.localeCompare(a.date!))[0]?.date ?? null;
  const sufficient = dated.length >= 2 && stale.length < dated.length && !evidence.incomplete && !undated.length && route.risk !== "high";
  const questions: string[] = [];
  if (!sufficient) questions.push("対象設備の最新の日時付き記録と、現在の状態を確認してください。");
  if (route.intent === "clarify") questions.push("履歴検索、異常調査、将来比較、下書き作成のどれを行いますか。");
  if (voice.questionDetected) questions.push("確認済み文字起こしに質問があります。回答に必要な対象・時点・責任者を確認してください。");
  if (semantic.state === "needs_confirmation") questions.push("画面と文字起こしの意味タグが一致しません。対象・時刻・状態が同じか確認してください。");
  if (semantic.state === "conflict") questions.push("画面と会話の矛盾を責任者が確認するまで、下書き保存や次の実行を行いません。");
  if (route.risk === "high") questions.push("安全責任者が現場を確認し、矛盾・隔離された資料を解消してください。");
  if (input.budgetUsd === 0) questions.push("AIによる追加文章生成の予算は0です。ローカル分析結果のみを利用できます。");
  const scenarios: Scenario[] = [];
  if (route.intent === "forecast" && sufficient) {
    const evidenceIds = dated.map(r => r.id);
    route.allowedOperations.push("compare_scenarios");
    const common = { kind: "conditional-scenario" as const, evidenceIds, estimatedCost: null, estimatedDuration: null, probability: null };
    scenarios.push(
      { ...common, id: "inspect", title: "追加点検で判断材料を集める", premise: "担当者が安全に点検できる条件を確認する。", expected: "現在の状態を記録し、履歴との差を比較できる。", risk: "点検中の停止時間と費用は未確認。", verify: "手順、担当、実施時刻、点検結果を確認する。" },
      { ...common, id: "repair", title: "修理・交換の必要性を評価する", premise: "点検で対象部品の不具合が確認された場合。", expected: "原因に対応する処置候補を検討できる。改善効果は未検証。", risk: "原因未確定の交換は不要な費用につながり得る。", verify: "根拠となる点検結果、見積り、処置後の再確認が必要。" },
      { ...common, id: "observe", title: "条件付き監視案を比較する", premise: "安全責任者が運転条件を確認した場合に限る。継続運転の許可ではない。", expected: "追加記録により症状の変化を追跡できる可能性がある。", risk: "見逃し・再発の可能性は未評価。", verify: "監視間隔、停止基準、担当者を責任者が決める。" },
    );
  }
  const missionId = `mission_${randomUUID()}`;
  const ready = sufficient && route.intent !== "clarify";
  const actions: MissionReport["actions"] = [];
  // A pure history query never gains a write capability just by being successful.
  if (ready && route.intent !== "history") {
    route.allowedOperations.push("save_action_draft");
    actions.push({ id: "inspection", kind: "inspection-draft", title: `${input.objectId} の確認・引継ぎ下書き`,
      body: ["記録に基づく確認案（作業実行の指示ではありません）。", ...alerts.map(a => `要確認: ${a.message}`), "次の確認: 現在の状態・担当・期限・完了条件を責任者と確認する。"].join("\n"),
      evidenceIds: dated.map(r => r.id) });
  }
  const status = route.risk === "high" ? "read_only" : ready ? "ready" : "needs_confirmation";
  route.reasons.push(sufficient ? "日時付き根拠を確認しました。予測確率は算出していません。" : "根拠が不足または矛盾しています。保存能力を付与しません。");
  const report: MissionReport = {
    schemaVersion: 1, missionId, input, createdAt: now.toISOString(), expiresAt: new Date(now.getTime() + 30 * 60000).toISOString(), status, route,
    timeline: evidence.records, snapshotDigest: evidenceDigest(evidence), incomplete: evidence.incomplete,
    notices: [...evidence.notices, ...(input.screenImageRefs.length ? ["利用者が確認した画面フレームを添付しました。動画・音声の連続取得や画像内容の自動判定は行っていません。画面の意味は利用者が選んだ安全なタグだけを使います。"] : []), ...(input.voice ? ["確認済み文字起こしをこのMissionの判断にだけ利用します。生音声は保存・LLM送信しません。"] : [])],
    voice,
    semantic,
    alerts, scenarios, actions, questions, decisionTrace: [],
    evidenceQuality: { datedRecords: dated.length, latestDate, label: sufficient ? "usable" : "insufficient", explanation: "記録件数・日付・矛盾・検索完了を点検した指標です。故障確率やAIの確信度ではありません。" },
    steps: [
      { id: "sort", label: "組分け帽子", status: "completed", detail: route.reasons[0] },
      { id: "room", label: "必要の部屋", status: "completed", detail: route.allowedOperations.join(" / ") },
      { id: "memory", label: "サイコメトリー", status: "completed", detail: `${evidence.records.length}件 / ${evidence.scannedFiles}ファイル確認` },
      { id: "sense", label: "フォース・センス", status: "completed", detail: `${alerts.length}件の確認事項` },
      { id: "vision", label: "フォース・ビジョン", status: scenarios.length ? "completed" : "skipped", detail: scenarios.length ? "条件付き3シナリオ。実測予測ではありません。" : "依頼対象外、または根拠不足のため実行しません。" },
      { id: "push", label: "フォース・プッシュ", status: actions.length ? "waiting" : "skipped", detail: actions.length ? "下書き保存は承認待ちです。" : "読取りのみ。追加確認が必要です。" },
    ],
    cost: { localModelCalls: 0, localModelCostUsd: 0, outerAgentCostUsd: null, savingsPercent: null, budgetUsd: input.budgetUsd,
      explanation: "この分析関数はモデル呼出し0回。Eve会話の生成費、PC稼働費は別です。品質同等の比較実測がないため削減率は表示しません。" },
  };
  report.decisionTrace = initialDecisionTrace(report);
  if (input.voice) report.decisionTrace.push({
    at: report.createdAt, stage: "voice", options: ["discard", "use-confirmed-transcript"], selected: voice.state === "confirmed" ? "use-confirmed-transcript" : "discard",
    tool: "voice_input_gate", reason: voice.reason, evidenceIds: [], model: null, inputTokens: null, outputTokens: null,
    costUsd: 0, costSource: "not-applicable", latencyMs: 0,
  });
  if (semantic.state !== "not_available") report.decisionTrace.push({
    at: report.createdAt, stage: "semantic", options: ["screen_only", "voice_only", "aligned", "needs_confirmation", "conflict", "safe-stop"],
    selected: semantic.state, tool: "semantic_relation_gate", reason: semantic.reason, evidenceIds: [], model: null, inputTokens: null, outputTokens: null,
    costUsd: 0, costSource: "not-applicable", latencyMs: 0,
  });
  const modelPlan = missionModelPlan(report);
  report.decisionTrace.push({
    at: report.createdAt, stage: "model-plan", options: modelPlan.map(item => item.id), selected: modelPlan.find(item => item.enabled)?.id ?? "safe-stop",
    tool: "missionModelPlan", reason: modelPlan.filter(item => item.enabled).map(item => `${item.model}: ${item.useWhen}`).join(" "), evidenceIds: [], model: null,
    inputTokens: null, outputTokens: null, costUsd: 0, costSource: "not-applicable", latencyMs: 0,
  });
  if (input.screenImageRefs.length) report.decisionTrace.push({
    at: report.createdAt, stage: "media", options: ["discard", "attach-masked-frame"], selected: "attach-masked-frame",
    tool: "screen_frame_gate", reason: "利用者が取得・マスキング確認した静止フレーム1件を承認対象へ結び付けました。生動画・音声は受け取りません。意味判断には確認済みタグだけを使います。",
    evidenceIds: [], model: null, inputTokens: null, outputTokens: null, costUsd: 0, costSource: "not-applicable", latencyMs: 0,
  });
  return report;
}

export async function analyzeMission(raw: unknown, options: Parameters<typeof collectEvidence>[1] = {}, now = new Date()): Promise<MissionReport> {
  const input = missionInputSchema.parse(raw);
  const evidence = input.source === "demo" ? demoEvidence(input.objectId, now) : await collectEvidence(input.objectId, options);
  return buildMission(input, evidence, now);
}

export function assertOperation(report: MissionReport, operation: string, now = new Date()): void {
  if (Date.parse(report.expiresAt) <= now.getTime()) throw new Error("Mission Roomの有効期限が切れました。再分析してください。");
  if (!report.route.allowedOperations.includes(operation)) throw new Error("このMission Roomでは許可されていない操作です。");
}
