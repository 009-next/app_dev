import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { request as httpRequest } from "node:http";
import { createMissionServer } from "../pc/server.ts";

test("PC実HTTP: 認証・CSRF・下書き確認・承認・再送・ポートキー・失効", async t => {
  const base = await mkdtemp(join(tmpdir(), "miru-pc-test-"));
  const outputRoot = join(base, "out"); const vault = join(base, "vault"); const cards = join(base, "cards");
  await Promise.all([mkdir(outputRoot), mkdir(vault), mkdir(cards)]);
  const app = createMissionServer({ outputRoot, vault, cards });
  const { origin, loginCode } = await app.listen(0);
  t.after(async () => {
    app.server.closeAllConnections(); await new Promise<void>((done, reject) => app.server.close(e => e ? reject(e) : done()));
    assert.ok(base.startsWith(join(tmpdir(), "miru-pc-test-"))); await rm(base, { recursive: true, force: true });
  });
  let cookie = ""; let csrf = "";
  const call = (path: string, data?: unknown, headers: Record<string, string> = {}) => fetch(origin + path, {
    method: data === undefined ? "GET" : "POST",
    headers: { Origin: origin, "Content-Type": "application/json", Cookie: cookie, "X-Miru-Csrf": csrf, ...headers },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const health = await fetch(origin + "/api/health");
  assert.equal(health.status, 200); assert.deepEqual(await health.json(), { status: "ok", version: "0.9.0", externalAudio: false });
  assert.equal((await call("/api/session")).status, 401);
  assert.equal((await call("/api/login", { code: "bad" })).status, 401);
  const login = await call("/api/login", { code: loginCode });
  assert.equal(login.status, 200); cookie = login.headers.get("set-cookie")!.split(";")[0]; csrf = (await login.json()).csrf;
  assert.ok(login.headers.get("set-cookie")!.includes("HttpOnly"));
  assert.equal((await call("/api/login", { code: loginCode })).status, 401);
  assert.equal((await call("/api/missions", {}, { Origin: "https://attacker.example" })).status, 403);
  assert.equal((await call("/api/missions", {}, { "X-Miru-Csrf": "bad" })).status, 403);
  const spoofedHost = await new Promise<number>(done => { const request = httpRequest(origin + "/api/session", { headers: { Host: "attacker.example", Cookie: cookie } }, response => { response.resume(); done(response.statusCode!); }); request.end(); });
  assert.equal(spoofedHost, 403);
  assert.equal((await call("/api/approve", { approvalId: "invented", decision: "approve" })).status, 400);
  const missionResponse = await call("/api/missions", { objectId: "obj_pump01", source: "demo", request: "今後の対応を比較して下書きを作成", budgetUsd: 0.1 });
  assert.equal(missionResponse.status, 200);
  const view = await missionResponse.json(); const id = view.report.missionId; const action = view.approvals[0];
  assert.equal(view.report.scenarios.length, 3); assert.equal((await readdir(outputRoot)).length, 0);
  const prepare = await call(`/api/missions/${id}/prepare`, action); assert.equal(prepare.status, 200);
  const approval = await prepare.json(); assert.equal((await readdir(outputRoot)).length, 0);
  assert.equal((await call("/api/approve", { approvalId: approval.approvalId, decision: "approve", approved: true })).status, 400);
  const saved = await call("/api/approve", { approvalId: approval.approvalId, decision: "approve" });
  assert.equal(saved.status, 200); assert.equal((await saved.json()).saved, true); assert.equal((await readdir(outputRoot)).length, 1);
  assert.equal((await call("/api/approve", { approvalId: approval.approvalId, decision: "approve" })).status, 200);
  assert.equal((await readdir(outputRoot)).length, 1);
  const repeated = await (await call(`/api/missions/${id}/prepare`, action)).json();
  assert.equal((await (await call("/api/approve", { approvalId: repeated.approvalId, decision: "approve" })).json()).deduplicated, true);
  const denied = await (await call(`/api/missions/${id}/prepare`, action)).json();
  assert.equal((await call("/api/approve", { approvalId: denied.approvalId, decision: "reject" })).status, 200);
  assert.equal((await call("/api/approve", { approvalId: denied.approvalId, decision: "approve" })).status, 400);
  const key = await (await call(`/api/missions/${id}/portkey`, {})).json();
  const jump = new URLSearchParams(new URL(key.url).hash.slice(1)).get("portkey");
  assert.equal((await call("/api/jump", { token: jump }, { Cookie: "miru_session=other" })).status, 401);
  assert.equal((await call("/api/jump", { token: jump })).status, 200);
  assert.equal((await call("/api/jump", { token: jump })).status, 400);
  const nextKey = await (await call(`/api/missions/${id}/portkey`, {})).json();
  await call(`/api/missions/${id}/revoke`, {});
  assert.equal((await call("/api/jump", { token: new URLSearchParams(new URL(nextKey.url).hash.slice(1)).get("portkey") })).status, 400);
  const high = await (await call("/api/missions", { objectId: "obj_pump01", source: "demo", request: "発煙した設備の今後を比較" })).json();
  assert.equal(high.report.status, "read_only");
  assert.equal((await call(`/api/missions/${high.report.missionId}/prepare`, { actionId: "inspection", digest: "a".repeat(64) })).status, 400);
  const empty = await (await call("/api/missions", { objectId: "obj_pump01", source: "vault", request: "今後を比較" })).json();
  assert.equal(empty.report.status, "needs_confirmation"); assert.equal(empty.report.timeline.length, 0);
  const page = await call("/"); assert.ok(page.headers.get("content-security-policy")!.includes("frame-ancestors 'none'"));
  assert.ok((await page.text()).includes("Mission Room"));
  const audioScript = await call("/audio-analysis.js"); assert.equal(audioScript.status, 200);
  assert.ok((await audioScript.text()).includes("/api/audio/analyze"));
  assert.equal((await call("/api/logout", {})).status, 200);
  assert.equal((await call(`/api/missions/${id}`)).status, 401);
});
