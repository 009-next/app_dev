import { spawn } from "node:child_process";

export type AudioDurationInspection = { durationMs: number; codec: string };
export type AudioDurationInspector = (bytes: Buffer, format: "webm" | "ogg" | "wav" | "mp3") => Promise<AudioDurationInspection>;

export class AudioGateError extends Error {
  readonly status = 429;
}

const positiveInteger = (value: string | undefined, fallback: number) => {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 1000 ? parsed : fallback;
};
const positiveUsd = (value: string | undefined, fallback: number) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 && parsed <= 1 ? parsed : fallback;
};

type SessionLimit = { active: number; minute: number[]; day: string; dayCount: number; reservedUsd: number };
export type AudioRequestLimiterOptions = { maxPerMinute: number; maxPerDay: number; maxReservedUsdPerDay: number; reserveUsdPerRequest: number };

/** Bounds external attempts even when a provider does not return a reliable invoice. */
export class AudioRequestLimiter {
  private readonly sessions = new Map<string, SessionLimit>();
  private readonly options: AudioRequestLimiterOptions;
  constructor(options: AudioRequestLimiterOptions = {
    maxPerMinute: positiveInteger(process.env.CONNECT_FORCE_AUDIO_MAX_REQUESTS_PER_MINUTE, 2),
    maxPerDay: positiveInteger(process.env.CONNECT_FORCE_AUDIO_MAX_REQUESTS_PER_DAY, 6),
    maxReservedUsdPerDay: positiveUsd(process.env.CONNECT_FORCE_AUDIO_MAX_RESERVED_USD_PER_DAY, 0.06),
    reserveUsdPerRequest: positiveUsd(process.env.CONNECT_FORCE_AUDIO_RESERVE_USD_PER_REQUEST, 0.01),
  }) { this.options = options; }

  acquire(sessionId: string, now = Date.now()) {
    const minuteFloor = now - 60_000;
    const day = new Date(now).toISOString().slice(0, 10);
    const entry = this.sessions.get(sessionId) ?? { active: 0, minute: [], day, dayCount: 0, reservedUsd: 0 };
    if (entry.day !== day) { entry.day = day; entry.dayCount = 0; entry.reservedUsd = 0; }
    entry.minute = entry.minute.filter(at => at > minuteFloor);
    if (entry.active >= 1) throw new AudioGateError("音声分析は同じ作業室で同時に1件だけ実行できます。");
    if (entry.minute.length >= this.options.maxPerMinute) throw new AudioGateError("音声分析の1分あたり上限に達しました。時間をおいて再実行してください。");
    if (entry.dayCount >= this.options.maxPerDay) throw new AudioGateError("音声分析の当日上限に達しました。合成デモまたは確認済み文字起こしを利用してください。");
    if (entry.reservedUsd + this.options.reserveUsdPerRequest > this.options.maxReservedUsdPerDay) throw new AudioGateError("音声分析の当日予約予算に達しました。外部送信を停止します。");
    entry.active += 1; entry.minute.push(now); entry.dayCount += 1; entry.reservedUsd += this.options.reserveUsdPerRequest; this.sessions.set(sessionId, entry);
    return { reservedUsd: this.options.reserveUsdPerRequest, remainingRequestsToday: Math.max(0, this.options.maxPerDay - entry.dayCount), release: () => {
      const current = this.sessions.get(sessionId); if (current) current.active = Math.max(0, current.active - 1);
    } };
  }

  delete(sessionId: string) { this.sessions.delete(sessionId); }
}

export function assertAudioDuration(inspection: AudioDurationInspection, maxMs = 30_000) {
  if (!Number.isFinite(inspection.durationMs) || inspection.durationMs <= 0) throw new Error("音声の実時間を確認できません。外部送信を停止しました。");
  if (inspection.durationMs > maxMs) throw new Error(`音声の実時間が${maxMs / 1000}秒を超えています。外部送信を停止しました。`);
  if (!/^[a-zA-Z0-9._-]{1,80}$/.test(inspection.codec)) throw new Error("音声codecを確認できません。外部送信を停止しました。");
}

/** Decodes stdin to a null output. It never writes the audio file to disk. */
export function createFfmpegDurationInspector(command = process.env.FFMPEG_PATH?.trim() || "ffmpeg"): AudioDurationInspector {
  return async bytes => new Promise<AudioDurationInspection>((resolve, reject) => {
    const child = spawn(command, ["-v", "error", "-nostats", "-i", "pipe:0", "-f", "null", "-", "-progress", "pipe:2"], { stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    const stderr: Buffer[] = []; let size = 0; let settled = false;
    const finish = (error?: Error, value?: AudioDurationInspection) => {
      if (settled) return; settled = true; clearTimeout(timer); if (error) reject(error); else resolve(value!);
    };
    const timer = setTimeout(() => { child.kill(); finish(new Error("音声の実時間検査が時間切れになりました。外部送信を停止しました。")); }, 3_000);
    child.stderr.on("data", chunk => { size += chunk.length; if (size > 16_000) { child.kill(); finish(new Error("音声検査結果が大きすぎます。外部送信を停止しました。")); } else stderr.push(Buffer.from(chunk)); });
    child.on("error", () => finish(new Error("ffmpegが利用できないため、音声の外部送信を停止しました。FFMPEG_PATHを確認してください。")));
    child.on("close", code => {
      if (code !== 0) { finish(new Error("音声をデコードできないため、外部送信を停止しました。")); return; }
      try {
        const progress = Buffer.concat(stderr).toString("utf8");
        const matches = [...progress.matchAll(/^out_time_us=(\d+)$/gm)];
        const durationMs = Math.round(Number(matches.at(-1)?.[1]) / 1000);
        finish(undefined, { durationMs, codec: "decoded-audio" });
      } catch { finish(new Error("音声の実時間を確認できないため、外部送信を停止しました。")); }
    });
    child.stdin.end(bytes);
  });
}
