import assert from "node:assert/strict";
import test from "node:test";
import { analyzeMission } from "../agent/lib/mission/analyze.ts";

const screenContext = (signals: ("anomaly" | "forecast" | "draft" | "history" | "normal")[]) => ({
  observedSignals: signals,
  capturedAt: "2026-09-21T12:00:00.000Z",
  capturedVia: "window" as const,
  confirmed: true as const,
});

test("意味統合は確認済みの画面・会話タグ一致を根拠として記録する", async () => {
  const report = await analyzeMission({
    objectId: "obj_pump01", source: "demo", request: "異音の確認事項を整理", budgetUsd: 0,
    beforeImageRefs: [], afterImageRefs: [], screenImageRefs: ["masked://screen/safe-frame"],
    screenContext: screenContext(["anomaly", "draft"]),
    voice: { transcript: "異音があるため、次に何を確認すればよいですか？", source: "manual", confirmed: true },
  }, {}, new Date("2026-09-21T12:05:00.000Z"));
  assert.equal(report.semantic.state, "aligned");
  assert.deepEqual(report.semantic.sharedSignals, ["anomaly"]);
  assert.ok(report.decisionTrace.some(row => row.stage === "semantic" && row.selected === "aligned"));
  assert.equal(report.status, "ready");
});

test("画面の正常表示と会話の異常申告が矛盾した場合は読取り専用へ安全停止する", async () => {
  const report = await analyzeMission({
    objectId: "obj_pump01", source: "demo", request: "異音の確認事項を整理", budgetUsd: 0,
    beforeImageRefs: [], afterImageRefs: [], screenImageRefs: ["masked://screen/safe-frame"],
    screenContext: screenContext(["normal"]),
    voice: { transcript: "異音があり、確認してください。", source: "manual", confirmed: true },
  }, {}, new Date("2026-09-21T12:05:00.000Z"));
  assert.equal(report.semantic.state, "conflict");
  assert.equal(report.status, "read_only");
  assert.ok(report.alerts.some(alert => alert.rule === "semantic_conflict" && alert.severity === "high"));
  assert.equal(report.actions.length, 0);
});

test("画面意味タグは検査済み画面フレームなしでは受け付けない", async () => {
  await assert.rejects(() => analyzeMission({
    objectId: "obj_pump01", source: "demo", request: "履歴を確認", budgetUsd: 0,
    beforeImageRefs: [], afterImageRefs: [], screenImageRefs: [], screenContext: screenContext(["history"]),
  }), /画面の意味タグ/);
});
