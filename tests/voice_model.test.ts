import assert from "node:assert/strict";
import test from "node:test";
import { createClaudeMissionEnhancer, runEnhancementLadder, type MissionEnhancer } from "../agent/lib/mission/enhance.ts";
import { missionModelPlan } from "../agent/lib/mission/model_tiers.ts";
import { analyzeMission } from "../agent/lib/mission/analyze.ts";

test("モデルラダーは失敗した低コスト経路を記録し、明示された次段だけを使う", async () => {
  const report = await analyzeMission({ objectId: "obj_voice01", request: "異音の確認事項を整理", source: "demo", budgetUsd: 0.1,
    beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [], voice: { transcript: "次に何を確認すればよいですか？", source: "manual", confirmed: true } });
  const failed: MissionEnhancer = async () => { throw new Error("first tier unavailable"); };
  const succeeded: MissionEnhancer = async () => ({ summary: "現在状態と担当者を確認してください。", model: "test/haiku", inputTokens: 10, outputTokens: 8, costUsd: 0.00005, costSource: "estimated", latencyMs: 2 });
  const result = await runEnhancementLadder(report, [failed, succeeded]);
  assert.equal(result.attempts, 2); assert.equal(result.result.model, "test/haiku"); assert.match(result.errors[0]!, /first tier/);
});

test("有料モデルは明示設定なしでは計画上も無効", async () => {
  const saved = { orca: process.env.ORCAROUTER_API_KEY, paid: process.env.CONNECT_FORCE_ENABLE_PAID_FALLBACK, sonnet: process.env.CONNECT_FORCE_ENABLE_SONNET_ESCALATION };
  try {
    delete process.env.ORCAROUTER_API_KEY; process.env.CONNECT_FORCE_ENABLE_PAID_FALLBACK = "no"; process.env.CONNECT_FORCE_ENABLE_SONNET_ESCALATION = "no";
    const plan = missionModelPlan({ route: { intent: "forecast", risk: "medium", reasons: [], selectedRoles: [], allowedOperations: [], recommendedModel: "high-quality" },
      voice: { state: "confirmed", source: "manual", questionDetected: true, matchedSignals: ["forecast"], screenRelation: "not_available", reason: "test" } });
    assert.ok(plan.every(tier => !tier.enabled));
  } finally {
    if (saved.orca === undefined) delete process.env.ORCAROUTER_API_KEY; else process.env.ORCAROUTER_API_KEY = saved.orca;
    if (saved.paid === undefined) delete process.env.CONNECT_FORCE_ENABLE_PAID_FALLBACK; else process.env.CONNECT_FORCE_ENABLE_PAID_FALLBACK = saved.paid;
    if (saved.sonnet === undefined) delete process.env.CONNECT_FORCE_ENABLE_SONNET_ESCALATION; else process.env.CONNECT_FORCE_ENABLE_SONNET_ESCALATION = saved.sonnet;
  }
});

test("Claudeフォールバックには確認済み文字起こしの原文を送らず、見積原価を実費と区別する", async () => {
  const original = process.env.ANTHROPIC_API_KEY;
  try {
    process.env.ANTHROPIC_API_KEY = ["unit", "test", "key", "for", "claude", "adapter"].join("-");
    const transcript = "顧客の固有名を含む会話原文は外部モデルへ送らない";
    const report = await analyzeMission({ objectId: "obj_voice02", request: "次の確認を整理", source: "demo", budgetUsd: 0.1,
      beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [], voice: { transcript, source: "manual", confirmed: true } });
    let requestBody = "";
    const fakeFetch = async (_url: string | URL | Request, init?: RequestInit) => {
      requestBody = String(init?.body ?? "");
      return new Response(JSON.stringify({ model: "claude-haiku-4-5-20251001", content: [{ type: "text", text: "確認事項を整理しました。" }], usage: { input_tokens: 20, output_tokens: 10 } }), { status: 200 });
    };
    const enhancer = createClaudeMissionEnhancer("claude-haiku-4-5-20251001", fakeFetch as typeof fetch);
    assert.ok(enhancer);
    const result = await enhancer(report, new AbortController().signal);
    assert.equal(requestBody.includes(transcript), false);
    assert.equal(result.costSource, "estimated");
    assert.equal(result.costUsd, 0.00007);
  } finally {
    if (original === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = original;
  }
});
