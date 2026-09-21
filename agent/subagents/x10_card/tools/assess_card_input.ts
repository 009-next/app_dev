import { defineTool } from "eve/tools";
import { z } from "zod";
import { assessCardInput } from "../../../lib/privacy.ts";

export default defineTool({
  description: "カード生成前に、画像のマスキング、前後画像、匿名ID、説明の最低条件を決定的に検査する。",
  inputSchema: z.object({
    objectId: z.string().min(1).max(64),
    beforeImageRefs: z.array(z.string().max(256)).max(20),
    afterImageRefs: z.array(z.string().max(256)).max(20),
    narration: z.string().max(4_000),
    maskingApproved: z.boolean(),
  }),
  execute(input) {
    return assessCardInput(input);
  },
});
