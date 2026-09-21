#!/usr/bin/env python3
"""動画の 1 コマを取り出して、視覚分析の入力（JPEG・幅 1600 以下）として使えるかを確かめる（検証ツール。アプリの依存ではない）。

    python extract_frame.py [画像] [--out 出力フォルダ]

1. 画像を 1 コマとする短い動画（mp4・30fps・2 秒）を作る。2. 動画の途中のコマを VideoCapture で抜く。3. 元画像との画素差・大きさ・JPEG 化後の大きさを出す。
"""
import argparse, io, json, pathlib, sys
import cv2, numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT = HERE / "assets" / "site.jpg"


def make_video(img_path: pathlib.Path, out: pathlib.Path, seconds: float = 2.0, fps: int = 30) -> tuple[int, int]:
    im = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = im.shape[:2]
    w2, h2 = w - w % 2, h - h % 2  # mp4 は偶数の大きさ
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w2, h2))
    if not vw.isOpened():
        raise RuntimeError("動画を書き出せません")
    for _ in range(int(seconds * fps)):
        vw.write(im[:h2, :w2])
    vw.release()
    return w2, h2


def grab(video: pathlib.Path, at_sec: float) -> np.ndarray:
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_MSEC, at_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("コマを取り出せません")
    return frame


def to_input_jpeg(frame: np.ndarray, max_w: int = 1600) -> bytes:
    im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)))
    b = io.BytesIO(); im.save(b, "JPEG", quality=85); return b.getvalue()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(); ap.add_argument("image", nargs="?", default=str(DEFAULT)); ap.add_argument("--out", default=str(HERE / "output")); a = ap.parse_args()
    src = pathlib.Path(a.image); out = pathlib.Path(a.out); out.mkdir(exist_ok=True)
    video = out / "demo_frame_source.mp4"
    w, h = make_video(src, video)
    res = {}
    for t in (0.0, 0.7, 1.5):
        f = grab(video, t)
        orig = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_COLOR)[:h, :w]
        diff = float(np.mean(np.abs(f.astype(np.int16) - orig.astype(np.int16))))
        res[str(t)] = {"size": f.shape[1::-1], "mean_abs_diff": round(diff, 2)}
    jpg = to_input_jpeg(grab(video, 0.7))
    (out / "demo_frame_extracted.jpg").write_bytes(jpg)
    im = Image.open(io.BytesIO(jpg))
    res["jpeg"] = {"bytes": len(jpg), "size": im.size, "format": im.format}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    ok = all(v["mean_abs_diff"] < 6 for k, v in res.items() if k != "jpeg") and im.width <= 1600
    print("コマ取り:", "可能（平均の画素差 6 未満・幅 1600 以下）" if ok else "要確認")


if __name__ == "__main__":
    main()
