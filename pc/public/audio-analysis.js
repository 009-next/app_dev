(() => {
  const $ = id => document.getElementById(id); const start = $("audioStart"), stop = $("audioStop"), send = $("audioSend"), confirm = $("audioConfirmed"), status = $("audioStatus"), result = $("audioResult");
  let stream = null, recorder = null, chunks = [], durationMs = 0, timer = null;
  const setStatus = text => { status.textContent = text; };
  function clearRecording() { chunks = []; durationMs = 0; send.hidden = true; confirm.checked = false; }
  function stopTracks() { if (stream) stream.getTracks().forEach(track => track.stop()); stream = null; }
  function stopRecording(message) { if (timer) clearTimeout(timer); timer = null; if (recorder && recorder.state !== "inactive") recorder.stop(); stopTracks(); start.hidden = false; stop.hidden = true; if (message) setStatus(message); }
  async function begin() {
    if (!window.miruExternalAudioEnabled) { setStatus("外部音声分析は未設定です。合成デモまたは確認済み文字起こしを利用してください。"); return; }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { setStatus("このブラウザは安全なマイク録音に対応していません。確認済み文字起こしを使ってください。"); return; }
    try { clearRecording(); result.hidden = true; stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
      const type = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
      recorder = new MediaRecorder(stream, { mimeType: type }); recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      recorder.onstop = () => { if (chunks.length) { durationMs = Math.min(30_000, durationMs || 30_000); send.hidden = false; setStatus("録音を停止しました。内容を確認し、送信確認にチェックしてから分析してください。"); } };
      recorder.start(); const started = Date.now(); timer = setTimeout(() => { durationMs = 30_000; stopRecording("30秒で録音を停止しました。"); }, 30_000);
      stop.onclick = () => { durationMs = Math.min(30_000, Date.now() - started); stopRecording("録音を停止しました。"); }; start.hidden = true; stop.hidden = false; setStatus("マイクだけを録音中です。最大30秒で自動停止します。");
    } catch { stopTracks(); setStatus("録音を開始できません。マイク権限・入力機器を確認してください。"); }
  }
  async function analyze() {
    if (!confirm.checked) throw new Error("外部送信の確認チェックを付けてください。"); if (!chunks.length || !durationMs) throw new Error("送信できる録音がありません。");
    const blob = new Blob(chunks, { type: "audio/webm" }); if (blob.size > 3_900_000) throw new Error("音声が3.9MBを超えました。短く録音してください。");
    const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(blob); });
    setStatus("ORCA ROUTERへ一度だけ分析依頼しています…"); send.disabled = true;
    try { const response = await fetch("/api/audio/analyze", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-miru-csrf": window.miruGetCsrf?.() ?? "" }, body: JSON.stringify({ audioDataUrl: encoded, format: "webm", durationMs, confirmed: true }) }); const payload = await response.json(); if (!response.ok) throw new Error(payload.error ?? "音声分析を完了できませんでした。"); result.textContent = `分析結果（自動でMissionには使いません）: ${payload.summary}`; result.hidden = false; setStatus(`分析完了: ${payload.model} / サーバー実測 ${Math.ceil(payload.measuredDurationMs / 100) / 10}秒（デコード完了） / 実費はAPI応答にないため不明 / 当日残り ${payload.remainingRequestsToday}回。必要なら内容を確認して文字起こし欄へ手入力してください。`); }
    finally { clearRecording(); send.disabled = false; }
  }
  start.addEventListener("click", begin); send.addEventListener("click", () => { analyze().catch(error => { setStatus(error.message ?? "音声分析を停止しました。"); }); }); window.addEventListener("beforeunload", () => { stopRecording(); clearRecording(); });
})();
