import { defineAgent, defineDynamic } from "eve";
import { routeRequest } from "./lib/mission/analyze.ts";
import { ORCA_ROUTER_CONTEXT_WINDOW_TOKENS, orcaRouterFreeModel } from "./lib/models/orca_router.ts";
import { usageState } from "./lib/mission/state.ts";

export default defineAgent({
  model: defineDynamic({ events: {
    "step.started": (_event, ctx) => {
      const user = [...ctx.messages].reverse().find(m => m.role === "user");
      const parts = Array.isArray(user?.content) ? user.content : [];
      if (ctx.messages.some(m => m.role === "user" && Array.isArray(m.content) && m.content.some(p => p.type === "image" || p.type === "file"))) {
        throw new Error("この入口はテキスト専用です。画像本体ではなく検査済み参照と作業説明を使用してください。");
      }
      const text = typeof user?.content === "string" ? user.content : parts.filter(p => p.type === "text").map(p => "text" in p ? p.text : "").join("\n");
      const route = routeRequest(text);
      const orca = orcaRouterFreeModel();
      usageState.update(state => ({ ...state, orcaRouterFreeActive: Boolean(orca) }));
      if (orca) {
        return {
          model: orca,
          reasoning: "provider-default" as const,
          modelContextWindowTokens: ORCA_ROUTER_CONTEXT_WINDOW_TOKENS,
        };
      }
      return route.recommendedModel === "high-quality" && route.risk !== "high"
        ? { model: "anthropic/claude-sonnet-5", reasoning: "medium" as const }
        : { model: "anthropic/claude-haiku-4.5", reasoning: "low" as const };
    },
  } }),
  defaultTools: false,
  reasoning: "medium",
  tool: false,
  compaction: {
    thresholdPercent: 0.75,
  },
  limits: {
    maxInputTokensPerSession: 120_000,
    maxOutputTokensPerSession: 20_000,
    maxTokenCostUsdPerSession: 1,
    sessionTimeoutMs: 24 * 60 * 60 * 1_000,
  },
});
