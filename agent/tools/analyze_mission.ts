import { defineTool } from "eve/tools";
import { missionInputSchema } from "../lib/mission/types.ts";
import { analyzeMission } from "../lib/mission/analyze.ts";
import { missionState } from "../lib/mission/state.ts";
import { actionDigest } from "../lib/mission/save.ts";

export default defineTool({
  description: "設備IDからMission Roomを生成。実Vaultまたは明示された合成デモを検索し、組分け・履歴・異常・条件付き将来比較・承認対象下書きを返す。画像解析や故障確率予測は行わない。",
  inputSchema: missionInputSchema,
  label: { start: ({ objectId }) => `${objectId} の過去・現在・選択肢を確認` },
  async execute(input, ctx) {
    // A failed new analysis must not leave the preceding room's write capability active.
    missionState.update(() => ({ report: null, locked: true }));
    const report = await analyzeMission(input, { signal: ctx.abortSignal });
    missionState.update(() => ({ report, locked: true }));
    return { ...report, approvals: report.actions.map(a => ({ actionId: a.id, digest: actionDigest(report, a.id) })) };
  },
});
