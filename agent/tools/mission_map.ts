import { defineTool } from "eve/tools";
import { z } from "zod";
import { missionState, usageState } from "../lib/mission/state.ts";

export default defineTool({
  description: "現在のMission Roomの根拠、処理段階、承認待ちと、取得できたEveセッション実使用量を返す。未報告費用は不明として扱う。",
  inputSchema: z.object({}),
  execute() {
    const entries = usageState.get().entries;
    return { report: missionState.get().report, usage: { entries,
      measuredCostUsd: entries.length && entries.every(e => e.costSource === "reported") ? entries.reduce((sum, e) => sum + e.costUsd!, 0) : null,
      unknownCostCalls: entries.filter(e => e.costSource === "unknown").length,
      note: "当セッションの記録済みモデル呼出しのみ。価格未報告の呼出しは不明として停止します。現在進行中の生成・インフラ費も含みません。" } };
  },
});
