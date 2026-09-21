// 保存済みの写真に、隠す範囲を追加する画面。範囲は 0〜1 の比率で、フォームの rects に入れて送る。
// モザイクにするのはサーバー（images.mosaic）。ここでは、範囲を指定するだけで、写真を加工して送ることはしない。
(() => {
  "use strict";
  const MIN_SIDE = 0.01;  // サーバー側 images.MIN_SIDE と同じ。これより小さい範囲は無視する
  const MAX_RECTS = 20;   // サーバー側 images.MAX_RECTS と同じ
  const canvas = document.getElementById("mask-edit-canvas");
  const form = document.getElementById("mask-edit-form");
  if (!canvas || !form) return;
  const ctx = canvas.getContext("2d");
  const field = form.querySelector('input[name="rects"]');
  const save = document.getElementById("mask-edit-save");
  const undo = document.getElementById("mask-edit-undo");
  const count = document.getElementById("mask-edit-count");
  const rects = [];   // [x, y, w, h] の比率
  let drag = null;
  let img = null;

  function redraw() {
    if (!img) return;
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const draw = (r, live) => {
      ctx.fillStyle = live ? "rgba(255,45,85,0.25)" : "rgba(0,0,0,0.55)";
      ctx.fillRect(r[0] * canvas.width, r[1] * canvas.height, r[2] * canvas.width, r[3] * canvas.height);
      ctx.lineWidth = 3;
      ctx.strokeStyle = "#ff2d55";
      ctx.strokeRect(r[0] * canvas.width, r[1] * canvas.height, r[2] * canvas.width, r[3] * canvas.height);
    };
    rects.forEach((r) => draw(r, false));
    if (drag) draw(dragRect(), true);
  }

  function sync() {
    field.value = JSON.stringify(rects);
    save.disabled = rects.length === 0;
    undo.hidden = rects.length === 0;
    count.textContent = rects.length ? rects.length + "か所を指定しています" : "";
    redraw();
  }

  function pos(e) {
    const b = canvas.getBoundingClientRect();
    const cl = (v) => Math.max(0, Math.min(1, v));
    return { x: cl((e.clientX - b.left) / b.width), y: cl((e.clientY - b.top) / b.height) };
  }

  function dragRect() {
    const a = drag.from, b = drag.to;
    return [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(a.x - b.x), Math.abs(a.y - b.y)];
  }

  canvas.addEventListener("pointerdown", (e) => {
    if (!img) return;
    canvas.setPointerCapture(e.pointerId);
    const p = pos(e);
    drag = { from: p, to: p };
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    drag.to = pos(e);
    redraw();
  });
  canvas.addEventListener("pointerup", () => {
    if (!drag) return;
    const r = dragRect();
    drag = null;
    if (r[2] >= MIN_SIDE && r[3] >= MIN_SIDE && rects.length < MAX_RECTS) rects.push(r.map((v) => Math.round(v * 10000) / 10000));
    sync();
  });
  canvas.addEventListener("pointercancel", () => { drag = null; redraw(); });
  undo.addEventListener("click", () => { rects.pop(); sync(); });

  img = new Image();
  img.onload = () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.hidden = false;
    canvas.style.touchAction = "none";
    canvas.style.maxWidth = "100%";
    canvas.style.height = "auto";
    sync();
  };
  img.onerror = () => { count.textContent = "写真を読み込めませんでした。"; img = null; };
  img.src = canvas.dataset.src;
})();
