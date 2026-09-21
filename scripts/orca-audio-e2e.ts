import { mkdtemp, mkdir, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { extname, join, resolve } from "node:path";
import { createMissionServer } from "../pc/server.ts";

const live = process.argv.includes("--yes") && process.env.CONNECT_FORCE_RUN_LIVE_AUDIO_EVALS === "yes";
const model = process.env.ORCA_AUDIO_MODEL ?? "google/gemini-2.5-flash";
const calls = 3;
const maxOutputTokens = 64;
// Conservative preflight bound: 20k input tokens per 1-second test clip plus 64 output tokens.
// Current published Gemini 2.5 Flash list prices: input $0.30/M, output $2.50/M.
const estimatedCeilingUsd = calls * ((20_000 * 0.30 + maxOutputTokens * 2.50) / 1_000_000);

async function testAudioFromFile() {
  const configured = process.env.CONNECT_FORCE_AUDIO_E2E_FILE?.trim();
  if (!configured) throw new Error("CONNECT_FORCE_AUDIO_E2E_FILEに、音声分析フォルダ内の匿名化済み音声ファイルを指定してください。");
  const path = resolve(configured); const format = extname(path).slice(1).toLowerCase();
  if (!["webm", "ogg", "wav", "mp3", "m4a"].includes(format)) throw new Error("E2E音声はwebm/ogg/wav/mp3/m4aだけを指定できます。");
  const info = await stat(path); if (!info.isFile() || info.size < 1 || info.size > 3_900_000) throw new Error("E2E音声は3.9MB以下の通常ファイルだけを指定できます。");
  if (format !== "m4a") return { bytes: await readFile(path), format: format as "webm" | "ogg" | "wav" | "mp3", filename: path.split(/[\\/]/).at(-1)! };
  const mp3 = await new Promise<Buffer>((resolve, reject) => {
    const child = spawn(process.env.FFMPEG_PATH?.trim() || "ffmpeg", ["-v", "error", "-nostdin", "-i", path, "-map", "0:a:0", "-vn", "-t", "30", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    const chunks: Buffer[] = []; const errors: Buffer[] = []; let size = 0;
    child.stdout.on("data", chunk => { size += chunk.length; if (size > 3_900_000) { child.kill(); reject(new Error("変換後のMP3が3.9MBを超えました。")); } else chunks.push(Buffer.from(chunk)); });
    child.stderr.on("data", chunk => errors.push(Buffer.from(chunk)));
    child.on("error", () => reject(new Error("ffmpegが利用できないため、M4Aを安全なMP3へ変換できません。")));
    child.on("close", code => code === 0 && chunks.length ? resolve(Buffer.concat(chunks)) : reject(new Error(`M4Aのメモリ内変換に失敗しました: ${Buffer.concat(errors).toString("utf8").slice(0, 160)}`)));
  });
  return { bytes: mp3, format: "mp3" as const, filename: path.split(/[\\/]/).at(-1)! };
}

if (!live) {
  console.log(JSON.stringify({ dryRun: true, calls, model, maxOutputTokens, estimatedCeilingUsd, capUsd: 2, requirements: ["--yes", "CONNECT_FORCE_RUN_LIVE_AUDIO_EVALS=yes", "CONNECT_FORCE_ENABLE_AUDIO_SEND=yes", "ORCAROUTER_API_KEY", "CONNECT_FORCE_AUDIO_E2E_FILE=<音声分析フォルダ内の匿名化済み音声>", "organization approval"], note: "音声分析フォルダ内の匿名化済み音声だけを使い、外部送信と費用発生を防ぐため二重ゲート時だけ実行します。" }, null, 2));
  process.exit(0);
}
if (estimatedCeilingUsd > 2) throw new Error(`事前見積り$${estimatedCeilingUsd.toFixed(4)}が上限$2を超えるため、実行しません。`);
if (!process.env.ORCAROUTER_API_KEY?.trim() || process.env.CONNECT_FORCE_ENABLE_AUDIO_SEND !== "yes") throw new Error("ORCAROUTER_API_KEYとCONNECT_FORCE_ENABLE_AUDIO_SEND=yesが必要です。");
process.env.CONNECT_FORCE_AUDIO_MAX_OUTPUT_TOKENS = String(maxOutputTokens);
process.env.CONNECT_FORCE_AUDIO_MAX_REQUESTS_PER_MINUTE = String(calls);
process.env.CONNECT_FORCE_AUDIO_MAX_REQUESTS_PER_DAY = String(calls);
process.env.CONNECT_FORCE_AUDIO_RESERVE_USD_PER_REQUEST = "0.01";
process.env.CONNECT_FORCE_AUDIO_MAX_RESERVED_USD_PER_DAY = "0.03";

const base = await mkdtemp(join(tmpdir(), "connect-force-orca-audio-e2e-"));
const outputRoot = join(base, "out"); const vault = join(base, "vault"); const cards = join(base, "cards");
await Promise.all([mkdir(outputRoot), mkdir(vault), mkdir(cards)]);
const app = createMissionServer({ outputRoot, vault, cards });
try {
  const { origin, loginCode } = await app.listen(0);
  const login = await fetch(origin + "/api/login", { method: "POST", headers: { Origin: origin, "content-type": "application/json" }, body: JSON.stringify({ code: loginCode }) });
  if (!login.ok) throw new Error(`ログイン準備に失敗しました（HTTP ${login.status}）。`);
  const cookie = login.headers.get("set-cookie")!.split(";")[0]; const csrf = (await login.json() as { csrf: string }).csrf;
  const audio = await testAudioFromFile(); const audioDataUrl = `data:audio/${audio.format === "mp3" ? "mpeg" : audio.format};base64,${audio.bytes.toString("base64")}`;
  const results: { run: number; status: number; measuredDurationMs?: number; inputTokens?: number | null; outputTokens?: number | null; estimatedUsd?: number | null; costSource?: string; latencyMs?: number; error?: string }[] = [];
  for (let run = 1; run <= calls; run += 1) {
    const started = Date.now();
    const response = await fetch(origin + "/api/audio/analyze", { method: "POST", headers: { Origin: origin, "content-type": "application/json", Cookie: cookie, "x-miru-csrf": csrf }, body: JSON.stringify({ audioDataUrl, format: audio.format, durationMs: 1, confirmed: true }) });
    const payload = await response.json() as { measuredDurationMs?: number; inputTokens?: number | null; outputTokens?: number | null; costSource?: string; error?: string };
    const estimatedUsd = typeof payload.inputTokens === "number" && typeof payload.outputTokens === "number"
      ? (payload.inputTokens * 0.30 + payload.outputTokens * 2.50) / 1_000_000 : null;
    results.push({ run, status: response.status, measuredDurationMs: payload.measuredDurationMs, inputTokens: payload.inputTokens, outputTokens: payload.outputTokens, estimatedUsd, costSource: payload.costSource, latencyMs: Date.now() - started, error: payload.error });
    if (!response.ok) break;
  }
  const successful = results.filter(result => result.status === 200);
  const totalEstimatedUsd = successful.every(result => result.estimatedUsd !== null) ? successful.reduce((sum, result) => sum + result.estimatedUsd!, 0) : null;
  const pass = successful.length === calls && successful.every(result => (result.measuredDurationMs ?? 0) > 0 && (result.measuredDurationMs ?? 0) <= 30_000) && (totalEstimatedUsd === null || totalEstimatedUsd <= 2);
  console.log(JSON.stringify({ dryRun: false, pass, model, audioFilename: audio.filename, audioFormat: audio.format, calls, maxOutputTokens, estimatedCeilingUsd, totalEstimatedUsd, results, retentionCheck: "指定音声の内容、API応答本文、キーはファイル・ログへ保存していません。提供者側の保持は別途確認してください。" }, null, 2));
  if (!pass) process.exitCode = 1;
} finally {
  app.server.closeAllConnections(); await new Promise<void>(done => app.server.close(() => done()));
  await rm(base, { recursive: true, force: true });
}
import { spawn } from "node:child_process";
