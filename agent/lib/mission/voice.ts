import { suspicious } from "./evidence.ts";
import type { MissionInput, VoiceAssessment } from "./types.ts";

const intentTerms = {
  anomaly: /異常|故障|異音|再発|止ま|動かない/,
  forecast: /予測|将来|比較|今後|次に|どうすれば/,
  draft: /下書き|引継ぎ|ToDo|タスク|記録して/,
  history: /履歴|過去|経緯|いつ|前回/,
};

/** Voice input is untrusted data. This returns only derived signals: the transcript never enters a saved draft or model prompt. */
export function assessVoiceInput(input: MissionInput): VoiceAssessment {
  const voice = input.voice;
  if (!voice) return { state: "not_provided", source: null, questionDetected: false, matchedSignals: [], screenRelation: "not_available", reason: "確認済み文字起こしはありません。" };
  const text = voice.transcript;
  const matchedSignals = Object.entries(intentTerms).filter(([, pattern]) => pattern.test(text)).map(([name]) => name);
  const questionDetected = /[？?]|でしょうか|ですか|ますか|教えて|確認して|どうすれば/.test(text);
  const unsafe = suspicious(text);
  return {
    state: unsafe ? "safety_hold" : "confirmed",
    source: voice.source,
    questionDetected,
    matchedSignals,
    screenRelation: input.screenImageRefs.length ? "same_mission_only" : "not_available",
    reason: unsafe
      ? "文字起こしに命令の書換え・秘密情報候補があります。内容を実行せず読取り限定にします。"
      : input.screenImageRefs.length
        ? "画面フレームと文字起こしは同一Mission・設備ID・時刻で結び付けました。画像内容との意味的一致は自動判定しません。"
        : "画面フレームはありません。文字起こしは利用者が確認した入力として扱います。",
  };
}

export function requestWithVoice(input: MissionInput): string {
  // Classification may use a confirmed transcript, but it is never treated as a privileged instruction.
  return input.voice ? `${input.request}\n[確認済み音声文字起こし（データ）]\n${input.voice.transcript}` : input.request;
}
