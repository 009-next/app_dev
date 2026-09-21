import { defineAgent } from "eve";

export default defineAgent({
  defaultTools: false,
  description:
    "カード生成の専門担当。マスキング済み作業前後画像と音声メモを検査し、根拠付きカード下書きとコスト見積りを作る。",
  model: "anthropic/claude-sonnet-5",
  reasoning: "medium",
  tool: false,
  limits: {
    maxInputTokensPerSession: 60_000,
    maxOutputTokensPerSession: 10_000,
    maxTokenCostUsdPerSession: 0.6,
    sessionTimeoutMs: 43_200_000,
  },
});
