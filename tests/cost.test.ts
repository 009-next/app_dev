import assert from "node:assert/strict";
import test from "node:test";
import { estimateTokenCost } from "../agent/lib/cost.ts";

test("キャッシュ入力を分けて原価を計算する", () => {
  const result = estimateTokenCost({
    inputTokens: 1_000,
    outputTokens: 500,
    inputUsdPerMillion: 3,
    outputUsdPerMillion: 15,
    cachedInputTokens: 600,
    cachedInputUsdPerMillion: 0.3,
  });
  assert.equal(result.inputCostUsd, 0.0012);
  assert.equal(result.cachedInputCostUsd, 0.00018);
  assert.equal(result.outputCostUsd, 0.0075);
  assert.equal(result.totalUsd, 0.00888);
  assert.equal(result.costPerOutputTokenUsd, 0.00001776);
});

test("出力ゼロでは出力単位原価をnullにする", () => {
  const result = estimateTokenCost({
    inputTokens: 100,
    outputTokens: 0,
    inputUsdPerMillion: 1,
    outputUsdPerMillion: 1,
  });
  assert.equal(result.costPerOutputTokenUsd, null);
});
