import { defineTool } from "eve/tools";
import { z } from "zod";
import { estimateTokenCost } from "../../../lib/cost.ts";

export default defineTool({
  description: "モデルの公開単価を入力として、カード1件の費用と出力1トークン当たり費用を決定的に計算する。",
  inputSchema: z.object({
    inputTokens: z.number().int().min(0).max(10_000_000),
    outputTokens: z.number().int().min(0).max(10_000_000),
    inputUsdPerMillion: z.number().min(0).max(10_000),
    outputUsdPerMillion: z.number().min(0).max(10_000),
    cachedInputTokens: z.number().int().min(0).max(10_000_000).optional(),
    cachedInputUsdPerMillion: z.number().min(0).max(10_000).optional(),
  }),
  execute(input) {
    return estimateTokenCost(input);
  },
});
