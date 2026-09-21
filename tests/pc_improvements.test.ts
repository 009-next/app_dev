import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import sharp from "sharp";
import { createMissionServer } from "../pc/server.ts";
import type { MissionEnhancer } from "../agent/lib/mission/enhance.ts";

test("PC改良E2E: マスキング画像・後追いAI・判断マップ・定期候補・承認保存", async t => {
  const base = await mkdtemp(join(tmpdir(), "connect-force-p1-"));
  const outputRoot = join(base, "out"); const vault = join(base, "vault"); const cards = join(base, "cards");
  await Promise.all([mkdir(outputRoot), mkdir(vault), mkdir(cards)]);
  const enhancer: MissionEnhancer = async () => ({
    summary: "再発記録と期限超過を確認し、責任者が現在状態を確認してください。",
    model: "test/low-cost", inputTokens: 24, outputTokens: 18, costUsd: 0, costSource: "reported", latencyMs: 3,
  });
  const app = createMissionServer({ outputRoot, vault, cards, enhancer });
  const { origin, loginCode } = await app.listen(0);
  t.after(async () => {
    app.server.closeAllConnections();
    await new Promise<void>((done, reject) => app.server.close(error => error ? reject(error) : done()));
    assert.ok(base.startsWith(join(tmpdir(), "connect-force-p1-")));
    await rm(base, { recursive: true, force: true });
  });
  let cookie = ""; let csrf = "";
  const call = (path: string, data?: unknown) => fetch(origin + path, {
    method: data === undefined ? "GET" : "POST",
    headers: { Origin: origin, "Content-Type": "application/json", Cookie: cookie, "X-Miru-Csrf": csrf },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const login = await call("/api/login", { code: loginCode });
  assert.equal(login.headers.get("permissions-policy"), "camera=(), microphone=(self), display-capture=(self)");
  cookie = login.headers.get("set-cookie")!.split(";")[0]; csrf = (await login.json()).csrf;

  const raw = await sharp({ create: { width: 64, height: 48, channels: 3, background: { r: 20, g: 80, b: 120 } } })
    .withMetadata({ orientation: 6 }).jpeg().toBuffer();
  const dataUrl = `data:image/jpeg;base64,${raw.toString("base64")}`;
  assert.equal((await call("/api/images", { objectId: "obj_pump01", role: "before", confirmed: false, dataUrl, extraMasks: [] })).status, 400);
  const before = await (await call("/api/images", { objectId: "obj_pump01", role: "before", confirmed: true, dataUrl, extraMasks: [{ x: 0.1, y: 0.1, width: 0.3, height: 0.3 }] })).json();
  const after = await (await call("/api/images", { objectId: "obj_pump01", role: "after", confirmed: true, dataUrl, extraMasks: [] })).json();
  const screen = await (await call("/api/images", { objectId: "obj_pump01", role: "screen", confirmed: true, dataUrl, extraMasks: [{ x: 0, y: 0, width: 0.2, height: 0.2 }] })).json();
  assert.match(before.ref, /^masked:\/\/before\//); assert.match(after.ref, /^masked:\/\/after\//); assert.match(screen.ref, /^masked:\/\/screen\//);

  const screenOnlyImage = await (await call("/api/images", { objectId: "obj_screen01", role: "screen", confirmed: true, dataUrl, extraMasks: [] })).json();
  const screenOnlyMission = await call("/api/missions", { objectId: "obj_screen01", source: "demo", request: "画面フレームを参考資料として履歴を確認", budgetUsd: 0,
    beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [screenOnlyImage.ref] });
  assert.equal(screenOnlyMission.status, 200);
  const screenOnlyView = await screenOnlyMission.json();
  assert.equal(screenOnlyView.report.input.screenImageRefs.length, 1);
  assert.ok(screenOnlyView.report.decisionTrace.some((row: { stage: string; selected: string }) => row.stage === "media" && row.selected === "attach-masked-frame"));

  const created = await call("/api/missions", { objectId: "obj_pump01", source: "demo", request: "今後の対応を比較して下書きを作成", budgetUsd: 0.1,
    beforeImageRefs: [before.ref], afterImageRefs: [after.ref], screenImageRefs: [screen.ref], screenContext: { observedSignals: ["forecast", "draft"], capturedAt: "2026-09-21T12:00:00.000Z", capturedVia: "window", confirmed: true }, voice: { transcript: "点検後に何を引き継げばよいですか？", source: "manual", confirmed: true } });
  assert.equal(created.status, 200);
  const initial = await created.json(); const missionId = initial.report.missionId;
  assert.equal(initial.enhancement.status, "queued");
  assert.equal(initial.report.voice.state, "confirmed");
  assert.equal(initial.report.voice.screenRelation, "same_mission_only");
  assert.equal(initial.report.semantic.state, "screen_only");
  await new Promise(resolve => setTimeout(resolve, 20));
  const view = await (await call(`/api/missions/${missionId}`)).json();
  assert.equal(view.enhancement.status, "complete");
  assert.ok(view.report.decisionTrace.some((row: { stage: string; model: string }) => row.stage === "model" && row.model === "test/low-cost"));
  assert.equal(view.report.cost.localModelCalls, 1);

  const periodic = await (await call("/api/periodic", { limit: 20 })).json();
  assert.equal(periodic.sent, 0);
  assert.ok(periodic.drafts.length >= 1);

  const prepared = await (await call(`/api/missions/${missionId}/prepare`, view.approvals[0])).json();
  const saved = await call("/api/approve", { approvalId: prepared.approvalId, decision: "approve" });
  assert.equal(saved.status, 200);
  assert.equal((await readdir(join(outputRoot, "images"))).length, 3);
  const draftFile = (await readdir(outputRoot)).find(name => name.endsWith(".json"))!;
  const draft = JSON.parse(await readFile(join(outputRoot, draftFile), "utf8"));
  assert.equal(draft.visibility, "private");
  assert.equal(draft.images.localPaths.length, 3);
  assert.deepEqual(draft.images.screen, [screen.ref]);
  assert.ok(!JSON.stringify(draft).includes("data:image"));
  assert.ok(!JSON.stringify(draft).includes("点検後に何を引き継げばよいですか"));
  assert.equal("screenContext" in draft, false);
  assert.ok(!JSON.stringify(draft).includes("2026-09-21T12:00:00.000Z"));
});
