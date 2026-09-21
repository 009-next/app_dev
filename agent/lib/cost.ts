export type CostEstimateInput = {
  inputTokens: number;
  outputTokens: number;
  inputUsdPerMillion: number;
  outputUsdPerMillion: number;
  cachedInputTokens?: number;
  cachedInputUsdPerMillion?: number;
};

export function estimateTokenCost(input: CostEstimateInput) {
  const cached = Math.min(input.cachedInputTokens ?? 0, input.inputTokens);
  const uncached = input.inputTokens - cached;
  const inputCost = (uncached * input.inputUsdPerMillion) / 1_000_000;
  const cachedInputCost =
    (cached * (input.cachedInputUsdPerMillion ?? input.inputUsdPerMillion)) / 1_000_000;
  const outputCost = (input.outputTokens * input.outputUsdPerMillion) / 1_000_000;
  const totalUsd = inputCost + cachedInputCost + outputCost;

  return {
    inputCostUsd: Number(inputCost.toFixed(8)),
    cachedInputCostUsd: Number(cachedInputCost.toFixed(8)),
    outputCostUsd: Number(outputCost.toFixed(8)),
    totalUsd: Number(totalUsd.toFixed(8)),
    costPerOutputTokenUsd:
      input.outputTokens === 0 ? null : Number((totalUsd / input.outputTokens).toFixed(10)),
  };
}
