(() => {
  "use strict";
  const MAX_SIDE = 1600;
  const root = document.getElementById("screenCapture");
  if (!root) return;
  const startButton = document.getElementById("screenStart");
  const captureButton = document.getElementById("screenFrame");
  const stopButton = document.getElementById("screenStop");
  const undoButton = document.getElementById("screenUndo");
  const clearButton = document.getElementById("screenClear");
  const fileInput = document.getElementById("screenFile");
  const confirmed = document.getElementById("screenConfirmed");
  const noCredentials = document.getElementById("screenNoCredentials");
  const noNotifications = document.getElementById("screenNoNotifications");
  const signalRoot = document.getElementById("screenSignals");
  const status = document.getElementById("screenStatus");
  const video = document.getElementById("screenPreview");
  const canvas = document.getElementById("screenCanvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const sensitiveText = [
    /powershell|terminal|command prompt|cmd\.exe|コマンド プロンプト|ターミナル/i,
    /(?:#|%23)(?:login|portkey)=|miru_session|orcarouter_api_key|authorization\s*:|bearer\s+[a-z0-9._-]+|api[ _-]?key/i,
    /通知|新着(?:メッセージ|メール)?|microsoft teams|slack|outlook|受信トレイ/i,
  ];
  let stream = null;
  let baseFrame = null;
  let startPoint = null;
  let masks = [];
  let captureMeta = null;
  let safetyLocked = false;

  const message = value => { status.textContent = value; };
  const checkedSignals = () => [...root.querySelectorAll('input[name="screenSignal"]:checked')].map(input => input.value);
  const guardConfirmed = () => Boolean(noCredentials.checked && noNotifications.checked);
  const stopStream = () => {
    if (stream) for (const track of stream.getTracks()) track.stop();
    stream = null; video.srcObject = null; video.hidden = true;
    captureButton.hidden = true; stopButton.hidden = true; startButton.disabled = safetyLocked;
  };
  const resetFrame = () => {
    safetyLocked = false; stopStream(); baseFrame = null; masks = []; startPoint = null; captureMeta = null; confirmed.checked = false;
    fileInput.value = ""; canvas.width = 1; canvas.height = 1; canvas.hidden = true;
    undoButton.hidden = true; clearButton.hidden = true; signalRoot.hidden = true;
    root.querySelectorAll('input[name="screenSignal"]').forEach(input => { input.checked = false; });
    startButton.disabled = false; message("画面共有またはスクリーンショットを選んでください。");
  };
  const safetyStop = reason => {
    safetyLocked = true; stopStream(); baseFrame = null; masks = []; startPoint = null; captureMeta = null; confirmed.checked = false;
    canvas.width = 1; canvas.height = 1; canvas.hidden = true; signalRoot.hidden = true;
    root.querySelectorAll('input[name="screenSignal"]').forEach(input => { input.checked = false; });
    undoButton.hidden = true; clearButton.hidden = false;
    message(`安全停止: ${reason} フレームは破棄し、送信・Mission利用を行いません。内容を除去後、「取得フレームを破棄・安全停止を解除」からやり直してください。`);
  };
  const redraw = () => {
    if (!baseFrame) return;
    ctx.putImageData(baseFrame, 0, 0);
    for (const rect of masks) {
      const block = Math.max(12, Math.round(Math.min(rect.width, rect.height) / 4));
      const sample = document.createElement("canvas");
      sample.width = Math.max(1, Math.ceil(rect.width / block));
      sample.height = Math.max(1, Math.ceil(rect.height / block));
      sample.getContext("2d").drawImage(canvas, rect.x, rect.y, rect.width, rect.height, 0, 0, sample.width, sample.height);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(sample, 0, 0, sample.width, sample.height, rect.x, rect.y, rect.width, rect.height);
      ctx.imageSmoothingEnabled = true;
    }
  };
  async function inspectVisibleText() {
    if (!("TextDetector" in window)) return { supported: false, hits: [] };
    try {
      const detector = new window.TextDetector();
      const results = await detector.detect(canvas);
      const hits = results.map(result => result.rawValue ?? "").filter(text => sensitiveText.some(pattern => pattern.test(text)));
      return { supported: true, hits };
    } catch {
      // OCR is an optional local aid. A failed scan never makes the frame safe by itself.
      return { supported: false, hits: [] };
    }
  }
  async function storeFrame(source, width, height, capturedVia) {
    const scale = Math.min(1, MAX_SIDE / Math.max(width, height));
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
    const textInspection = await inspectVisibleText();
    if (textInspection.hits.length) {
      safetyStop("端末内の画面文字検査で、ターミナル・認証情報・通知に見える文字を検出しました。");
      return;
    }
    baseFrame = ctx.getImageData(0, 0, canvas.width, canvas.height);
    masks = []; captureMeta = { capturedAt: new Date().toISOString(), capturedVia };
    confirmed.checked = false; canvas.hidden = false; undoButton.hidden = false; clearButton.hidden = false; signalRoot.hidden = false;
    message(textInspection.supported
      ? "フレームを取得し、端末内の危険文字検査を通過しました。機微情報をマスクし、画面の状態タグを選んで確認してください。"
      : "フレームを取得しました。このブラウザでは端末内の文字検査を利用できません。共有前の安全停止ゲートを再確認し、機微情報をマスクしてください。");
  }
  function requireGuard() {
    if (safetyLocked) { message("安全停止中です。内容を除去後、破棄ボタンで解除してください。"); return false; }
    if (!guardConfirmed()) { message("共有前の安全停止ゲートを両方確認してください。ターミナル、認証URL、通知が見える場合は開始できません。"); return false; }
    return true;
  }

  startButton.addEventListener("click", async () => {
    if (!requireGuard()) return;
    if (!navigator.mediaDevices?.getDisplayMedia) {
      message("このブラウザは画面共有に対応していません。スクリーンショットを選択してください。"); return;
    }
    try {
      stopStream();
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 1, max: 5 } }, audio: false,
        monitorTypeSurfaces: "exclude", selfBrowserSurface: "exclude", surfaceSwitching: "exclude",
      });
      const track = stream.getVideoTracks()[0];
      const displaySurface = track?.getSettings?.().displaySurface;
      if (!track || stream.getAudioTracks().length || !["window", "browser"].includes(displaySurface)) {
        safetyStop("画面全体、音声を含む共有、または共有対象を安全に確認できない共有を検出しました。対象ウィンドウか動画タブだけを選んでください。"); return;
      }
      video.srcObject = stream; video.muted = true; video.hidden = false; await video.play();
      startButton.disabled = true; captureButton.hidden = false; stopButton.hidden = false;
      message("共有中です。対象ウィンドウまたは動画タブだけを確認し、「この瞬間を取得」を押してください。検出時は安全停止します。");
      track.addEventListener("ended", () => {
        if (stream) { stopStream(); message(baseFrame ? "画面共有を終了しました。取得済みフレームだけが端末内に残っています。" : "画面共有を終了しました。"); }
      }, { once: true });
    } catch (error) {
      stopStream();
      message(error?.name === "NotAllowedError" ? "画面共有は許可されませんでした。必要ならスクリーンショットを選択してください。" : "画面共有を開始できません。ブラウザ設定または対応状況を確認してください。");
    }
  });
  captureButton.addEventListener("click", async () => {
    if (!stream || video.readyState < 2 || !video.videoWidth || !video.videoHeight) { message("共有画面をまだ取得できません。少し待ってから再実行してください。"); return; }
    const active = stream; const surface = active.getVideoTracks()[0]?.getSettings?.().displaySurface;
    if (!requireGuard() || !["window", "browser"].includes(surface)) { safetyStop("取得時に安全な共有対象を確認できませんでした。"); return; }
    await storeFrame(video, video.videoWidth, video.videoHeight, surface); stopStream();
  });
  stopButton.addEventListener("click", () => { stopStream(); message("画面共有を終了しました。フレームは取得していません。"); });
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0]; if (!file || !requireGuard()) return;
    if (file.size > 5_000_000) { fileInput.value = ""; message("スクリーンショットは5MB以下にしてください。"); return; }
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      await storeFrame(bitmap, bitmap.width, bitmap.height, "file"); bitmap.close();
    } catch { fileInput.value = ""; message("JPEG、PNG、WebPのスクリーンショットを選択してください。"); }
  });
  canvas.addEventListener("pointerdown", event => {
    if (!baseFrame) return;
    const box = canvas.getBoundingClientRect();
    startPoint = { x: (event.clientX - box.left) * canvas.width / box.width, y: (event.clientY - box.top) * canvas.height / box.height };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointerup", event => {
    if (!startPoint) return;
    const box = canvas.getBoundingClientRect();
    const end = { x: (event.clientX - box.left) * canvas.width / box.width, y: (event.clientY - box.top) * canvas.height / box.height };
    const rect = { x: Math.max(0, Math.min(startPoint.x, end.x)), y: Math.max(0, Math.min(startPoint.y, end.y)), width: Math.abs(end.x - startPoint.x), height: Math.abs(end.y - startPoint.y) };
    startPoint = null; if (rect.width >= 8 && rect.height >= 8) { masks.push(rect); redraw(); confirmed.checked = false; }
  });
  canvas.addEventListener("pointercancel", () => { startPoint = null; });
  undoButton.addEventListener("click", () => { masks.pop(); redraw(); confirmed.checked = false; });
  clearButton.addEventListener("click", resetFrame);
  window.addEventListener("pagehide", stopStream);

  window.miruScreenCapture = {
    async prepareUpload(api, objectId) {
      if (!baseFrame) return { screenImageRefs: [] };
      if (safetyLocked) throw new Error("画面フレームは安全停止中です。内容を破棄してからやり直してください。");
      if (!guardConfirmed() || !confirmed.checked) throw new Error("画面フレームの安全停止ゲートとマスキング確認にチェックしてください。");
      const observedSignals = checkedSignals();
      if (!observedSignals.length || !captureMeta) throw new Error("画面で確認した状態を1つ以上選んでください。画面の原文は入力しません。");
      const result = await api("/api/images", { objectId, role: "screen", confirmed: true, dataUrl: canvas.toDataURL("image/jpeg", 0.85), extraMasks: [] });
      return { screenImageRefs: [result.ref], screenContext: { observedSignals, ...captureMeta, confirmed: true } };
    },
  };
})();
