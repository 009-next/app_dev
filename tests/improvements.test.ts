import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import sharp from "sharp";
import { processMaskedImage } from "../agent/lib/images.ts";
import { buildMission, routeRequest } from "../agent/lib/mission/analyze.ts";
import { runTieredEnhancement, type MissionEnhancer } from "../agent/lib/mission/enhance.ts";
import { buildPeriodicDrafts } from "../agent/lib/mission/periodic.ts";
import { demoEvidence } from "../agent/lib/mission/evidence.ts";

test("自律性25シナリオ: 目的・危険度・禁止書込みを再現可能に判定する", async () => {
  const cases = JSON.parse(await readFile(new URL("../evals/autonomy/scenarios.json", import.meta.url), "utf8")) as Array<{ id: string; request: string; intent: string; risk: string }>;
  assert.equal(cases.length, 25);
  for (const item of cases) {
    const route = routeRequest(item.request);
    assert.equal(route.intent, item.intent, item.id);
    assert.equal(route.risk, item.risk, item.id);
    assert.ok(route.allowedOperations.includes("read_history"), item.id);
    assert.ok(!route.allowedOperations.includes("save_action_draft"), item.id);
  }
});

test("画像を再エンコードし、EXIFを除去して追加ぼかしを適用する", async () => {
  const raw = await sharp({ create: { width: 120, height: 80, channels: 3, background: { r: 200, g: 20, b: 20 } } })
    .withMetadata({ orientation: 6 }).jpeg().toBuffer();
  assert.ok((await sharp(raw).metadata()).exif);
  const result = await processMaskedImage(raw, [{ x: 0.1, y: 0.1, width: 0.4, height: 0.4 }]);
  const metadata = await sharp(result.bytes).metadata();
  assert.equal(metadata.format, "jpeg");
  assert.equal(metadata.exif, undefined);
  assert.equal(metadata.icc, undefined);
  assert.equal(result.sha256.length, 64);
});

test("段階切替は一次検証失敗時だけ明示設定済みfallbackを使う", async () => {
  const report = buildMission({ objectId: "obj_demo", request: "今後を比較", source: "demo", budgetUsd: 0.1, beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [] }, demoEvidence("obj_demo", new Date("2026-09-20T00:00:00Z")), new Date("2026-09-20T00:00:00Z"));
  const primary: MissionEnhancer = async () => { throw new Error("invalid structure"); };
  const fallback: MissionEnhancer = async () => ({ summary: "根拠と現在状態を責任者が確認してください。", model: "configured-high", inputTokens: 10, outputTokens: 8, costUsd: 0.01, costSource: "reported", latencyMs: 5 });
  const result = await runTieredEnhancement(report, primary, fallback);
  assert.equal(result.fallbackUsed, true);
  assert.equal(result.primaryError, "invalid structure");
  assert.equal(result.result.model, "configured-high");
});

test("定期確認は対象を絞り、最大20件の通知下書きだけを作る", () => {
  const now = new Date("2026-09-20T00:00:00Z");
  const reports = Array.from({ length: 25 }, (_, index) => buildMission(
    { objectId: `obj_${String(index).padStart(3, "0")}`, request: "期限超過と再発を確認", source: "demo", budgetUsd: 0, beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [] },
    demoEvidence("obj_pump01", now), now,
  ));
  const drafts = buildPeriodicDrafts(reports, 20);
  assert.ok(drafts.length <= 20);
  assert.ok(drafts.every(draft => draft.body.includes("通知は送信していません")));
});
