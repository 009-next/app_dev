import io

import pytest
from PIL import Image

from app import images


def _jpeg_with_exif() -> bytes:
    im = Image.new("RGB", (200, 100), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "TestMaker"  # Make
    exif[0x0112] = 1
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_input_has_exif_and_output_has_none():
    raw = _jpeg_with_exif()
    assert images.has_metadata(raw)  # テストの前提: 入力には EXIF がある
    assert not images.has_metadata(images.process(raw))


def test_blur_changes_pixels_only_inside_rect():
    raw = _jpeg_with_exif()
    plain = Image.open(io.BytesIO(images.process(raw))).convert("RGB")
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    for x in range(100, 200):
        for y in range(100):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    blurred = Image.open(io.BytesIO(images.process(buf.getvalue(), [[0.4, 0.0, 0.2, 1.0]]))).convert("RGB")
    assert blurred.getpixel((100, 50)) not in ((0, 0, 0), (255, 255, 255))  # 境界がぼけた
    assert blurred.getpixel((5, 50))[0] > 240  # 範囲外は白のまま
    assert plain.size == (200, 100)


@pytest.mark.parametrize("bad", [[[0, 0, 1]], [[-0.1, 0, 0.5, 0.5]], [[0, 0, 0, 0.5]], [["a", 0, 0.5, 0.5]]])
def test_bad_blur_rects_are_rejected(bad):
    with pytest.raises(images.ImageError):
        images.process(_jpeg_with_exif(), bad)


def test_non_image_and_oversize_are_rejected():
    with pytest.raises(images.ImageError):
        images.process(b"not an image")
    with pytest.raises(images.ImageError):
        images.process(b"0" * (images.MAX_BYTES + 1))
