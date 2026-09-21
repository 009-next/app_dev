(() => {
  "use strict";
  const MAX_SIDE = 1600;
  const widgets = [...document.querySelectorAll(".mask-widget")].map(root => {
    const input = root.querySelector(".mask-file");
    const canvas = root.querySelector(".mask-canvas");
    const undo = root.querySelector(".mask-undo");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    let bitmap = null; let start = null; let rects = [];
    const redraw = () => {
      if (!bitmap) return;
      ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      for (const rect of rects) {
        const block = Math.max(12, Math.round(Math.min(rect.width, rect.height) / 4));
        const sample = document.createElement("canvas");
        sample.width = Math.max(1, Math.ceil(rect.width / block)); sample.height = Math.max(1, Math.ceil(rect.height / block));
        sample.getContext("2d").drawImage(canvas, rect.x, rect.y, rect.width, rect.height, 0, 0, sample.width, sample.height);
        ctx.imageSmoothingEnabled = false; ctx.drawImage(sample, 0, 0, sample.width, sample.height, rect.x, rect.y, rect.width, rect.height); ctx.imageSmoothingEnabled = true;
      }
    };
    input.addEventListener("change", async () => {
      bitmap?.close(); bitmap = null; rects = [];
      const file = input.files?.[0]; if (!file) { canvas.hidden = true; undo.hidden = true; return; }
      if (file.size > 5_000_000) { input.value = ""; throw new Error("画像は5MB以下にしてください。"); }
      bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
      canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.hidden = false; undo.hidden = false; redraw();
    });
    canvas.addEventListener("pointerdown", event => { const box = canvas.getBoundingClientRect(); start = { x: (event.clientX - box.left) * canvas.width / box.width, y: (event.clientY - box.top) * canvas.height / box.height }; canvas.setPointerCapture(event.pointerId); });
    canvas.addEventListener("pointerup", event => {
      if (!start) return; const box = canvas.getBoundingClientRect();
      const end = { x: (event.clientX - box.left) * canvas.width / box.width, y: (event.clientY - box.top) * canvas.height / box.height };
      const rect = { x: Math.max(0, Math.min(start.x, end.x)), y: Math.max(0, Math.min(start.y, end.y)), width: Math.abs(end.x - start.x), height: Math.abs(end.y - start.y) };
      start = null; if (rect.width >= 8 && rect.height >= 8) { rects.push(rect); redraw(); }
    });
    undo.addEventListener("click", () => { rects.pop(); redraw(); });
    return {
      role: root.dataset.role,
      selected: () => Boolean(bitmap),
      async upload(api, objectId) {
        if (!bitmap) return null;
        return api("/api/images", { objectId, role: root.dataset.role, confirmed: true, dataUrl: canvas.toDataURL("image/jpeg", 0.85), extraMasks: [] });
      },
    };
  });
  window.miruMasks = {
    async prepareUploads(api, objectId) {
      const active = widgets.filter(widget => widget.selected());
      if (!active.length) return { beforeImageRefs: [], afterImageRefs: [] };
      if (active.length !== 2) throw new Error("写真を追加する場合は、作業前・作業後を両方選択してください。");
      if (!document.getElementById("maskConfirmed").checked) throw new Error("写真のマスキング確認にチェックしてください。");
      const results = await Promise.all(active.map(widget => widget.upload(api, objectId)));
      return {
        beforeImageRefs: results.filter((_, index) => active[index].role === "before").map(result => result.ref),
        afterImageRefs: results.filter((_, index) => active[index].role === "after").map(result => result.ref),
      };
    },
  };
})();
