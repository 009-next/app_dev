import assert from "node:assert/strict";
import test from "node:test";
import { assessCardInput } from "../agent/lib/privacy.ts";
import { resolveInside, safeOperationId } from "../agent/lib/paths.ts";

test("未マスキング画像を拒否する", () => {
  const result = assessCardInput({
    objectId: "obj_123456",
    beforeImageRefs: ["file:///raw/before.jpg"],
    afterImageRefs: ["masked://after.jpg"],
    narration: "部品を交換した",
    maskingApproved: false,
  });
  assert.equal(result.allowed, false);
  assert.ok(result.blockingReasons.some((reason) => reason.includes("マスキング")));
  assert.ok(result.blockingReasons.some((reason) => reason.includes("masked://")));
});

test("検査済み入力を許可する", () => {
  const result = assessCardInput({
    objectId: "obj_123456",
    beforeImageRefs: ["masked://before.jpg"],
    afterImageRefs: ["masked://after.jpg"],
    narration: "部品を交換した",
    maskingApproved: true,
  });
  assert.equal(result.allowed, true);
});

test("パストラバーサルと危険なoperationIdを拒否する", () => {
  assert.throws(() => resolveInside("C:\\safe", "..\\secret.md"));
  assert.throws(() => safeOperationId("../../bad"));
});
