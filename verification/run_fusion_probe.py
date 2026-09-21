#!/usr/bin/env python3
"""統合分析（app/fusion.py）を、本番の経路で、実 API を通して測る。台本: senario.txt（site.jpg を共有画面・デモ用ボイスで音声分析）。

    python run_fusion_probe.py --yes --transcribe-only              # 音声を文字にするだけ（安い）
    python run_fusion_probe.py --yes --repeats 3 --max-cost 0.6 [--open x,y,w,h ...] [--from-video]

- 画像: site.jpg を、端末のフィルターと同じ計算（make_call_scenes.reveal_filter）で全面ぼかし→開ける範囲だけ元に戻す。
  --from-video を付けると、画像を 1 コマの動画にして、コマを抜き出した画像を使う（動画が撮れないときの代わりの入力）。
- 音声: デモ用ボイス（m4a）を、ffmpeg で 16kHz・モノラルの wav にして使う（アプリの受け口は wav / mp3）。
- 通すもの: fusion.start → 文字の確認 → fusion.confirm_and_analyze（AI の呼び出しは本番の経路）。生成した xlsx / docx / 赤丸の画像は 結果/fusion_* に保存。
- キーは ORCA_API_KEY（表示しない）。
"""
from __future__ import annotations

import argparse, datetime as dt, io, json, os, pathlib, subprocess, sys, tempfile, time
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ.setdefault("MIRUCON_LLM_PROFILE", "orca")
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT))
from PIL import Image  # noqa: E402
import extract_frame as ef  # noqa: E402
from app import auth, authz, cards, db, fusion, jobs, llm, objects  # noqa: E402
from app.tests.fakes import client_dynamic  # noqa: E402

PHOTO = HERE / "assets" / "site.jpg"
VOICE = next((p for p in (HERE / "assets" / "demo_voice.wav", HERE / "assets" / "demo_voice.m4a") if p.exists()), HERE / "assets" / "demo_voice.wav")
C = {"reason": "r", "evidence": ""}


def _js_round(x):
    import math
    return int(math.floor(x + 0.5))


def _pixelate(arr, x, y, w, h):
    """mask.js の pixelate と同じ計算（ブロックごとの平均色）。"""
    import numpy as np
    block = max(10, _js_round(min(w, h) / 4))
    for by in range(0, h, block):
        for bx in range(0, w, block):
            bw, bh = min(block, w - bx), min(block, h - by)
            cell = arr[y + by:y + by + bh, x + bx:x + bx + bw, :3].astype(np.int64)
            arr[y + by:y + by + bh, x + bx:x + bx + bw, :3] = np.floor(cell.reshape(-1, 3).sum(axis=0) / (bw * bh) + 0.5).astype(np.uint8)


def _reveal_filter(img, opens):
    """全面を潰し、開ける範囲だけ元に戻す（mask.js の reveal モード）。"""
    import numpy as np
    arr = np.array(img.convert("RGB"))
    shown = [arr[y:y + h, x:x + w].copy() for x, y, w, h in opens]
    _pixelate(arr, 0, 0, arr.shape[1], arr.shape[0])
    for (x, y, w, h), s in zip(opens, shown):
        arr[y:y + h, x:x + w] = s
    return Image.fromarray(arr)


class mcs:  # noqa: N801
    reveal_filter = staticmethod(_reveal_filter)
DEFAULT_OPEN = "470,590,290,240"      # 油圧ショベルのバケット周り（1536×1024 の画素）


