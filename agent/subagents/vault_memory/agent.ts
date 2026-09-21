import { defineAgent } from "eve";

export default defineAgent({
  defaultTools: false,
  description:
    "your_folder/vaultの記憶担当。既存ノートを検索・引用し、承認後にタスクや判断ログを追記する。",
  model: "anthropic/claude-haiku-4.5",
  reasoning: "low",
  tool: false,
  limits: {
    maxInputTokensPerSession: 40_000,
    maxOutputTokensPerSession: 6_000,
    maxTokenCostUsdPerSession: 0.2,
    sessionTimeoutMs: 43_200_000,
  },
});
