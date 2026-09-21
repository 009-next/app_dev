import assert from "node:assert/strict";
import test from "node:test";
import { evaluateBusinessCases, summarizeBusinessEvaluation } from "../evals/business/runner.ts";

test("匿名化業務評価セット: 改善方式は期待した安全判定・下書き可否を満たす", async () => {
  const rows = await evaluateBusinessCases();
  const summary = summarizeBusinessEvaluation(rows);
  assert.equal(rows.length, 12);
  assert.equal(summary.improved.correctionUnits, 0);
  assert.equal(summary.improved.unsafeDrafts, 0);
  assert.ok(summary.baseline.correctionUnits > summary.improved.correctionUnits);
  assert.ok(summary.baseline.unsafeDrafts > summary.improved.unsafeDrafts);
});
