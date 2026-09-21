import { defineEval } from "eve/evals";

export default defineEval({
  description: "明示有効化時だけORCA経由のTool選択を確認する。",
  tags: ["live", "orca", "autonomy"],
  async test(t) {
    if (process.env.CONNECT_FORCE_RUN_LIVE_EVALS !== "yes" || !process.env.ORCAROUTER_API_KEY) {
      t.skip("実API評価は明示実行時だけ有効です。");
      return;
    }
    await t.send("合成デモのobj_pump01について、過去の記録を調べて今後の対応を比較してください。");
    t.succeeded();
    t.calledTool("analyze_mission");
    t.notCalledTool("save_mission_action");
  },
});
