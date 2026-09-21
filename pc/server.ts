import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { z } from "zod";
import { decodeImageDataUrl, maskRectSchema, processMaskedImage } from "../agent/lib/images.ts";
import { analyzeMission, assertOperation } from "../agent/lib/mission/analyze.ts";
import { configuredMissionEnhancers, runEnhancementLadder, type MissionEnhancer } from "../agent/lib/mission/enhance.ts";
import { buildPeriodicDrafts } from "../agent/lib/mission/periodic.ts";
import { PortkeyStore } from "../agent/lib/mission/portkey.ts";
import { actionDigest, saveMissionAction } from "../agent/lib/mission/save.ts";
import { missionInputSchema, type MissionReport } from "../agent/lib/mission/types.ts";
import { audioAnalysisInputSchema, createOrcaAudioAnalyzer, decodeAudioInput, type AudioAnalyzer } from "../agent/lib/mission/orca_audio.ts";
import { assertAudioDuration, AudioGateError, AudioRequestLimiter, createFfmpegDurationInspector, type AudioDurationInspector } from "../agent/lib/mission/audio_guard.ts";

type Session = { id: string; csrf: string; expiresAt: number };
type Approval = { id: string; owner: string; missionId: string; actionId: string; digest: string; operationId: string; expiresAt: number;
  state: "pending" | "saving" | "saved" | "rejected" | "failed"; result?: Awaited<ReturnType<typeof saveMissionAction>> };
type Enhancement = { status: "queued" | "complete" | "skipped" | "failed"; summary: string | null; reason: string };
type Room = { owner: string; report: MissionReport; audit: { at: string; event: string }[]; enhancement: Enhancement };
type StoredImage = { owner: string; objectId: string; role: "before" | "after" | "screen"; bytes: Buffer; createdAt: number; width: number; height: number; sha256: string };
const token = () => randomBytes(32).toString("base64url");
const equal = (a: string, b: string) => Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));

