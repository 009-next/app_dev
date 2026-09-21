// 端末上のマスキング。元の写真はこのページのメモリ（ImageBitmap）にだけ置く。
// サーバーへ送るのは、範囲を潰した後の Canvas を JPEG にしたものだけ。
// 元の写真の <input type=file> は name を持たないので、フォームの送信には入らない。
// 保存（localStorage / IndexedDB / Cache）や、Canvas 以外からの送信は行わない。
(() => {
  "use strict";
  const MAX_SIDE = 1600;       // サーバー側 images.MAX_SIDE と同じ。縮小すると EXIF も残らない
  const MIN_RECT = 8;          // これより小さい範囲はタップの誤操作として無視する
  const OPEN_LIMIT = 0.20;     // 開けてよい面積の上限（サーバー app/vision.py の OPEN_MAX_RATIO と同じ）
  const MEASURE_W = 320, MEASURE_H = 180;   // 開けた面積を測る、小さな白黒の面の大きさ
  // 操作のモード。legacy=旧来（四角だけ・面積は送らない）／extended=拡張（四角＋なぞって囲む・面積を測って送る）。既定は旧来
  const form = document.querySelector("[data-mask-form]");
  if (!form) return;

  const modeSel = form.querySelector(".mask-mode-select");
  const toolSel = form.querySelector(".mask-tool-select");
  const toolWrap = form.querySelector(".mask-tool-wrap");
  // 選んだモードは、この画面を開いている間だけ有効（端末には保存しない。開き直すと旧来に戻る）。mask.js は何も保存しない約束（テストで固定）
  let mode = "legacy";
  const isExtended = () => mode === "extended";
  const recAsCall = form.querySelector(".mask-rec-as-call");   // 動画ファイルを、通話の録画として取り込む（デモ・検証用）
  const confirmLabel = form.querySelector(".mask-confirm");
  const confirmBox = form.querySelector("#mask-confirmed");
  const errorEl = form.querySelector("#mask-error");

  // 範囲を強くモザイクにする。ブロックは短辺の 1/4 以上にして、元に戻せない粗さにする。
  // 縮小描画は間引き（点の抽出）になりうるので、ブロックごとに平均色を自分で計算する。
  function pixelate(ctx, r) {
    const block = Math.max(10, Math.round(Math.min(r.w, r.h) / 4));
    const img = ctx.getImageData(r.x, r.y, r.w, r.h);
    const d = img.data;
    for (let by = 0; by < r.h; by += block) {
      for (let bx = 0; bx < r.w; bx += block) {
        const bw = Math.min(block, r.w - bx), bh = Math.min(block, r.h - by);
        let sr = 0, sg = 0, sb = 0;
        for (let y = by; y < by + bh; y++) {
          for (let x = bx; x < bx + bw; x++) {
            const i = (y * r.w + x) * 4;
            sr += d[i]; sg += d[i + 1]; sb += d[i + 2];
          }
        }
        const n = bw * bh;
        const ar = Math.round(sr / n), ag = Math.round(sg / n), ab = Math.round(sb / n);
        for (let y = by; y < by + bh; y++) {
          for (let x = bx; x < bx + bw; x++) {
            const i = (y * r.w + x) * 4;
            d[i] = ar; d[i + 1] = ag; d[i + 2] = ab; d[i + 3] = 255;
          }
        }
      }
    }
    ctx.putImageData(img, r.x, r.y);
  }

  class Widget {
    constructor(el) {
      this.el = el;
      this.role = el.dataset.role;
      this.input = el.querySelector(".mask-file");
      this.canvas = el.querySelector(".mask-canvas");
      this.help = el.querySelector(".mask-help");
      this.undo = el.querySelector(".mask-undo");
      this.video = el.querySelector(".mask-video");       // 動画から1コマ選ぶときだけ使う。端末の外へは出さない
      this.pick = el.querySelector(".mask-pick");
      this.share = el.querySelector(".mask-share");
      this.source = el.querySelector(".mask-source");   // 出どころ。カードに残し、AI にも伝える
      this.area = el.querySelector(".mask-area");       // 「いま開いている範囲」の表示（拡張モードの通話の画面だけ）
      this.openRatio = 0;
      this.stream = null;   // 通話の画面のライブ映像。1コマ取ったらすぐ止める
      this.reveal = false;  // true: 全面を隠した状態から、見せる所だけを開ける（通話の画面から取り込んだとき）
      this.videoUrl = null;
      this.ctx = this.canvas.getContext("2d");
      this.bitmap = null;   // 元の写真。ここから外へ出さない
      this.rects = [];
      this.drag = null;
      this.input.addEventListener("change", () => this.load());
      this.undo.addEventListener("click", () => { this.rects.pop(); this.redraw(); });
      this.pick.addEventListener("click", () => this.grabFrame());
      // 画面共有は、対応している端末（主に PC）でだけ出す。iOS・Android のブラウザは対応していない
      if (navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) this.share.hidden = false;
      this.share.addEventListener("click", () => this.startShare());
      this.canvas.addEventListener("pointerdown", (e) => this.down(e));
      this.canvas.addEventListener("pointermove", (e) => this.move(e));
      this.canvas.addEventListener("pointerup", (e) => this.up(e));
      this.canvas.addEventListener("pointercancel", () => { this.drag = null; this.redraw(); });
    }

    async load() {
      this.release();
      this.closeVideo();
      const file = this.input.files && this.input.files[0];
      if (!file) return;
      // 動画は、そのままでは送らない。端末の中で再生して、作り手が選んだ1コマだけを写真にする
      if (file.type.startsWith("video/") || /\.(mp4|mov|m4v|webm|3gp)$/i.test(file.name)) {
        this.loadVideo(file);
        this.input.value = "";
        return;
      }
      this.reveal = false;
      this.setSource("camera");
      try {
        // 向きは EXIF に従って直す。Canvas に描いた時点で、位置・日時などのメタデータは消える
        const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
        const s = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
        this.canvas.width = Math.round(bmp.width * s);
        this.canvas.height = Math.round(bmp.height * s);
        this.bitmap = bmp;
        this.canvas.hidden = this.help.hidden = false;
        this.redraw();
      } catch (_) {
        this.input.value = "";
        // 動画を選んだ人に「写真として読めない」とだけ返すと、何が悪いのか分からない。理由を分けて出す
        setError(file.type.startsWith("video/") || /\.(mp4|mov|m4v|avi|webm|mkv|3gp)$/i.test(file.name)
          ? "動画は取り込めません。写真を選ぶか、動画を一時停止した画面を撮って、その写真を選んでください。"
          : "この写真は読み込めませんでした。別の写真か、JPEG・PNG で保存し直したものをお試しください。");
      }
      this.input.value = "";  // 選択した元ファイルへの参照を手放す
      syncConfirm();
    }

    setSource(kind) {
      if (this.source) this.source.value = kind;
    }

    async startShare() {
      // LINE・Teams・Google Meet・Zoom の通話は、別のアプリ・別のページなので、直接は読めない。
      // 作り手が「その通話のウィンドウ」を選んで共有したものだけを受け取る。
      this.release();
      this.closeVideo();
      let stream;
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: { displaySurface: "window" },   // ウィンドウ単位を求める（画面全体は下で断る）
          audio: false,                           // 通話の音声は取らない
          selfBrowserSurface: "exclude",          // このページ自身は選べないようにする
          surfaceSwitching: "exclude",            // 途中で別のウィンドウに切り替えさせない
          systemAudio: "exclude",
        });
      } catch (_) {
        setError("画面の共有は始まりませんでした。取り消されたか、この端末では使えません。");
        return;
      }
      const track = stream.getVideoTracks()[0];
      const surface = track && track.getSettings ? track.getSettings().displaySurface : null;
      if (surface === "monitor") {
        // 画面全体は、通知の表示や関係のないウィンドウまで写る。ウィンドウ単位を選び直してもらう
        stream.getTracks().forEach((t) => t.stop());
        setError("画面全体は取り込めません。通話のウィンドウだけを選んでください"
          + "（画面全体には、通知や関係のない画面まで写ります）。");
        return;
      }
      this.stream = stream;
      track.addEventListener("ended", () => this.stopShare());
      this.video.srcObject = stream;
      this.video.hidden = this.pick.hidden = false;
      this.video.play().catch(() => {});
      setError("通話のウィンドウを共有しています。残したい場面で「この画面を写真にする」を押してください。"
        + "写真にするのは1コマだけで、映像も音声も記録しません。");
      syncConfirm();
    }

    stopShare() {
      if (this.stream) {
        this.stream.getTracks().forEach((t) => t.stop());
        this.stream = null;
      }
      if (this.video) {
        this.video.srcObject = null;
        this.video.hidden = this.pick.hidden = true;
      }
    }

    loadVideo(file) {
      this.reveal = false;
      this.closeVideo();
      const url = URL.createObjectURL(file);   // 端末の中だけの参照。サーバーへは送らない
      this.videoUrl = url;
      this.video.onloadeddata = () => {
        this.video.hidden = this.pick.hidden = false;
        // 読み込み直後は、最初のコマがまだ描かれておらず、すぐ取ると黒い画像になる。ごくわずかに進めて、コマを描かせる
        try { if (this.video.currentTime === 0) this.video.currentTime = 0.05; } catch (_) { /* 進められなくても、従来どおり */ }
        setError("");
      };
      this.video.onerror = () => {
        this.closeVideo();
        setError("この動画は、このブラウザでは開けませんでした。"
          + "別の端末（撮影した端末）で開くか、動画を一時停止した画面を撮って、その写真を選んでください。");
      };
      this.video.src = url;
      syncConfirm();
    }

    grabFrame() {
      const v = this.video;
      if (!v || !v.videoWidth) return;
      this.reveal = !!this.stream;   // 通話の画面から取り込んだときだけ、逆向き（全部隠して、見せる所を開ける）
      this.setSource(this.stream ? "call_screen" : "video_frame");
      // 作り手が「通話の録画として取り込む」を選んだ動画ファイルも、通話の画面と同じ扱い（全部隠して、開けた所だけが残る。デモ・検証用。既定は選ばない）
      const asRec = !this.stream && !!recAsCall && recAsCall.checked;
      if (asRec) { this.reveal = true; this.setSource("call_screen"); }
      if (this.stream || asRec) {
        // 通話の画面には第三者が写る。共有範囲の初期値を、最も狭いものにする（作り手は選び直せる）
        const sel = form.querySelector("select[name=scope]");
        if (sel && [...sel.options].some((o) => o.value === "invited_only")) sel.value = "invited_only";
      }
      const s = Math.min(1, MAX_SIDE / Math.max(v.videoWidth, v.videoHeight));
      this.canvas.width = Math.round(v.videoWidth * s);
      this.canvas.height = Math.round(v.videoHeight * s);
      const tmp = document.createElement("canvas");
      tmp.width = this.canvas.width; tmp.height = this.canvas.height;
      tmp.getContext("2d").drawImage(v, 0, 0, tmp.width, tmp.height);
      this.bitmap = tmp;          // 以降は写真と同じ扱い。drawImage は canvas も受け取る
      this.rects = [];
      this.canvas.hidden = this.help.hidden = false;
      this.closeVideo();          // 動画・共有は、コマを取ったらすぐ手放す
      this.redraw();
      syncConfirm();
    }

    closeVideo() {
      this.stopShare();
      if (this.video) {
        this.video.pause();
        this.video.removeAttribute("src");
        this.video.load();
        this.video.hidden = this.pick.hidden = true;
      }
      if (this.videoUrl) { URL.revokeObjectURL(this.videoUrl); this.videoUrl = null; }
    }

    redraw() {
      if (!this.bitmap) return;
      this.ctx.drawImage(this.bitmap, 0, 0, this.canvas.width, this.canvas.height);
      if (this.reveal) {
        // 通話の画面は、相手の顔・名前・チャット・通知が写る。見落としても漏れないよう、
        // まず全面を潰し、作り手がなぞった所だけを開ける（写真とは逆の向き）
        const shown = this.rects.map((r) => this.ctx.getImageData(r.x, r.y, r.w, r.h));
        pixelate(this.ctx, { x: 0, y: 0, w: this.canvas.width, h: this.canvas.height });
        this.rects.forEach((r, i) => {
          if (r.type === "path") {
            // なぞった形: 開ける所を、その形の中だけ、元に戻す。clip() は縁が半透明になり、形の外の 1 画素に元の色が混ざるので、
            // 形を白黒の面に描いて、しきい値（半分以上）で 0/1 に分け、画素ごとに「元」か「潰した色」かを決める
            const mc = document.createElement("canvas");
            mc.width = r.w; mc.height = r.h;
            const mg = mc.getContext("2d");
            mg.translate(-r.x, -r.y);
            this.tracePath(mg, r.pts, 1);
            mg.fill();
            const m = mg.getImageData(0, 0, r.w, r.h).data;
            const cur = this.ctx.getImageData(r.x, r.y, r.w, r.h);
            const src = shown[i].data;
            for (let k = 0; k < m.length; k += 4) {
              if (m[k + 3] >= 128) { cur.data[k] = src[k]; cur.data[k + 1] = src[k + 1]; cur.data[k + 2] = src[k + 2]; cur.data[k + 3] = 255; }
            }
            this.ctx.putImageData(cur, r.x, r.y);
          } else {
            this.ctx.putImageData(shown[i], r.x, r.y);
          }
        });
      } else {
        this.rects.forEach((r) => pixelate(this.ctx, r));
      }
      if (this.drag) {
        this.ctx.save();
        this.ctx.lineWidth = 3;
        this.ctx.strokeStyle = "#ff2d55";
        if (this.drag.pts) {   // なぞっている途中の軌跡
          this.ctx.beginPath();
          this.drag.pts.forEach((q, i) => (i ? this.ctx.lineTo(q[0], q[1]) : this.ctx.moveTo(q[0], q[1])));
          this.ctx.stroke();
        } else {
          const r = this.dragRect();
          this.ctx.strokeRect(r.x, r.y, r.w, r.h);
        }
        this.ctx.restore();
      }
      this.measure();
      this.undo.hidden = this.rects.length === 0;
      if (this.help) {
        this.help.textContent = this.reveal
          ? (isExtended()
            ? "通話の画面は、はじめは全部隠れています。残したい所を、四角でドラッグするか、指・マウスでなぞって囲んで開けてください。"
            : "通話の画面は、はじめは全部隠れています。カードに残したい所だけを指でなぞって開けてください。")
          : "顔・ナンバー・書類など、写ってはいけない所を指でなぞって隠してください。";
      }
    }

    // 点列 pts を、倍率 k で、閉じた経路として描く（塗りつぶし・切り抜き用）
    tracePath(ctx, pts, k) {
      ctx.beginPath();
      pts.forEach((q, i) => (i ? ctx.lineTo(q[0] * k, q[1] * k) : ctx.moveTo(q[0] * k, q[1] * k)));
      ctx.closePath();
    }

    // 開けた面積の割合（0〜1）を、形から正確に測る。小さな白黒の面に、開けた形を白で塗り、白の面積を数える（重なりは二重に数えない）
    measure() {
      if (!this.reveal || !isExtended() || !this.bitmap) {
        this.openRatio = 0;
        if (this.area) this.area.hidden = true;
        return;
      }
      const c = document.createElement("canvas");
      c.width = MEASURE_W; c.height = MEASURE_H;
      const g = c.getContext("2d");
      g.fillStyle = "#000"; g.fillRect(0, 0, MEASURE_W, MEASURE_H);
      g.fillStyle = "#fff";
      const kx = MEASURE_W / this.canvas.width, ky = MEASURE_H / this.canvas.height;
      for (const r of this.rects) {
        if (r.type === "path") {
          g.beginPath();
          r.pts.forEach((q, i) => (i ? g.lineTo(q[0] * kx, q[1] * ky) : g.moveTo(q[0] * kx, q[1] * ky)));
          g.closePath(); g.fill();
        } else {
          g.fillRect(r.x * kx, r.y * ky, r.w * kx, r.h * ky);
        }
      }
      const d = g.getImageData(0, 0, MEASURE_W, MEASURE_H).data;
      let white = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] > 127) white++;
      this.openRatio = white / (MEASURE_W * MEASURE_H);
      if (this.area) {
        const over = this.openRatio > OPEN_LIMIT;
        this.area.hidden = false;
        this.area.textContent = "いま開いている範囲: " + Math.round(this.openRatio * 100) + "%（上限 " + Math.round(OPEN_LIMIT * 100) + "%）"
          + (over ? " 開けすぎです。範囲を減らしてください。" : "");
        this.area.style.color = over ? "#c0182f" : "";
      }
    }

    pos(e) {
      const b = this.canvas.getBoundingClientRect();
      const sx = this.canvas.width / b.width;
      const sy = this.canvas.height / b.height;
      const cl = (v, m) => Math.max(0, Math.min(m, v));
      return { x: cl((e.clientX - b.left) * sx, this.canvas.width), y: cl((e.clientY - b.top) * sy, this.canvas.height) };
    }

    dragRect() {
      const a = this.drag.from, b = this.drag.to;
      return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
    }

    down(e) {
      if (!this.bitmap) return;
      this.canvas.setPointerCapture(e.pointerId);
      const p = this.pos(e);
      // なぞって囲む: 拡張モードの通話の画面で、道具が「なぞる」のとき
      if (this.reveal && isExtended() && toolSel && toolSel.value === "path") this.drag = { from: p, to: p, pts: [[p.x, p.y]] };
      else this.drag = { from: p, to: p };
    }

    move(e) {
      if (!this.drag) return;
      this.drag.to = this.pos(e);
      if (this.drag.pts) {
        const last = this.drag.pts[this.drag.pts.length - 1];
        if (Math.hypot(this.drag.to.x - last[0], this.drag.to.y - last[1]) >= 2) this.drag.pts.push([this.drag.to.x, this.drag.to.y]);
      }
      this.redraw();
    }

    up() {
      if (!this.drag) return;
      if (this.drag.pts) {   // なぞった軌跡を、閉じた形にする
        const pts = this.drag.pts.map((q) => [Math.round(q[0]), Math.round(q[1])]);
        this.drag = null;
        const xs = pts.map((q) => q[0]), ys = pts.map((q) => q[1]);
        const x = Math.min(...xs), y = Math.min(...ys), w = Math.max(...xs) - x, h = Math.max(...ys) - y;
        if (pts.length >= 3 && w >= MIN_RECT && h >= MIN_RECT) {
          this.rects.push({ type: "path", pts, x, y, w: Math.max(1, w), h: Math.max(1, h) });
          confirmBox.checked = false;
        }
        this.redraw();
        return;
      }
      const r = this.dragRect();
      this.drag = null;
      if (r.w >= MIN_RECT && r.h >= MIN_RECT) {
        this.rects.push({ x: Math.floor(r.x), y: Math.floor(r.y), w: Math.ceil(r.w), h: Math.ceil(r.h) });
        confirmBox.checked = false;  // 範囲を変えたら、確認をやり直す
      }
      this.redraw();
    }

    // マスキング後の JPEG。元の写真は含まない
    toBlob() {
      return new Promise((resolve) => this.canvas.toBlob(resolve, "image/jpeg", 0.85));
    }

    release() {
      // 動画から取り出したコマは canvas なので close() を持たない
      if (this.bitmap && typeof this.bitmap.close === "function") this.bitmap.close();
      this.bitmap = null;
      this.rects = [];
      this.drag = null;
      this.reveal = false;
      this.openRatio = 0;
      if (this.area) this.area.hidden = true;
      this.canvas.hidden = this.help.hidden = this.undo.hidden = true;
      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    }
  }

  const widgets = Array.from(form.querySelectorAll(".mask-widget")).map((el) => new Widget(el));
  const showTool = () => { if (toolWrap) toolWrap.hidden = !isExtended(); };
  if (modeSel) {
    modeSel.value = mode;   // 開き直したときの、ブラウザの復元値ではなく、旧来から始める
    showTool();
    modeSel.addEventListener("change", () => {
      mode = modeSel.value === "extended" ? "extended" : "legacy";
      if (toolSel && mode === "legacy") toolSel.value = "rect";
      showTool();
      // モードを変えたら、範囲を最初からやり直す（旧来の四角だけの範囲に、なぞった形が混ざらないように）
      widgets.forEach((w) => { w.rects = []; w.drag = null; if (w.bitmap) w.redraw(); });
      confirmBox.checked = false;
    });
  }
  const active = () => widgets.filter((w) => w.bitmap);

  function setError(msg) { errorEl.textContent = msg; }
  function syncConfirm() {
    const on = active().length > 0;
    confirmLabel.hidden = !on;
    if (!on) confirmBox.checked = false;
  }

  form.addEventListener("submit", async (ev) => {
    const photos = active();
    if (photos.length === 0) return;   // 写真なし: 通常のフォーム送信（文字だけ）
    ev.preventDefault();
    setError("");
    if (!confirmBox.checked) {
      setError("隠すべき箇所をすべて隠したことを確認してください。");
      return;
    }
    // 拡張モードの通話の画面: 開けすぎなら、送る前に止める（作り手が、その場で範囲を減らせる）
    const over = photos.find((w) => w.reveal && isExtended() && w.openRatio > OPEN_LIMIT);
    if (over) {
      setError("通話の画面の開けた範囲が広すぎます（" + Math.round(over.openRatio * 100) + "%・上限 " + Math.round(OPEN_LIMIT * 100) + "%）。範囲を減らしてください。");
      return;
    }
    const fd = new FormData();
    for (const el of form.elements) {
      if (el.name && !el.disabled) fd.append(el.name, el.value);  // 文字の項目だけ。ファイル入力は name がないので入らない
    }
    fd.append("mask_confirmed", "1");
    for (const w of photos) {
      fd.append("photo_" + w.role, await w.toBlob(), w.role + ".jpg");
      // 拡張モードの通話の画面だけ、端末が測った面積を送る（旧来モードは、従来どおり何も送らない）
      if (w.reveal && isExtended()) fd.append("open_ratio_" + w.role, w.openRatio.toFixed(4));
    }
    try {
      const res = await fetch(form.action, { method: "POST", body: fd, credentials: "same-origin" });
      if (res.redirected) {
        widgets.forEach((w) => w.release());
        location.assign(res.url);
      } else {
        setError("送信できませんでした（" + res.status + "）。写真を確認してください。");
      }
    } catch (_) {
      setError("送信できませんでした。通信を確認してください。");
    }
  });
})();
