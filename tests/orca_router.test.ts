import assert from "node:assert/strict";
import test from "node:test";
import { WORKFLOW_DESERIALIZE, WORKFLOW_SERIALIZE } from "@ai-sdk/provider-utils";
import {
  ORCA_ROUTER_FREE_MODEL,
  OrcaRouterFreeModel,
  orcaRouterFreeModel,
} from "../agent/lib/models/orca_router.ts";

test("ORCA ROUTERは環境変数でのみ有効化し、シリアライズ結果にキーを含めない", () => {
  const original = process.env.ORCAROUTER_API_KEY;
  try {
    delete process.env.ORCAROUTER_API_KEY;
    assert.equal(orcaRouterFreeModel(), null);

    const testKey = ["unit", "test", "key", "for", "orca", "router"].join("-");
    process.env.ORCAROUTER_API_KEY = testKey;
    const model = orcaRouterFreeModel();
    assert.ok(model instanceof OrcaRouterFreeModel);
    assert.equal(model.modelId, ORCA_ROUTER_FREE_MODEL);
    assert.equal(model.provider, "orcarouter.chat");

    const serialized = OrcaRouterFreeModel[WORKFLOW_SERIALIZE](model);
    assert.deepEqual(serialized, { modelId: ORCA_ROUTER_FREE_MODEL });
    assert.equal(JSON.stringify(serialized).includes(testKey), false);
    assert.equal(OrcaRouterFreeModel[WORKFLOW_DESERIALIZE](serialized).modelId, ORCA_ROUTER_FREE_MODEL);
  } finally {
    if (original === undefined) delete process.env.ORCAROUTER_API_KEY;
    else process.env.ORCAROUTER_API_KEY = original;
  }
});
