import { defineEval } from "eve/evals";

export default defineEval({
  description: "認証済みORCA環境だけで、自然言語からのMission分析、usage確認、保存承認待ちをE2E確認する。",
  tags: ["live", "orca", "mission", "approval", "usage"],
  async test(t) {
    if (process.env.CONNECT_FORCE_RUN_LIVE_EVALS !== "yes" || !process.env.ORCAROUTER_API_KEY) {
      t.skip("実API評価は明示実行時だけ有効です。");
      return;
    }

    const analysis = await t.send("合成デモとして obj_pump01 の異音について履歴を確認し、確認用の下書きを提示してください。source は demo を使い、保存はまだ行わないでください。");
    analysis.expectOk();
    t.calledTool("analyze_mission", { count: 1 });
    analysis.notCalledTool("save_mission_action");

    const usage = await analysis.session.send("いまのMission Roomの使用量と承認待ち状況を、Mission Mapで確認してください。");
    usage.expectOk();
    t.calledTool("mission_map", { count: 1 });

    const save = await usage.session.start("先ほど提示した下書きを保存してください。保存前の承認を要求してください。");
    await save.waitForEvent("input.requested");
    save.session.requireInputRequest({ toolName: "save_mission_action" });
    const parked = await save.result();
    parked.calledTool("save_mission_action", { status: "pending", count: 1 });
    t.succeeded();
  },
});
