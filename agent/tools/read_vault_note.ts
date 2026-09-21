import { defineTool } from "eve/tools";
import legacy from "../subagents/vault_memory/tools/read_vault_note.ts";
import { missionState } from "../lib/mission/state.ts";

// Keep existing capabilities reachable without allowing them to bypass a room policy.
export default defineTool({
  ...legacy,
  execute(input, ctx) {
    if (missionState.get().locked) throw new Error("Mission Roomの処理中は専用Toolだけを使用できます。一般操作は新しい会話で依頼してください。");
    return legacy.execute(input, ctx);
  },
});