export function createMissionServer(options: { vault?: string; cards?: string; outputRoot?: string; enhancer?: MissionEnhancer | null; fallbackEnhancer?: MissionEnhancer; fallbackEnhancers?: MissionEnhancer[]; audioAnalyzer?: AudioAnalyzer | null; audioDurationInspector?: AudioDurationInspector; audioRequestLimiter?: AudioRequestLimiter } = {}) {
  let loginCode = token();
  const loginExpiresAt = Date.now() + 10 * 60000;
  const sessions = new Map<string, Session>();
  const rooms = new Map<string, Room>();
  const approvals = new Map<string, Approval>();
  const images = new Map<string, StoredImage>();
  const keys = new PortkeyStore();
  const configuredEnhancers = options.enhancer === undefined ? configuredMissionEnhancers() : [options.enhancer, ...(options.fallbackEnhancers ?? []), ...(options.fallbackEnhancer ? [options.fallbackEnhancer] : [])].filter((value): value is MissionEnhancer => Boolean(value));
  const audioAnalyzer = options.audioAnalyzer === undefined ? createOrcaAudioAnalyzer() : options.audioAnalyzer;
  const audioDurationInspector = options.audioDurationInspector ?? createFfmpegDurationInspector();
  const audioRequestLimiter = options.audioRequestLimiter ?? new AudioRequestLimiter();
  let origin = "";
  const uiRoot = fileURLToPath(new URL("./public/", import.meta.url));
  const reply = (res: ServerResponse, status: number, value: unknown) => {
    res.writeHead(status, { "content-type": "application/json; charset=utf-8" }); res.end(JSON.stringify(value));
  };
  async function body(req: IncomingMessage, maxBytes = 16000): Promise<unknown> {
    if (req.headers["content-type"]?.split(";")[0] !== "application/json") throw new Error("JSON形式で送信してください。");
    const chunks: Buffer[] = []; let size = 0;
    for await (const chunk of req) {
      const bytes = Buffer.from(chunk); size += bytes.length;
      if (size > maxBytes) throw new Error("入力が大きすぎます。");
      chunks.push(bytes);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  }
  function roomFor(id: string, owner: string) {
    const room = rooms.get(id);
    if (!room || room.owner !== owner) throw new Error("この作業室へアクセスできません。");
    assertOperation(room.report, "view_map"); return room;
  }
  function roomView(room: Room) {
    return { report: room.report, audit: room.audit, enhancement: room.enhancement,
      approvals: room.report.actions.map(a => ({ actionId: a.id, digest: actionDigest(room.report, a.id) })) };
  }
  function removeSessionImages(owner: string) {
    for (const [ref, image] of images) if (image.owner === owner) images.delete(ref);
  }
  function prune() {
    for (const [id, session] of sessions) if (session.expiresAt <= Date.now()) { sessions.delete(id); removeSessionImages(id); audioRequestLimiter.delete(id); }
    for (const [id, room] of rooms) if (!sessions.has(room.owner) || Date.parse(room.report.expiresAt) <= Date.now()) { rooms.delete(id); keys.revokeMission(id); }
    for (const [id, approval] of approvals) if (approval.expiresAt <= Date.now() || !rooms.has(approval.missionId)) approvals.delete(id);
    for (const [ref, image] of images) if (image.createdAt + 30 * 60000 <= Date.now()) images.delete(ref);
  }
  function scheduleEnhancement(room: Room) {
    if (!configuredEnhancers.length || room.report.input.budgetUsd <= 0) return;
    queueMicrotask(async () => {
      try {
        const { result, attempts, errors } = await runEnhancementLadder(room.report, configuredEnhancers);
        const fallbackUsed = attempts > 1;
        room.enhancement = { status: "complete", summary: result.summary, reason: fallbackUsed ? "前段の検証失敗後、明示設定された次段モデルを使用しました。" : "匿名化した判断メタデータを第1モデルで要約しました。" };
        room.report.cost.localModelCalls += attempts;
        room.report.cost.outerAgentCostUsd = result.costUsd;
        room.report.cost.explanation = `後追い要約: ${result.model} / 費用 ${result.costUsd === null ? "不明" : "$" + result.costUsd.toFixed(6)} / ${result.latencyMs}ms。ローカル解析はモデル0回です。`;
        room.report.decisionTrace.push({
          at: new Date().toISOString(), stage: fallbackUsed ? "fallback" : "model",
          options: fallbackUsed ? ["safe-stop", "configured-fallback"] : ["accept", "fallback", "safe-stop"],
          selected: fallbackUsed ? "configured-fallback" : "accept", tool: "mission_enhancer",
          reason: errors.join(" / ") || "構造・長さ検査を通過", evidenceIds: room.report.timeline.map(item => item.id),
          model: result.model, inputTokens: result.inputTokens, outputTokens: result.outputTokens,
          costUsd: result.costUsd, costSource: result.costSource, latencyMs: result.latencyMs,
        });
        room.audit.push({ at: new Date().toISOString(), event: "匿名化した判断メタデータの後追い要約が完了しました。" });
      } catch (error) {
        room.enhancement = { status: "failed", summary: null, reason: "AI要約を採用できませんでした。ローカル解析結果はそのまま利用できます。" };
        room.report.decisionTrace.push({
          at: new Date().toISOString(), stage: "fallback", options: ["configured-fallback", "safe-stop"], selected: "safe-stop",
          tool: "mission_enhancer", reason: error instanceof Error ? error.message.slice(0, 160) : "AI要約に失敗",
          evidenceIds: [], model: null, inputTokens: null, outputTokens: null, costUsd: null, costSource: "unknown", latencyMs: null,
        });
        room.audit.push({ at: new Date().toISOString(), event: "AI要約を安全停止しました。ローカル解析結果は保持しています。" });
      }
    });
  }
  const server = createServer(async (req, res) => {
    res.setHeader("Cache-Control", "no-store"); res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer"); res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
    res.setHeader("Permissions-Policy", "camera=(), microphone=(self), display-capture=(self)");
    try {
      prune();
      if (req.headers.host !== new URL(origin).host) { reply(res, 403, { error: "Hostが一致しません。" }); return; }
      if (req.method === "POST" && req.headers.origin !== origin) { reply(res, 403, { error: "Originが一致しません。" }); return; }
      const url = new URL(req.url ?? "/", origin);
      if (req.method === "GET" && url.pathname === "/api/health") { reply(res, 200, { status: "ok", version: "0.9.0", externalAudio: Boolean(audioAnalyzer) }); return; }
      if (req.method === "GET" && ["/", "/app.js", "/mask.js", "/screen-capture.js", "/voice-input.js", "/audio-analysis.js", "/style.css", "/enhancements.css"].includes(url.pathname)) {
        const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
        const content = await readFile(resolve(uiRoot, name));
        res.writeHead(200, { "content-type": name.endsWith("html") ? "text/html; charset=utf-8" : name.endsWith("js") ? "text/javascript; charset=utf-8" : "text/css; charset=utf-8" });
        res.end(content); return;
      }
      if (req.method === "POST" && url.pathname === "/api/login") {
        const input = z.object({ code: z.string().max(128) }).strict().parse(await body(req));
        if (!loginCode || Date.now() >= loginExpiresAt || !equal(input.code, loginCode)) { reply(res, 401, { error: "起動時のログインリンクは期限切れ、使用済み、または不正です。" }); return; }
        const session: Session = { id: token(), csrf: token(), expiresAt: Date.now() + 8 * 3600000 };
        sessions.set(session.id, session); loginCode = "";
        res.setHeader("Set-Cookie", `miru_session=${session.id}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800`);
        reply(res, 200, { csrf: session.csrf, mode: "local-rules", expiresAt: new Date(session.expiresAt).toISOString(), externalAudio: Boolean(audioAnalyzer) }); return;
      }
      const cookie = req.headers.cookie?.split(";").map(c => c.trim()).find(c => c.startsWith("miru_session="))?.slice(13);
      const session = cookie ? sessions.get(cookie) : undefined;
      if (!session) { reply(res, 401, { error: "PowerShellに表示された起動時ログインリンクから開いてください。" }); return; }
      if (req.method === "GET" && url.pathname === "/api/session") { reply(res, 200, { csrf: session.csrf, mode: "local-rules", expiresAt: new Date(session.expiresAt).toISOString(), externalAudio: Boolean(audioAnalyzer) }); return; }
      if (req.method === "POST" && req.headers["x-miru-csrf"] !== session.csrf) { reply(res, 403, { error: "確認トークンが一致しません。" }); return; }
      if (req.method === "POST" && url.pathname === "/api/logout") {
        sessions.delete(session.id); removeSessionImages(session.id); audioRequestLimiter.delete(session.id); prune(); res.setHeader("Set-Cookie", "miru_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0");
        reply(res, 200, { loggedOut: true }); return;
      }
      if (req.method === "POST" && url.pathname === "/api/audio/analyze") {
        if (!audioAnalyzer) throw new Error("ORCA ROUTERの音声分析は未設定です。キー、明示有効化、承認済みモデルを確認してください。");
        const input = audioAnalysisInputSchema.parse(await body(req, 5_500_000));
        const permit = audioRequestLimiter.acquire(session.id);
        try {
          const decoded = decodeAudioInput(input);
          const inspection = await audioDurationInspector(decoded.bytes, input.format);
          assertAudioDuration(inspection);
          const result = await audioAnalyzer(input);
          // Raw audio and the provider response are intentionally not retained in rooms, audits, drafts, or logs.
          reply(res, 200, { ...result, measuredDurationMs: inspection.durationMs, codec: inspection.codec, reservedCostCapUsd: permit.reservedUsd, remainingRequestsToday: permit.remainingRequestsToday }); return;
        } finally { permit.release(); }
      }
      if (req.method === "POST" && url.pathname === "/api/images") {
        if (images.size >= 200) throw new Error("画像の一時保存上限です。作業室を完了するか、再起動してください。");
        const input = z.object({
          objectId: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$/),
          role: z.enum(["before", "after", "screen"]),
          confirmed: z.literal(true),
          dataUrl: z.string().max(7_000_000),
          extraMasks: z.array(maskRectSchema).max(20).default([]),
        }).strict().parse(await body(req, 7_200_000));
        const processed = await processMaskedImage(decodeImageDataUrl(input.dataUrl), input.extraMasks);
        const ref = `masked://${input.role}/${token()}`;
        images.set(ref, { owner: session.id, objectId: input.objectId, role: input.role, bytes: processed.bytes,
          createdAt: Date.now(), width: processed.width, height: processed.height, sha256: processed.sha256 });
        reply(res, 200, { ref, width: processed.width, height: processed.height, sha256: processed.sha256 }); return;
      }
      if (req.method === "POST" && url.pathname === "/api/missions") {
        if (rooms.size >= 50) throw new Error("作業室数の上限です。時間をおいて再実行してください。");
        const input = missionInputSchema.parse(await body(req));
        const paired = [...input.beforeImageRefs, ...input.afterImageRefs];
        if (paired.length && (!input.beforeImageRefs.length || !input.afterImageRefs.length)) throw new Error("作業前・作業後の検査済み画像を両方登録してください。");
        const supplied = [...paired, ...input.screenImageRefs];
        for (const ref of supplied) {
          const image = images.get(ref);
          if (!image || image.owner !== session.id || image.objectId !== input.objectId) throw new Error("検査済み画像を確認できません。再登録してください。");
        }
        const report = await analyzeMission(input, options);
        const enhancement: Enhancement = configuredEnhancers.length && report.input.budgetUsd > 0
          ? { status: "queued", summary: null, reason: "ローカル結果を先に表示し、匿名化したAI要約を後追いしています。" }
          : { status: "skipped", summary: null, reason: "有効なモデル未設定または予算0のため、モデル0回のローカル解析だけを表示します。" };
        const room: Room = { owner: session.id, report, enhancement, audit: [{ at: report.createdAt, event: "ローカル分析完了。元資料は変更していません。" }] };
        rooms.set(report.missionId, room); reply(res, 200, roomView(room)); scheduleEnhancement(room); return;
      }
      if (req.method === "POST" && url.pathname === "/api/periodic") {
        const input = z.object({ limit: z.number().int().min(1).max(20).default(20) }).strict().parse(await body(req));
        const owned = [...rooms.values()].filter(room => room.owner === session.id && Date.parse(room.report.expiresAt) > Date.now());
        const drafts = buildPeriodicDrafts(owned.map(room => room.report), input.limit);
        for (const draft of drafts) {
          const room = rooms.get(draft.missionId)!;
          room.report.decisionTrace.push({ at: new Date().toISOString(), stage: "periodic", options: ["skip", "draft"], selected: "draft",
            tool: "periodic_review", reason: draft.reasonRules.join(" / "), evidenceIds: draft.evidenceIds, model: null,
            inputTokens: null, outputTokens: null, costUsd: 0, costSource: "not-applicable", latencyMs: 0 });
        }
        reply(res, 200, { drafts, sent: 0, note: "外部通知は送信していません。承認用の候補だけです。" }); return;
      }
      const match = url.pathname.match(/^\/api\/missions\/(mission_[a-z0-9-]+)(?:\/(prepare|portkey|revoke))?$/);
      if (match) {
        const room = roomFor(match[1], session.id);
        if (req.method === "GET" && !match[2]) { reply(res, 200, roomView(room)); return; }
        if (req.method === "POST" && match[2] === "prepare") {
          const input = z.object({ actionId: z.string().max(64), digest: z.string().length(64) }).strict().parse(await body(req));
          assertOperation(room.report, "save_action_draft");
          if (input.digest !== actionDigest(room.report, input.actionId)) throw new Error("下書きが変わりました。再確認してください。");
          if (approvals.size >= 100) throw new Error("承認要求数の上限です。");
          const approval: Approval = { id: token(), owner: session.id, missionId: room.report.missionId, actionId: input.actionId,
            digest: input.digest, operationId: `${room.report.missionId}_${input.actionId}`, expiresAt: Date.now() + 5 * 60000, state: "pending" };
          approvals.set(approval.id, approval); room.audit.push({ at: new Date().toISOString(), event: "承認要求を作成しました。" });
          reply(res, 200, { approvalId: approval.id, action: room.report.actions.find(a => a.id === input.actionId), destination: `data/mission-drafts/${approval.operationId}.json`, digest: approval.digest }); return;
        }
        if (req.method === "POST" && match[2] === "portkey") {
          const key = keys.issue(session.id, room.report.missionId); room.audit.push({ at: new Date().toISOString(), event: "5分間・一回限りの移動キーを発行しました。" });
          reply(res, 200, { url: `${origin}/#portkey=${key.token}`, expiresAt: key.expiresAt }); return;
        }
        if (req.method === "POST" && match[2] === "revoke") {
          keys.revokeMission(room.report.missionId); room.audit.push({ at: new Date().toISOString(), event: "移動キーを無効化しました。" }); reply(res, 200, { revoked: true }); return;
        }
      }
      if (req.method === "POST" && url.pathname === "/api/jump") {
        const input = z.object({ token: z.string().max(256) }).strict().parse(await body(req));
        const missionId = keys.consume(input.token, session.id); const room = roomFor(missionId, session.id);
        room.audit.push({ at: new Date().toISOString(), event: "ポートキーを使用して作業室へ移動しました。" }); reply(res, 200, roomView(room)); return;
      }
      if (req.method === "POST" && url.pathname === "/api/approve") {
        const input = z.object({ approvalId: z.string().max(128), decision: z.enum(["approve", "reject"]) }).strict().parse(await body(req));
        const approval = approvals.get(input.approvalId);
        if (!approval || approval.owner !== session.id || approval.expiresAt <= Date.now()) throw new Error("承認要求が期限切れまたは権限外です。");
        const room = roomFor(approval.missionId, session.id);
        if (approval.state === "saved" && input.decision === "approve") { reply(res, 200, approval.result); return; }
        if (approval.state !== "pending") throw new Error("この承認要求は処理済みです。");
        if (input.decision === "reject") { approval.state = "rejected"; room.audit.push({ at: new Date().toISOString(), event: "下書き保存を取り消しました。" }); reply(res, 200, { saved: false }); return; }
        approval.state = "saving";
        try {
          const result = await saveMissionAction(room.report, approval.actionId, approval.digest, approval.operationId, {
            ...options,
            resolveImage: async ref => {
              const image = images.get(ref);
              return image?.owner === session.id ? image.bytes : undefined;
            },
          });
          approval.state = "saved"; approval.result = result;
          room.report.steps = room.report.steps.map(step => step.id === "push" ? { ...step, status: "completed", detail: result.path } : step);
          room.audit.push({ at: new Date().toISOString(), event: `人の承認後に保存: ${result.path}` }); reply(res, 200, result); return;
        } catch (error) { approval.state = "failed"; room.audit.push({ at: new Date().toISOString(), event: "保存を中止しました。再分析または再確認が必要です。" }); throw error; }
      }
      reply(res, 404, { error: "見つかりません。" });
    } catch (error) {
      const known = error instanceof Error && !("code" in error) && !(error instanceof z.ZodError);
      reply(res, error instanceof AudioGateError ? error.status : 400, { error: known ? error.message : "入力または記録を確認できません。形式・権限・保存先を確認してください。" });
    }
  });
  server.requestTimeout = 15000; server.headersTimeout = 10000;
  return { server, async listen(port = 4317) {
    await new Promise<void>((ready, reject) => { server.once("error", reject); server.listen(port, "127.0.0.1", () => { server.removeListener("error", reject); ready(); }); });
    const address = server.address(); if (!address || typeof address === "string") throw new Error("起動に失敗しました。");
    origin = `http://127.0.0.1:${address.port}`; return { origin, loginUrl: `${origin}/#login=${loginCode}`, loginCode };
  } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  createMissionServer().listen(Number(process.env.MIRU_PC_PORT ?? "4317")).then(({ origin, loginUrl }) => {
    console.log(`Mission Room PCを起動しました。10分以内に次の一回限りのリンクを開いてください:\n${loginUrl}\n稼働確認: ${origin}/api/health\n終了: Ctrl+C`);
  }).catch(error => { console.error("PC画面を起動できません。ポートの競合を確認してください。", error.code ?? ""); process.exitCode = 1; });
}
