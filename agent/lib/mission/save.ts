import { mkdir, open, readFile, realpath } from "node:fs/promises";
import { resolve } from "node:path";
import { safeOperationId, projectRoot, missionDraftRoot } from "../paths.ts";
import { assertOperation } from "./analyze.ts";
import { canonicalInside, collectEvidence, digest, evidenceDigest } from "./evidence.ts";
import type { MissionReport } from "./types.ts";

export function actionDigest(report: MissionReport, actionId: string): string {
  const action = report.actions.find(a => a.id === actionId);
  if (!action) throw new Error("対象の行動がありません。");
  return digest(JSON.stringify({ missionId: report.missionId, snapshot: report.snapshotDigest,
    images: { before: report.input.beforeImageRefs, after: report.input.afterImageRefs, screen: report.input.screenImageRefs }, action }));
}

// Call only after Eve approval or server-side, session-bound human confirmation.
// No Tool schema exposes an 'approved: true' escape hatch.
export async function saveMissionAction(report: MissionReport, actionId: string, expectedDigest: string, operationId: string,
  options: { outputRoot?: string; vault?: string; cards?: string; now?: Date; resolveImage?: (ref: string) => Promise<Buffer | undefined> } = {}) {
  assertOperation(report, "save_action_draft", options.now);
  safeOperationId(operationId);
  if (expectedDigest !== actionDigest(report, actionId)) throw new Error("承認対象が変わりました。再確認してください。");
  if (report.input.source === "vault") {
    const current = await collectEvidence(report.input.objectId, options);
    if (evidenceDigest(current) !== report.snapshotDigest) throw new Error("根拠が更新されました。再分析してから承認してください。");
  }
  const action = report.actions.find(a => a.id === actionId)!;
  const root = options.outputRoot ?? missionDraftRoot();
  await mkdir(root, { recursive: true });
  if (!options.outputRoot && !process.env.CONNECT_FORCE_MISSION_DRAFT_ROOT) await canonicalInside(projectRoot(), root);
  const canonicalRoot = await realpath(root);
  const target = resolve(canonicalRoot, `${operationId}.json`);
  const imageRefs = [...report.input.beforeImageRefs, ...report.input.afterImageRefs, ...report.input.screenImageRefs];
  const imagePaths: string[] = [];
  if (imageRefs.length) {
    if (!options.resolveImage) throw new Error("検査済み画像を確認できません。画像を再登録してください。");
    const imageRoot = resolve(canonicalRoot, "images");
    await mkdir(imageRoot, { recursive: true });
    for (const [index, ref] of imageRefs.entries()) {
      const bytes = await options.resolveImage(ref);
      if (!bytes) throw new Error("検査済み画像の有効期限が切れました。画像を再登録してください。");
      const name = `${operationId}_${index + 1}.jpg`;
      const imageTarget = resolve(imageRoot, name);
      let imageHandle;
      try { imageHandle = await open(imageTarget, "wx", 0o600); await imageHandle.writeFile(bytes); await imageHandle.sync(); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        if (!(await readFile(imageTarget)).equals(bytes)) throw new Error("operationIdが別の画像に使われています。");
      } finally { await imageHandle?.close(); }
      imagePaths.push(resolve(imageRoot, name));
    }
  }
  const payload = { schemaVersion: 1, status: "draft", missionId: report.missionId, objectId: report.input.objectId,
    source: report.input.source, visibility: "private", approvedDigest: expectedDigest, action,
    snapshotDigest: report.snapshotDigest, images: { before: report.input.beforeImageRefs, after: report.input.afterImageRefs,
      screen: report.input.screenImageRefs, localPaths: imagePaths },
    evidence: report.timeline.filter(r => action.evidenceIds.includes(r.id)), createdAt: report.createdAt };
  const serialized = JSON.stringify(payload, null, 2) + "\n";
  let handle;
  try { handle = await open(target, "wx", 0o600); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    await canonicalInside(canonicalRoot, target);
    if (await readFile(target, "utf8") !== serialized) throw new Error("operationIdが別内容に使われています。");
    return { saved: true, deduplicated: true, path: target };
  }
  try { await handle.writeFile(serialized, "utf8"); await handle.sync(); } finally { await handle.close(); }
  return { saved: true, deduplicated: false, path: target };
}
