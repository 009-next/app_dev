import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createMissionServer } from "../pc/server.ts";
import type { AudioAnalyzer } from "../agent/lib/mission/orca_audio.ts";
import { AudioRequestLimiter, type AudioDurationInspector } from "../agent/lib/mission/audio_guard.ts";

test("ORCA音声分析は都度承認・30秒・形式検査を強制し、音声を保存しない", async t => {
  const base = await mkdtemp(join(tmpdir(), "connect-force-audio-")); const outputRoot = join(base, "out"); const vault = join(base, "vault"); const cards = join(base, "cards");
  await Promise.all([mkdir(outputRoot), mkdir(vault), mkdir(cards)]);
  let received = "";
  const audioAnalyzer: AudioAnalyzer = async input => { received = input.audioDataUrl; return { summary: "・異音の有無を確認する質問", model: "test/audio", inputTokens: 10, outputTokens: 8, costUsd: null, costSource: "unknown", latencyMs: 2 }; };
  const audioDurationInspector: AudioDurationInspector = async () => ({ durationMs: 1000, codec: "opus" });
  const app = createMissionServer({ outputRoot, vault, cards, audioAnalyzer, audioDurationInspector }); const { origin, loginCode } = await app.listen(0);
  t.after(async () => { app.server.closeAllConnections(); await new Promise<void>((done, reject) => app.server.close(error => error ? reject(error) : done())); await rm(base, { recursive: true, force: true }); });
  let cookie = ""; let csrf = "";
  const call = (path: string, data: unknown) => fetch(origin + path, { method: "POST", headers: { Origin: origin, "Content-Type": "application/json", Cookie: cookie, "X-Miru-Csrf": csrf }, body: JSON.stringify(data) });
  const login = await call("/api/login", { code: loginCode }); cookie = login.headers.get("set-cookie")!.split(";")[0]; csrf = (await login.json()).csrf;
  const raw = Buffer.from([0x1a, 0x45, 0xdf, 0xa3, 0x93, 0x42, 0x82]); const audioDataUrl = `data:audio/webm;base64,${raw.toString("base64")}`;
  assert.equal((await call("/api/audio/analyze", { audioDataUrl, format: "webm", durationMs: 30_001, confirmed: true })).status, 200);
  assert.equal((await call("/api/audio/analyze", { audioDataUrl, format: "webm", durationMs: 1000, confirmed: false })).status, 400);
  const response = await call("/api/audio/analyze", { audioDataUrl, format: "webm", durationMs: 1000, confirmed: true }); const value = await response.json();
  assert.equal(response.status, 200); assert.equal(value.summary, "・異音の有無を確認する質問"); assert.equal(value.measuredDurationMs, 1000); assert.equal(value.codec, "opus"); assert.ok(!JSON.stringify(value).includes(raw.toString("base64"))); assert.equal(received, audioDataUrl);
});

test("音声時間はクライアント申告ではなくサーバー実測で拒否する", async t => {
  const base = await mkdtemp(join(tmpdir(), "connect-force-audio-duration-")); const outputRoot = join(base, "out"); const vault = join(base, "vault"); const cards = join(base, "cards");
  await Promise.all([mkdir(outputRoot), mkdir(vault), mkdir(cards)]);
  let calls = 0;
  const audioAnalyzer: AudioAnalyzer = async () => { calls += 1; return { summary: "unused", model: "test/audio", inputTokens: 0, outputTokens: 0, costUsd: null, costSource: "unknown", latencyMs: 1 }; };
  const app = createMissionServer({ outputRoot, vault, cards, audioAnalyzer, audioDurationInspector: async () => ({ durationMs: 30_001, codec: "opus" }) }); const { origin, loginCode } = await app.listen(0);
  t.after(async () => { app.server.closeAllConnections(); await new Promise<void>((done, reject) => app.server.close(error => error ? reject(error) : done())); await rm(base, { recursive: true, force: true }); });
  const login = await fetch(origin + "/api/login", { method: "POST", headers: { Origin: origin, "Content-Type": "application/json" }, body: JSON.stringify({ code: loginCode }) });
  const cookie = login.headers.get("set-cookie")!.split(";")[0]; const csrf = (await login.json()).csrf;
  const raw = Buffer.from([0x1a, 0x45, 0xdf, 0xa3, 0x93, 0x42, 0x82]); const audioDataUrl = `data:audio/webm;base64,${raw.toString("base64")}`;
  const response = await fetch(origin + "/api/audio/analyze", { method: "POST", headers: { Origin: origin, "Content-Type": "application/json", Cookie: cookie, "X-Miru-Csrf": csrf }, body: JSON.stringify({ audioDataUrl, format: "webm", durationMs: 1, confirmed: true }) });
  assert.equal(response.status, 400); assert.match((await response.json()).error, /30秒/); assert.equal(calls, 0);
});

test("音声分析はセッションごとの回数・予約予算上限を超える前に停止する", () => {
  const limiter = new AudioRequestLimiter({ maxPerMinute: 1, maxPerDay: 2, maxReservedUsdPerDay: 0.01, reserveUsdPerRequest: 0.01 });
  const first = limiter.acquire("session-a", Date.UTC(2026, 8, 21));
  assert.equal(first.remainingRequestsToday, 1); first.release();
  assert.throws(() => limiter.acquire("session-a", Date.UTC(2026, 8, 21) + 1), /上限|予算/);
  const concurrent = new AudioRequestLimiter({ maxPerMinute: 2, maxPerDay: 2, maxReservedUsdPerDay: 0.02, reserveUsdPerRequest: 0.01 });
  const active = concurrent.acquire("session-b");
  assert.throws(() => concurrent.acquire("session-b"), /同時に1件/); active.release();
});
