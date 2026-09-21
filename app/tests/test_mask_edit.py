"""保存済みの写真に、隠す範囲を追加する操作（提案から直接指定）のテスト。"""

import io
import math
import pathlib

import pytest
from PIL import Image

from app import auth, cards, db, images, objects, web
from app.tests.test_web import get, login, post, text


def stripes(w=200, h=200) -> bytes:
    """縦縞（黒白）の JPEG。モザイクで縞が消えるかを見る。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(0, w, 8):
        for xx in range(x, min(w, x + 4)):
            for y in range(h):
                im.putpixel((xx, y), (0, 0, 0))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


def contrast(jpeg: bytes, box) -> int:
    im = Image.open(io.BytesIO(jpeg)).convert("L").crop(box)
    px = list(im.getdata())
    return max(px) - min(px)


@pytest.fixture(autouse=True)
def _reset():
    web.reset_limits()
    auth.reset_rate()
    web.JOBS = web.jobs.Inline()
    yield
    web.JOBS = web.jobs.Inline()


@pytest.fixture
def card(conn, alice, tmp_path):
    obj = objects.register_object(conn, alice, "502号室")[0]
    c = cards.create_card(conn, alice, obj["obj_id"], images_in=[("before", stripes(), None)], data_dir=tmp_path, scope="org_only")["card"]
    img = db.one(conn, "SELECT * FROM image WHERE card_id=?", (c["card_id"],))
    return c, img


# ---- images.mosaic ----------------------------------------------------------------

def test_mosaic_removes_detail_inside_only_and_strips_metadata():
    raw = stripes()
    out = images.mosaic(raw, [[0.25, 0.25, 0.5, 0.5]])
    assert contrast(raw, (60, 60, 140, 140)) > 200 and contrast(out, (60, 60, 140, 140)) < 120  # 縞（黒白）が、平均色に潰れる
    assert contrast(out, (0, 0, 40, 40)) > 200  # 範囲の外は、そのまま
    assert not images.has_metadata(out)


@pytest.mark.parametrize("bad", [[], "x", [[0.1, 0.1, 0.5]], [["a", 0.1, 0.3, 0.3]], [[-0.1, 0.1, 0.3, 0.3]], [[0.1, 0.1, 0.005, 0.5]],
                                 [[0.8, 0.1, 0.5, 0.5]], [[float("nan"), 0, 0.5, 0.5]], [[True, 0, 0.5, 0.5]], [[0, 0, 0.1, 0.1]] * 21, None])
def test_bad_rects_are_rejected(bad):
    with pytest.raises(images.ImageError):
        images.mosaic(stripes(), bad)


def test_mosaic_rejects_non_image():
    with pytest.raises(images.ImageError):
        images.mosaic(b"junk", [[0, 0, 0.5, 0.5]])


# ---- cards.add_mask_rects ------------------------------------------------------------

def test_add_mask_rects_rewrites_the_stored_file_and_audits(conn, alice, card):
    c, img = card
    before = pathlib.Path(img["path"]).read_bytes()
    cards.add_mask_rects(conn, alice, c["card_id"], img["image_id"], [[0.25, 0.25, 0.5, 0.5]])
    after = pathlib.Path(img["path"]).read_bytes()
    assert after != before and contrast(after, (60, 60, 140, 140)) < 120
    assert not list(pathlib.Path(img["path"]).parent.glob("*.tmp"))  # 一時ファイルを残さない
    row = db.one(conn, "SELECT detail FROM audit_log WHERE action='mask.add'")
    assert row["detail"] == "1か所"


def test_add_mask_rects_clears_the_ai_proposal_and_records_it(conn, alice, card):
    c, img = card
    db.run(conn, "UPDATE card SET proposed_mask='{\"target\": \"宛名\", \"reason\": \"r\"}' WHERE card_id=?", (c["card_id"],))
    cards.add_mask_rects(conn, alice, c["card_id"], img["image_id"], [[0.1, 0.1, 0.3, 0.3]])
    assert db.one(conn, "SELECT proposed_mask FROM card")["proposed_mask"] is None
    assert db.one(conn, "SELECT COUNT(*) c FROM audit_log WHERE action='mask.proposal_applied'")["c"] == 1


def test_invalid_rects_leave_the_file_untouched(conn, alice, card):
    c, img = card
    before = pathlib.Path(img["path"]).read_bytes()
    with pytest.raises(images.ImageError):
        cards.add_mask_rects(conn, alice, c["card_id"], img["image_id"], [[0.1, 0.1, 5, 5]])
    assert pathlib.Path(img["path"]).read_bytes() == before
    assert db.one(conn, "SELECT COUNT(*) c FROM audit_log WHERE action='mask.add'")["c"] == 0


def test_add_mask_rects_permissions_and_ids(conn, alice, outsider, card, tmp_path):
    c, img = card
    with pytest.raises(objects.NotFound):
        cards.add_mask_rects(conn, outsider, c["card_id"], img["image_id"], [[0, 0, 0.5, 0.5]])
    other = cards.create_card(conn, alice, db.one(conn, "SELECT obj_id FROM object")["obj_id"], images_in=[("after", stripes(), None)],
                              data_dir=tmp_path)["card"]
    with pytest.raises(objects.NotFound):  # 別のカードの写真は、このカードからは触れない
        cards.add_mask_rects(conn, alice, other["card_id"], img["image_id"], [[0, 0, 0.5, 0.5]])
    with pytest.raises(objects.NotFound):
        cards.add_mask_rects(conn, alice, c["card_id"], "img_nope", [[0, 0, 0.5, 0.5]])
    pathlib.Path(img["path"]).unlink()
    with pytest.raises(objects.NotFound):
        cards.add_mask_rects(conn, alice, c["card_id"], img["image_id"], [[0, 0, 0.5, 0.5]])


# ---- Web ---------------------------------------------------------------------------

def test_edit_page_requires_login_and_uses_only_external_script(conn, alice, card):
    c, img = card
    url = f"/c/{c['card_id']}/mask/{img['image_id']}"
    assert get(conn, url).status == 303
    r = get(conn, url, cookie=login(conn, alice))
    body = text(r)
    assert r.status == 200 and f'data-src="/mimg/{img["image_id"]}"' in body
    assert '<script src="/static/mask-edit.js" defer></script>' in body and body.count("<script") == 1


def test_edit_page_hides_other_orgs_and_wrong_ids(conn, alice, outsider, card):
    c, img = card
    assert get(conn, f"/c/{c['card_id']}/mask/{img['image_id']}", cookie=login(conn, outsider)).status == 404
    assert get(conn, f"/c/{c['card_id']}/mask/img_nope", cookie=login(conn, alice)).status == 404


def test_post_rects_saves_and_redirects_and_rejects_bad_input(conn, alice, card):
    c, img = card
    ck = login(conn, alice)
    url = f"/c/{c['card_id']}/mask/{img['image_id']}"
    before = pathlib.Path(img["path"]).read_bytes()
    assert post(conn, url, {"rects": "not json"}, cookie=ck).status == 400
    assert post(conn, url, {"rects": "[[0.1,0.1,9,9]]"}, cookie=ck).status == 400
    assert post(conn, url, {"rects": "[]"}, cookie=ck).status == 400
    assert pathlib.Path(img["path"]).read_bytes() == before
    r = post(conn, url, {"rects": "[[0.25,0.25,0.5,0.5]]"}, cookie=ck)
    assert r.status == 303 and dict(r.headers)["Location"] == f"/c/{c['card_id']}"
    assert pathlib.Path(img["path"]).read_bytes() != before


def test_post_from_other_origin_and_without_login_do_nothing(conn, alice, card):
    c, img = card
    url = f"/c/{c['card_id']}/mask/{img['image_id']}"
    before = pathlib.Path(img["path"]).read_bytes()
    assert post(conn, url, {"rects": "[[0.25,0.25,0.5,0.5]]"}, cookie=login(conn, alice), headers={"origin": "http://evil.example"}).status == 403
    assert post(conn, url, {"rects": "[[0.25,0.25,0.5,0.5]]"}).status == 303
    assert pathlib.Path(img["path"]).read_bytes() == before


def test_card_page_links_to_the_editor_and_proposal_links_directly(conn, alice, card):
    c, img = card
    ck = login(conn, alice)
    page = text(get(conn, f"/c/{c['card_id']}", cookie=ck))
    assert f'/c/{c["card_id"]}/mask/{img["image_id"]}' in page and "この写真に隠す範囲を追加する" in page and "追加のぼかし" not in page
    db.run(conn, "UPDATE card SET proposed_mask='{\"target\": \"宛名\", \"reason\": \"r\"}' WHERE card_id=?", (c["card_id"],))
    page = text(get(conn, f"/c/{c['card_id']}", cookie=ck))
    assert "の写真で、隠す範囲を指定する" in page  # 提案の枠から、直接、範囲の指定へ
    post(conn, f"/c/{c['card_id']}/mask/{img['image_id']}", {"rects": "[[0.1,0.1,0.3,0.3]]"}, cookie=ck)
    assert "追加のぼかし" not in text(get(conn, f"/c/{c['card_id']}", cookie=ck))  # 対応すると、提案は消える


def test_shared_image_shows_the_edited_file_and_static_is_whitelisted(conn, alice, card):
    c, img = card
    db.run(conn, "UPDATE card SET scope='link_30d' WHERE card_id=?", (c["card_id"],))
    db.run(conn, "UPDATE image SET mask_confirmed=1")
    s = cards.issue_share(conn, alice, c["card_id"], confirmed=True)
    cards.add_mask_rects(conn, alice, c["card_id"], img["image_id"], [[0.25, 0.25, 0.5, 0.5]])
    import re
    src = re.search(r'src="(/img/[^"]+)"', text(get(conn, f"/s/{s['token']}"))).group(1).replace("&amp;", "&")
    served = get(conn, src)
    assert served.status == 200 and contrast(served.body, (60, 60, 140, 140)) < 120  # すでに発行した共有にも、すぐ反映される
    js = web.handle(None, "GET", "/static/mask-edit.js", {"host": "localhost"})
    assert js.status == 200 and js.content_type.startswith("application/javascript")
    assert web.handle(None, "GET", "/static/..", {"host": "localhost"}).status == 404


def test_edit_script_only_reads_the_image_and_sends_only_rectangles():
    src = "\n".join(l for l in (web.STATIC_DIR / "mask-edit.js").read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("//"))
    for banned in ["fetch(", "XMLHttpRequest", "sendBeacon", "toBlob", "toDataURL", "localStorage", "indexedDB"]:
        assert banned not in src
    assert "JSON.stringify(rects)" in src
