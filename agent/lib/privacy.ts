export type CardInput = {
  objectId: string;
  beforeImageRefs: string[];
  afterImageRefs: string[];
  narration: string;
  maskingApproved: boolean;
};

const MASKED_REF = /^masked:\/\/[a-zA-Z0-9][a-zA-Z0-9._/-]{2,255}$/;

export function assessCardInput(input: CardInput): {
  allowed: boolean;
  blockingReasons: string[];
  nextAction: string;
} {
  const blockingReasons: string[] = [];

  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$/.test(input.objectId)) {
    blockingReasons.push("objectIdは個人情報を含まない不透明IDにしてください。");
  }
  if (!input.maskingApproved) {
    blockingReasons.push("端末側マスキングの確認がありません。");
  }
  if (input.beforeImageRefs.length === 0 || input.afterImageRefs.length === 0) {
    blockingReasons.push("作業前・作業後の画像参照が両方必要です。");
  }
  const invalidRefs = [...input.beforeImageRefs, ...input.afterImageRefs].filter(
    (ref) => !MASKED_REF.test(ref),
  );
  if (invalidRefs.length > 0) {
    blockingReasons.push("画像参照は端末側処理済みを示す masked:// 形式だけ利用できます。");
  }
  if (input.narration.trim().length < 3) {
    blockingReasons.push("短い作業説明を入力してください。");
  }

  return {
    allowed: blockingReasons.length === 0,
    blockingReasons,
    nextAction:
      blockingReasons.length === 0
        ? "構造化カード下書きを作成できます。"
        : "不足項目を修正し、再検査してください。",
  };
}
