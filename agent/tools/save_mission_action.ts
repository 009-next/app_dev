import { defineTool } from "eve/tools";
import { always } from "eve/tools/approval";
import { z } from "zod";
import { missionState } from "../lib/mission/state.ts";
import { saveMissionAction } from "../lib/mission/save.ts";

export default defineTool({
  description: "人の承認後、現在のMission Roomに既にある行動下書きだけをローカル保存。本文・権限・承認済みフラグの自己申告は受け付けない。根拠変更や期限切れ時は拒否。",
  inputSchema: z.object({ missionId: z.string().max(64), actionId: z.string().max(64), digest: z.string().regex(/^[a-f0-9]{64}$/),
    operationId: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{7,63}$/) }).strict(),
  approval: always(),
  label: { start: ({ missionId }) => `確認済み下書きを保存: ${missionId}` },
  async execute(input) {
    const report = missionState.get().report;
    if (!report || report.missionId !== input.missionId) throw new Error("現在のMission Roomではありません。再分析してください。");
    const saved = await saveMissionAction(report, input.actionId, input.digest, input.operationId);
    missionState.update(s => ({ ...s, report: s.report ? { ...s.report, steps: s.report.steps.map(step => step.id === "push" ? { ...step, status: "completed", detail: saved.path } : step) } : null }));
    return saved;
  },
});
