const $ = id => document.getElementById(id);
let csrf = ""; let room = null; let pendingApproval = null; let busy = false;
window.miruGetCsrf = () => csrf;
const node = (tag, text, className) => { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element; };
const showError = error => { $("error").textContent = error.message ?? String(error); $("error").hidden = false; };
async function api(path, data) {
  const response = await fetch(path, { method: data === undefined ? "GET" : "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-miru-csrf": csrf }, ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
  const payload = await response.json(); if (!response.ok) throw new Error(payload.error ?? "処理を完了できませんでした。"); return payload;
}
async function run(task) {
  if (busy) return; busy = true; $("error").hidden = true;
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  try { await task(); } catch (error) { showError(error); }
  finally { busy = false; document.querySelectorAll("button").forEach(b => b.disabled = false); }
}
function authenticated(value) {
  csrf = value.csrf; $("controls").hidden = false; $("loginHint").hidden = true; $("logout").hidden = false;
  window.miruExternalAudioEnabled = Boolean(value.externalAudio);
  $("sessionStatus").textContent = "● このPCで認証済み";
}
function render(value) {
  room = value; const r = value.report; sessionStorage.setItem("miru-room", r.missionId); $("report").hidden = false;
  $("roomId").textContent = r.missionId; $("roomTitle").textContent = `${r.input.objectId} の作業室`;
  $("roomMeta").textContent = `${r.input.source === "demo" ? "合成デモ・実記録ではありません" : "実際のローカル記録"} / 画面フレーム: ${r.input.screenImageRefs?.length ?? 0}件 / 音声文字起こし: ${r.voice?.state === "confirmed" ? "確認済み" : "なし"} / 確認: ${new Date(r.createdAt).toLocaleString("ja-JP")} / 有効期限: ${new Date(r.expiresAt).toLocaleTimeString("ja-JP")}`;
  $("roomStatus").textContent = ({ ready: "根拠確認済み", read_only: "読取り専用・判断保留", needs_confirmation: "追加確認が必要" })[r.status];
  $("evidenceCount").textContent = `${r.timeline.length} 件`; $("alertCount").textContent = `${r.alerts.length} 件`;
  $("steps").replaceChildren(...r.steps.map((step, i) => { const box = node("article", undefined, `step ${step.status}`); box.append(node("span", `0${i + 1}`, "number"), node("b", step.label), node("p", step.detail)); return box; }));
  $("route").replaceChildren(...r.route.reasons.map(reason => node("p", reason)), node("p", `許可された処理: ${r.route.allowedOperations.join(" / ")}`), node("p", `音声入力: ${r.voice?.reason ?? "なし"}`), node("p", `Eve会話での推奨: ${r.route.recommendedModel === "high-quality" ? "高品質モデル" : "低コストモデル"}（このPC解析ではモデルを呼び出していません）`));
  const enhancement = value.enhancement ?? { status: "skipped", summary: null, reason: "AI後追い処理なし" };
  $("enhancement").replaceChildren(node("b", `後追いAI: ${({ queued: "処理中", complete: "完了", skipped: "未実行", failed: "安全停止" })[enhancement.status]}`), node("p", enhancement.summary ?? enhancement.reason));
  if (enhancement.summary) $("enhancement").append(node("small", enhancement.reason, "fineprint"));
  const displayedCost = r.cost.localModelCalls === 0 ? "$0 " : r.cost.outerAgentCostUsd === null ? "不明 " : `$${r.cost.outerAgentCostUsd.toFixed(6)} `;
  $("modelCost").replaceChildren(document.createTextNode(displayedCost), node("em", `/ ${r.cost.localModelCalls}回`));
  $("modelCostSource").textContent = r.cost.outerAgentCostUsd === null && r.cost.localModelCalls ? "費用不明のため追加生成を停止" : "ローカル解析はモデル費0。外部モデル費は実費・推定・不明を区別";
  $("costNote").textContent = r.cost.explanation;
  $("timeline").replaceChildren(...r.timeline.map(record => { const box = node("article", undefined, "timeline-item"); box.append(node("time", record.date ?? "日時不明"), node("p", record.text), node("span", `${record.source} / ${record.path}:${record.line}`, "source")); return box; }));
  if (!r.timeline.length) $("timeline").append(node("p", "該当する記録がありません。設備IDと資料を確認してください。", "fineprint"));
  for (const notice of r.notices) $("timeline").append(node("p", notice, "fineprint"));
  $("alerts").replaceChildren(...r.alerts.map(a => { const box = node("article", undefined, `alert ${a.severity}`); box.append(node("p", a.message), node("small", `ルール: ${a.rule}`), node("small", a.evidenceIds.length ? `根拠: ${a.evidenceIds.join(" / ")}` : "根拠: 依頼または検索状態")); return box; }));
  if (!r.alerts.length) $("alerts").append(node("p", "設定済みルールでは警告なし。設備の安全を保証する判定ではありません。", "fineprint"));
  const semantic = r.semantic ?? { state: "not_available", screenSignals: [], voiceSignals: [], sharedSignals: [], conflicts: [], reason: "このMissionには意味統合の入力がありません。" };
  const semanticLabels = { not_available: "未実行", screen_only: "画面のみ", voice_only: "会話のみ", aligned: "意味が一致", needs_confirmation: "確認が必要", conflict: "矛盾・安全停止", safety_hold: "音声を安全停止" };
  const semanticBox = node("article", undefined, `semantic-relation ${semantic.state}`);
  semanticBox.append(node("b", `画面 × 会話: ${semanticLabels[semantic.state] ?? semantic.state}`), node("p", semantic.reason));
  if (semantic.screenSignals.length || semantic.voiceSignals.length) semanticBox.append(node("small", `画面: ${semantic.screenSignals.join(" / ") || "なし"} | 会話: ${semantic.voiceSignals.join(" / ") || "なし"} | 一致: ${semantic.sharedSignals.join(" / ") || "なし"}`));
  $("semanticRelation").replaceChildren(semanticBox);
  $("questions").replaceChildren(...r.questions.map(q => node("p", q, "questions")));
  $("scenarios").replaceChildren(...r.scenarios.map((s, i) => { const box = node("article", undefined, "scenario"); box.append(node("h3", `0${i + 1} / ${s.title}`)); for (const [title, text] of [["前提", s.premise], ["想定", s.expected], ["リスク", s.risk], ["確認条件", s.verify]]) { const p = node("p"); p.append(node("b", title), node("span", text)); box.append(p); } box.append(node("p", `根拠 ${s.evidenceIds.length}件 / 費用・時間・確率: 未計測`, "fineprint")); return box; }));
  if (!r.scenarios.length) $("scenarios").append(node("p", "今回の依頼では比較を実行していません。根拠不足・矛盾がある場合も生成を停止します。", "fineprint"));
  $("actions").replaceChildren(...r.actions.map(action => { const box = node("article", undefined, "action"); box.append(node("p", action.title), node("pre", action.body), node("p", `根拠: ${action.evidenceIds.join(" / ")}`, "fineprint")); const b = node("button", "内容を確認して保存へ"); b.addEventListener("click", () => run(async () => { const approval = value.approvals.find(a => a.actionId === action.id); pendingApproval = await api(`/api/missions/${r.missionId}/prepare`, approval); $("approvalBody").textContent = pendingApproval.action.body; $("approvalDestination").textContent = `保存先: ${pendingApproval.destination}`; $("approval").showModal(); })); box.append(b); return box; }));
  if (!r.actions.length) $("actions").append(node("p", "この作業室には保存権限がありません。表示された確認事項を解消してください。", "fineprint"));
  $("decisionTrace").replaceChildren(...r.decisionTrace.map(trace => { const box = node("article", undefined, "decision-item"); box.append(node("b", `${trace.stage}: ${trace.selected}`), node("p", trace.reason), node("small", `候補: ${trace.options.join(" / ")} | Tool: ${trace.tool ?? "なし"} | Model: ${trace.model ?? "なし"} | 費用: ${trace.costUsd === null ? "不明" : "$" + trace.costUsd.toFixed(6)} | 待ち時間: ${trace.latencyMs === null ? "不明" : trace.latencyMs + "ms"}`)); if (trace.evidenceIds.length) box.append(node("small", `根拠: ${trace.evidenceIds.join(" / ")}`)); return box; }));
  $("audit").replaceChildren(...value.audit.map(a => node("li", `${new Date(a.at).toLocaleTimeString("ja-JP")} ${a.event}`)));
  if (enhancement.status === "queued") setTimeout(() => { if (!busy && room?.report.missionId === r.missionId) run(refresh); }, 600);
}
async function refresh() { if (room) render(await api(`/api/missions/${room.report.missionId}`)); }
async function analyze() {
  $("message").textContent = "記録を集め、根拠と処理を確認しています…";
  try { const images = await window.miruMasks.prepareUploads(api, $("objectId").value); const screen = await window.miruScreenCapture.prepareUpload(api, $("objectId").value); const voice = window.miruVoiceInput.prepare(); const source = $("source").value; render(await api("/api/missions", { objectId: $("objectId").value, request: $("request").value, source, budgetUsd: source === "demo" ? 0 : 0.1, ...images, ...screen, ...voice })); $("jumpLink").hidden = true; $("keyExpiry").textContent = ""; $("message").textContent = source === "demo" ? "合成デモのローカル分析が完了しました。外部モデルは呼び出していません。" : "ローカル分析が完了しました。AI要約は後追いで状態を更新します。"; }
  catch (e) { $("message").textContent = ""; throw e; }
}
$("missionForm").addEventListener("submit", event => { event.preventDefault(); run(analyze); });
$("demo").addEventListener("click", () => run(async () => { $("source").value = "demo"; $("objectId").value = "obj_pump01"; $("request").value = "過去の故障履歴を調べ、今後の対応を比較し、確認用の下書きを作成して"; await analyze(); }));
$("refresh").addEventListener("click", () => run(refresh));
$("periodic").addEventListener("click", () => run(async () => { const result = await api("/api/periodic", { limit: 20 }); $("periodicDrafts").replaceChildren(...result.drafts.map(draft => { const box = node("article", undefined, "periodic-draft"); box.append(node("b", draft.title), node("p", draft.body), node("small", `理由: ${draft.reasonRules.join(" / ")} | 根拠: ${draft.evidenceIds.join(" / ") || "なし"}`)); return box; })); if (!result.drafts.length) $("periodicDrafts").append(node("p", "現在の作業室に通知下書きの候補はありません。", "fineprint")); $("periodicDrafts").append(node("p", result.note, "fineprint")); }));
$("portkey").addEventListener("click", () => run(async () => { const key = await api(`/api/missions/${room.report.missionId}/portkey`, {}); $("jumpLink").href = key.url; $("jumpLink").hidden = false; $("keyExpiry").textContent = `有効期限: ${new Date(key.expiresAt).toLocaleTimeString("ja-JP")} / 同じログインで1回のみ`; }));
$("revoke").addEventListener("click", () => run(async () => { await api(`/api/missions/${room.report.missionId}/revoke`, {}); $("jumpLink").hidden = true; $("keyExpiry").textContent = "発行済みキーを無効化しました。"; await refresh(); }));
async function jump() { const params = new URLSearchParams(location.hash.slice(1)); const key = params.get("portkey"); if (key) { history.replaceState(null, "", location.pathname); render(await api("/api/jump", { token: key })); $("jumpLink").hidden = true; $("keyExpiry").textContent = "移動キーは使用済みです。"; $("message").textContent = "ポートキーで作業室へ移動しました。キーは使用済みです。"; } }
window.addEventListener("hashchange", () => { if (location.hash.startsWith("#portkey=")) run(jump); });
for (const [id, decision] of [["approve", "approve"], ["reject", "reject"]]) $(id).addEventListener("click", () => run(async () => { if (!pendingApproval) return; const result = await api("/api/approve", { approvalId: pendingApproval.approvalId, decision }); $("approval").close(); pendingApproval = null; $("message").textContent = result.saved ? `承認後に保存しました: ${result.path}` : "保存を取り消しました。"; await refresh(); }));
$("logout").addEventListener("click", () => run(async () => { await api("/api/logout", {}); sessionStorage.removeItem("miru-room"); room = null; location.reload(); }));
run(async () => {
  const params = new URLSearchParams(location.hash.slice(1)); const code = params.get("login");
  if (code) { history.replaceState(null, "", location.pathname); authenticated(await api("/api/login", { code })); }
  else { try { authenticated(await api("/api/session")); } catch (e) { $("loginHint").hidden = false; $("sessionStatus").textContent = "未接続"; throw e; } }
  if (params.has("portkey")) await jump();
  else { const id = sessionStorage.getItem("miru-room"); if (id) { try { render(await api(`/api/missions/${id}`)); } catch { sessionStorage.removeItem("miru-room"); } } }
});
