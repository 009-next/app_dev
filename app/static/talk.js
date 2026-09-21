// 会話の集音。始めた間だけ動く（常時の集音はしない）。音声は保存しない。
// - mic / screen_audio: 端末内の音声認識（processLocally）だけを使う。端末内で処理できると確認できないときは、始めない（クラウドへ送る認識には落とさない）。
// - audio_analysis: 音声を短い区切りの WAV にして、サーバー経由で音声対応のモデル（第三者）へ送る。組織のオーナーがオンにし、作り手が確認したセッションだけ。
// 結果の文字は、サーバーの会話の文字として保存され、作り手が確認・修正・削除してから、AI に渡る。
(() => {
  "use strict";
  const root = document.querySelector("[data-talk]");
  if (!root) return;
  const sid = root.dataset.session;
  const source = root.dataset.source;
  const btn = document.getElementById("talk-toggle");
  const status = document.getElementById("talk-status");
  const CHUNK_SEC = 25;       // サーバーの上限（約 45 秒）より短く
  const RATE = 16000;
  let stop = null;

  const say = (m) => { status.textContent = m; };
  const post = (path, body, headers) => fetch(path, { method: "POST", body, headers, credentials: "same-origin" });
  const addText = (text) => post(`/talk/${sid}/add`, new URLSearchParams({ who: "不明", text }),
    { "Content-Type": "application/x-www-form-urlencoded" });

  async function getStream() {
    if (source === "screen_audio") {
      // 画面・タブの音声（相手の声）。映像は要らないので止める。音声が付かなかったら、始めない
      const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      s.getVideoTracks().forEach((t) => t.stop());
      if (!s.getAudioTracks().length) { s.getTracks().forEach((t) => t.stop()); throw new Error("音声が共有されませんでした。タブの音声を共有してください（Chrome 系のみ）。"); }
      return s;
    }
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  }

  // ---- 端末内の文字起こし ----
  async function startLocal(stream) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR || !("processLocally" in SR.prototype) || !SR.available) {
      throw new Error("この端末のブラウザでは、端末内の文字起こしを確認できませんでした。手入力か、Aqua Voice などの入力ツールを使ってください。");
    }
    const opts = { langs: ["ja-JP"], processLocally: true };
    let a = await SR.available(opts);
    if (a === "downloadable" && SR.install) { say("端末内の日本語の認識データを取得しています…"); await SR.install(opts); a = await SR.available(opts); }
    if (a !== "available") throw new Error("端末内の日本語の文字起こしが使えません（音声を外へ送る認識には切り替えません）。手入力か入力ツールを使ってください。");
    const rec = new SR();
    rec.lang = "ja-JP"; rec.continuous = true; rec.interimResults = false; rec.processLocally = true;
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) { const t = e.results[i][0].transcript.trim(); if (t) addText(t); }
      }
    };
    rec.onerror = (e) => say("文字起こしを止めました: " + e.error);
    const track = stream.getAudioTracks()[0];
    if (source === "screen_audio") rec.start(track); else rec.start();
    return () => rec.stop();
  }

  // ---- 音声分析（短い区切りの WAV をサーバーへ）----
  function wav(samples) {
    const b = new ArrayBuffer(44 + samples.length * 2), v = new DataView(b);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
    return new Blob([b], { type: "audio/wav" });
  }

  async function startAnalysis(stream) {
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    let buf = [], n = 0, sending = 0;
    const ratio = ctx.sampleRate / RATE;
    const flush = async () => {
      if (!n) return;
      const all = new Float32Array(n); let o = 0; for (const c of buf) { all.set(c, o); o += c.length; } buf = []; n = 0;
      const out = new Float32Array(Math.floor(all.length / ratio));
      for (let i = 0; i < out.length; i++) out[i] = all[Math.floor(i * ratio)];
      const fd = new FormData(); fd.append("audio", wav(out), "chunk.wav"); fd.append("format", "wav");
      sending++;
      try { const r = await post(`/talk/${sid}/audio`, fd); if (!r.ok) say("音声分析を続けられません（" + r.status + "）"); } finally { sending--; }
    };
    proc.onaudioprocess = (e) => {
      const d = e.inputBuffer.getChannelData(0); buf.push(new Float32Array(d)); n += d.length;
      if (n / ctx.sampleRate >= CHUNK_SEC) flush();
    };
    src.connect(proc); proc.connect(ctx.destination);
    return async () => { proc.disconnect(); src.disconnect(); await flush(); await ctx.close(); while (sending) await new Promise((r) => setTimeout(r, 200)); };
  }

  btn.addEventListener("click", async () => {
    if (stop) {  // 止める
      btn.disabled = true; say("止めています…");
      try { await stop(); } finally { stop = null; location.reload(); }
      return;
    }
    let stream = null;
    try {
      say("マイクの許可を確認しています…");
      stream = await getStream();
      const end = source === "audio_analysis" ? await startAnalysis(stream) : await startLocal(stream);
      stop = async () => { await end(); stream.getTracks().forEach((t) => t.stop()); };
      btn.textContent = "止める";
      say("集音中です。話し終えたら、止めてください。文字は、あとで確認できます。");
    } catch (err) {
      if (stream) stream.getTracks().forEach((t) => t.stop());
      if (err && err.name === "NotAllowedError") say("マイク（または画面の音声）の使用が許可されませんでした。手入力か入力ツールを使うか、ブラウザの設定で許可してください。");
      else say(err && err.message ? err.message : "集音を始められませんでした。手入力か入力ツールを使ってください。");
    }
  });
})();
