"""画像の取り込み: EXIF などのメタデータの除去（N3）と、手動のぼかし領域の適用。

- 元の画像は保存しない。ピクセルだけを新しい画像へ写し、メタデータ（位置・日時・端末）を持ち越さない。
- 顔・ナンバーの自動検出は、この縦切りの範囲外。作り手が指定した領域だけをぼかす。
"""

from __future__ import annotations

import io
import math

from PIL import Image, ImageFilter, ImageOps

MAX_BYTES = 5_000_000
MAX_SIDE = 1600


class ImageError(ValueError):
    pass


def process(raw: bytes, blur_rects: list[list[float]] | None = None) -> bytes:
    """JPEG のバイト列を返す。blur_rects は [x, y, w, h]（0〜1 の比率）の一覧。"""
    if len(raw) > MAX_BYTES:
        raise ImageError("画像が大きすぎます")
    try:
        Image.open(io.BytesIO(raw)).verify()
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:  # 壊れた画像・画像でないファイル
        raise ImageError("画像として読み取れません") from e
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    w, h = im.size
    for rect in blur_rects or []:
        if len(rect) != 4 or not all(isinstance(v, (int, float)) for v in rect):
            raise ImageError("ぼかしの領域の形式が正しくありません")
        x, y, rw, rh = rect
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < rw <= 1 and 0 < rh <= 1):
            raise ImageError("ぼかしの領域が範囲外です")
        box = (int(x * w), int(y * h), min(w, int((x + rw) * w)), min(h, int((y + rh) * h)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        region = im.crop(box).filter(ImageFilter.GaussianBlur(radius=max(12, (box[2] - box[0]) // 6)))
        im.paste(region, box)
    clean = Image.frombytes("RGB", im.size, im.tobytes())  # メタデータを持ち越さない
    out = io.BytesIO()
    clean.save(out, "JPEG", quality=85)
    return out.getvalue()


MAX_RECTS = 20
MIN_SIDE = 0.01  # 比率。これより小さい範囲は、タップの誤操作として拒否する


def check_rects(rects) -> list[tuple[float, float, float, float]]:
    """[x, y, w, h]（0〜1 の比率）の一覧を検証する。壊れた値・範囲外・小さすぎる範囲・多すぎる件数は ImageError。"""
    if not isinstance(rects, list) or not rects or len(rects) > MAX_RECTS:
        raise ImageError("ぼかしの領域は、1〜%d 件で指定してください" % MAX_RECTS)
    out = []
    for r in rects:
        if not (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in r)):
            raise ImageError("ぼかしの領域の形式が正しくありません")
        x, y, w, h = map(float, r)
        if not (0 <= x <= 1 and 0 <= y <= 1 and w >= MIN_SIDE and h >= MIN_SIDE and x + w <= 1.0001 and y + h <= 1.0001):
            raise ImageError("ぼかしの領域が範囲外、または小さすぎます")
        out.append((x, y, w, h))
    return out


def mosaic(raw: bytes, rects) -> bytes:
    """すでに処理済みの画像に、モザイクを追加する（ブロックごとの平均色。元に戻せない）。端末側の pixelate と同じ粗さ。
    元の写真は保存していないので、この画像の上にさらに重ねる形で隠す。出力は process() を通し、メタデータを持ち越さない。"""
    boxes = check_rects(rects)
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
    except Exception as e:
        raise ImageError("画像として読み取れません") from e
    w, h = im.size
    for x, y, rw, rh in boxes:
        box = (int(x * w), int(y * h), min(w, math.ceil((x + rw) * w)), min(h, math.ceil((y + rh) * h)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        region = im.crop(box)
        bw, bh = region.size
        block = max(10, round(min(bw, bh) / 4))
        small = region.resize((max(1, math.ceil(bw / block)), max(1, math.ceil(bh / block))), Image.BOX)
        im.paste(small.resize((bw, bh), Image.NEAREST), box)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return process(buf.getvalue())


def has_metadata(data: bytes) -> bool:
    """テスト用: EXIF が残っていないか。"""
    im = Image.open(io.BytesIO(data))
    return bool(im.getexif()) or "exif" in im.info or "icc_profile" in im.info
