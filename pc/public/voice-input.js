(() => {
  const $ = id => document.getElementById(id);
  const transcript = $("voiceTranscript");
  const source = $("voiceSource");
  const confirm = $("voiceConfirmed");
  const status = $("voiceStatus");
  const start = $("voiceStart");
  const stop = $("voiceStop");
  let recognition = null;

  function setStatus(text) { status.textContent = text; }
  function supported() { return window.SpeechRecognition || window.webkitSpeechRecognition; }
  function stopRecognition() {
    if (recognition) recognition.stop();
    recognition = null; start.hidden = false; stop.hidden = true;
  }
  function startRecognition() {
    const Recognition = supported();
    if (!Recognition) { setStatus("このブラウザは音声認識に対応していません。端末またはAqua Voiceで文字起こしした内容を貼り付けてください。"); return; }
    // The browser obtains microphone permission. Raw audio never enters this app's server or storage.
    recognition = new Recognition(); recognition.lang = "ja-JP"; recognition.continuous = false; recognition.interimResults = true; source.value = "browser-speech";
    recognition.onresult = event => {
      let value = "";
      for (let i = event.resultIndex; i < event.results.length; i++) value += event.results[i][0].transcript;
      transcript.value = `${transcript.value}${transcript.value && value ? "\n" : ""}${value}`.slice(0, 4000);
      confirm.checked = false;
    };
    recognition.onerror = event => { setStatus(`音声認識を停止しました: ${event.error}。必要なら文字起こしを手入力してください。`); stopRecognition(); };
    recognition.onend = () => { if (recognition) { setStatus("文字起こしを確認・編集して、送信対象であることにチェックしてください。"); stopRecognition(); } };
    try { recognition.start(); start.hidden = true; stop.hidden = false; setStatus("マイク権限を許可した場合だけ認識します。音声データは本アプリへ保存・送信しません。"); }
    catch { setStatus("音声認識を開始できません。ブラウザのマイク権限と既定の入力機器を確認してください。"); stopRecognition(); }
  }
  start.addEventListener("click", startRecognition); stop.addEventListener("click", () => { setStatus("集音を停止しました。文字起こしを確認してください。"); stopRecognition(); });
  transcript.addEventListener("input", () => { confirm.checked = false; });
  window.addEventListener("beforeunload", stopRecognition);
  window.miruVoiceInput = {
    prepare() {
      const value = transcript.value.trim();
      if (!value) return {};
      if (!confirm.checked) throw new Error("文字起こしを確認し、送信対象であることにチェックしてください。");
      return { voice: { transcript: value, source: source.value, confirmed: true } };
    },
    clear() { stopRecognition(); transcript.value = ""; confirm.checked = false; setStatus("音声は保存されません。必要なら確認済み文字起こしを入力してください。"); },
  };
})();
