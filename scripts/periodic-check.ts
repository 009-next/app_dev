import { analyzeMission } from "../agent/lib/mission/analyze.ts";
import { buildPeriodicDrafts } from "../agent/lib/mission/periodic.ts";

const ids = [...new Set((process.env.CONNECT_FORCE_PERIODIC_OBJECT_IDS ?? "").split(",").map(value => value.trim()).filter(Boolean))].slice(0, 20);
if (!ids.length) {
  console.log(JSON.stringify({ drafts: [], sent: 0, note: "CONNECT_FORCE_PERIODIC_OBJECT_IDSが未設定です。外部通知は送信していません。" }, null, 2));
  process.exit(0);
}
const reports = [];
for (const objectId of ids) {
  reports.push(await analyzeMission({ objectId, request: "期限超過、再発、長期未更新を確認して通知下書き候補を作成", source: "vault", budgetUsd: 0 }));
}
console.log(JSON.stringify({ drafts: buildPeriodicDrafts(reports, 20), sent: 0, note: "候補抽出だけを行いました。保存・通知送信はしていません。" }, null, 2));
