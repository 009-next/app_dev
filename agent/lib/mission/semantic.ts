import type { MissionInput, SemanticAssessment, VoiceAssessment } from "./types.ts";

/**
 * Compares only derived, user-confirmed labels. It deliberately does not OCR,
 * retain, or externally transmit a screen frame or the transcript text.
 */
export function assessSemanticRelation(input: MissionInput, voice: VoiceAssessment): SemanticAssessment {
  const screenSignals = input.screenContext?.observedSignals ?? [];
  const voiceSignals = voice.state === "confirmed" ? voice.matchedSignals : [];
  const capturedAt = input.screenContext?.capturedAt ?? null;
  if (voice.state === "safety_hold") return {
    state: "safety_hold", screenSignals, voiceSignals: [], sharedSignals: [], conflicts: [], capturedAt,
    reason: "文字起こしが安全停止になったため、画面との統合判断を行いません。",
  };
  if (!screenSignals.length && !voiceSignals.length) return {
    state: "not_available", screenSignals, voiceSignals, sharedSignals: [], conflicts: [], capturedAt,
    reason: "画面の意味タグと確認済み文字起こしの両方が必要です。",
  };
  if (!screenSignals.length) return {
    state: "voice_only", screenSignals, voiceSignals, sharedSignals: [], conflicts: [], capturedAt,
    reason: "確認済み文字起こしだけがあります。画面との意味的一致は判定しません。",
  };
  if (!voiceSignals.length) return {
    state: "screen_only", screenSignals, voiceSignals, sharedSignals: [], conflicts: [], capturedAt,
    reason: "画面の意味タグだけがあります。会話との意味的一致は判定しません。",
  };
  const sharedSignals = screenSignals.filter(signal => voiceSignals.includes(signal));
  const conflicts: string[] = [];
  if (screenSignals.includes("normal") && voiceSignals.includes("anomaly")) conflicts.push("画面は正常・完了、会話は異常を示しています。");
  if (conflicts.length) return {
    state: "conflict", screenSignals, voiceSignals, sharedSignals, conflicts, capturedAt,
    reason: "画面と会話の安全上重要な意味が矛盾しています。自動保存・実行は停止します。",
  };
  if (sharedSignals.length) return {
    state: "aligned", screenSignals, voiceSignals, sharedSignals, conflicts, capturedAt,
    reason: `画面と確認済み文字起こしで「${sharedSignals.join("・")}」が一致しました。原文や画像内容は保存せず、確認事項の根拠としてだけ使います。`,
  };
  return {
    state: "needs_confirmation", screenSignals, voiceSignals, sharedSignals, conflicts, capturedAt,
    reason: "画面と会話に共通する安全な意味タグがありません。別の時点・対象でないか確認してください。",
  };
}