def wav_of(m4a: pathlib.Path) -> bytes:
    out = pathlib.Path(tempfile.mkdtemp(prefix="fvoice_")) / "v.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(m4a), "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(out)], check=True)
    return out.read_bytes()


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(); ap.add_argument("--yes", action="store_true"); ap.add_argument("--transcribe-only", action="store_true")
    ap.add_argument("--repeats", type=int, default=1); ap.add_argument("--max-cost", type=float, default=0.6)
    ap.add_argument("--open", action="append", default=None); ap.add_argument("--from-video", action="store_true"); a = ap.parse_args()
    opens = [tuple(int(v) for v in o.split(",")) for o in (a.open or [DEFAULT_OPEN])]
    print(f"音声 {VOICE.name} / 画像 {PHOTO.name} 開ける範囲 {opens} / 動画のコマ: {a.from_video} / 繰り返し {a.repeats}。dry-run: {not a.yes}")
    if not a.yes:
        return
    wav = wav_of(VOICE)
    src = PHOTO
    if a.from_video:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="fframe_")); v = tmp / "v.mp4"
        ef.make_video(PHOTO, v); src = tmp / "frame.jpg"; src.write_bytes(ef.to_input_jpeg(ef.grab(v, 0.7)))
    img = Image.open(src).convert("RGB")
    buf = io.BytesIO(); mcs.reveal_filter(img, opens).save(buf, "JPEG", quality=88); filtered = buf.getvalue()
    ratio = sum(w * h for _, _, w, h in opens) / (img.width * img.height)
    print(f"開けた面積 {ratio:.1%}（上限 20%）／フィルター後の画像 {len(filtered)} バイト")

    config = llm.load_config()
    config["providers"] = [p for p in config["providers"] if p in ("orca", "anthropic") and os.environ.get(p.upper() + "_API_KEY")]
    tmpd = pathlib.Path(tempfile.mkdtemp(prefix="fusion_")); conn = db.connect(tmpd / "f.db"); db.init(conn)
    db.run(conn, "INSERT INTO org VALUES('org_f','検証','info@example.test',?)", (db.now(),))
    db.run(conn, "INSERT INTO org_setting(org_id, external_llm, talk_llm, talk_audio, vision_llm, fusion_demo) VALUES('org_f',1,1,1,1,1)")
    mid = auth.add_member(conn, "org_f", "maker@example.test", "member"); conn.commit()
    actor = authz.Actor(kind="member", org_id="org_f", member_id=mid, role="member")
    obj = objects.register_object(conn, actor, "検証の現場")[0]
    fake = client_dynamic(lambda k, i: [("select_card_type", {**C, "type_id": "maintenance"})] if "select_card_type" in {t["name"] for t in k["tools"]}
                          else [("no_action", C)] if "propose_extra_mask" in {t["name"] for t in k["tools"]}
                          else [("write_card_text", {"title": "t", "changes": ["c"], "description": "d"})], echo_model=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S"); outd = HERE / "output" / f"fusion_{ts}"; outd.mkdir(parents=True, exist_ok=True)
    rows, total = [], 0.0
    real = llm.call
    def counting(*args, **kw):
        r = real(*args, **kw); counting.cost += r.cost_usd or 0; counting.n += 1; return r
    counting.cost, counting.n = 0.0, 0
    llm.call = counting
    orig_validate = fusion.validate
    def spy(inp, tnorm, names):   # 不採用のとき、人名らしい語を（この検証の画面にだけ）出す。アプリ本体には、名前を残さない
        res, why = orig_validate(inp, tnorm, names)
        if res is None and "人名" in "".join(why):
            from app import talk
            txt = json.dumps(inp, ensure_ascii=False)
            print("    [不採用の人名候補]", sorted(talk.person_names(txt)), [n for n in names])
        return res, why
    fusion.validate = spy
    try:
        for k in range(a.repeats):
            card = cards.create_card(conn, actor, obj["obj_id"], before_desc="現場の確認", after_desc="打合せ中", images_in=[("before", filtered, None, "call_screen", ratio)],
                                     mask_confirmed=True, client_factory=fake)["card"]
            image = db.one(conn, "SELECT * FROM image WHERE card_id=?", (card["card_id"],))
            t0 = time.time(); c0 = counting.cost
            fid = fusion.start(conn, actor, card["card_id"], image["image_id"], wav, "wav", consent=True, jobs=jobs.Inline(), config=config)
            st = fusion.get(conn, actor, fid); t1 = time.time()
            print(f"[{k + 1}] 文字（{st['status']}・{t1 - t0:.1f}秒・${counting.cost - c0:.4f}）")
            for t in st["transcript"]:
                print(f"   [{t['who']}] {t['text']}")
            row = {"run": k + 1, "transcript": st["transcript"], "status_audio": st["status"], "audio_sec": round(t1 - t0, 1)}
            if a.transcribe_only or st["status"] != "transcribed":
                rows.append(row); continue
            c1 = counting.cost; t2 = time.time()
            fusion.confirm_and_analyze(conn, actor, fid, None, jobs=jobs.Inline(), config=config)
            st = fusion.get(conn, actor, fid); t3 = time.time()
            row.update({"status": st["status"], "why": st["why"], "analyze_sec": round(t3 - t2, 1), "cost_analyze": round(counting.cost - c1, 4), "cost_total": round(counting.cost - c0, 4),
                        "result": st["result"], "files": st["files"]})
            print(f"    → {st['status']}（{t3 - t2:.1f}秒・分析 ${counting.cost - c1:.4f}）{st['why']}")
            if st["result"]:
                r = st["result"]; tg = [(x["label"], x["box"]) for x in r.get("targets", [])]
                print(f"    理解: {r['understanding']}\n    対象: {tg}\n    表: {r['table']['columns']} {len(r['table']['rows'])}行\n    メール件名: {r['email']['subject']}")
                for name in st["files"]:
                    (outd / f"run{k + 1}_{name}").write_bytes(fusion.download(conn, actor, fid, name)[1])
            rows.append(row)
            if counting.cost > a.max_cost:
                print("費用の上限で中断"); break
    finally:
        llm.call = real
    (outd / "raw.json").write_text(json.dumps({"opens": opens, "open_ratio": round(ratio, 4), "from_video": a.from_video, "cost": round(counting.cost, 4), "llm_calls": counting.n, "runs": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    (outd / "filtered_input.jpg").write_bytes(filtered)
    print(f"\n実費 ${counting.cost:.4f}／LLM {counting.n} 呼び出し／保存: {outd}")


if __name__ == "__main__":
    main()
