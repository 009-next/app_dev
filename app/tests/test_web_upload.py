"""P-4: 写真の受け入れ（マスキング後の JPEG だけ）と、静的ファイル配信のテスト。"""

import io
import pathlib
import re

import pytest
from PIL import Image

from app import auth, cards, db, images, objects, web
from app.tests.fakes import client_sequence
from app.tests.test_web import HOST, COMMON, WRITE, get, login, text

B = "----testboundary"


def multipart(fields: dict, files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    out = b""
    for k, v in fields.items():
        out += f'--{B}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, data in files:
        out += (f'--{B}\r\nContent-Disposition: form-data; name="{name}"; filename="x.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{B}--\r\n".encode(), f"multipart/form-data; boundary={B}"


def send(conn, o, cookie, fields, files, **kw):
    body, ctype = multipart(fields, files)
    headers = {**HOST, "cookie": cookie, "content-type": ctype, **kw.get("headers", {})}
    return web.handle(conn, "POST", f"/o/{o['obj_id']}/cards", headers, body)


@pytest.fixture(autouse=True)
def _reset():
    web.reset_limits()
    auth.reset_rate()
    web.CLIENT_FACTORY = None
    yield
    web.CLIENT_FACTORY = None


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "DATA_DIR", tmp_path)


def test_masked_photo_is_accepted_and_reencoded(conn, alice, obj, jpeg):
    r = send(conn, obj, login(conn, alice), {"before_desc": "x", "scope": "org_only", "mask_confirmed": "1"},
             [("photo_before", jpeg), ("photo_after", jpeg)])
    assert r.status == 303
    rows = db.many(conn, "SELECT * FROM image ORDER BY role")
    assert [x["role"] for x in rows] == ["after", "before"] and all(x["mask_confirmed"] == 1 for x in rows)
    from pathlib import Path
    assert not images.has_metadata(Path(rows[0]["path"]).read_bytes())


def test_photo_without_mask_confirmation_is_rejected_and_nothing_saved(conn, alice, obj, jpeg):
    r = send(conn, obj, login(conn, alice), {"before_desc": "x"}, [("photo_before", jpeg)])
    assert r.status == 400 and "確認" in text(r)
    assert db.one(conn, "SELECT COUNT(*) c FROM card")["c"] == 0 and db.one(conn, "SELECT COUNT(*) c FROM image")["c"] == 0


@pytest.mark.parametrize("name,data", [("photo_evil", b"x"), ("photo_before", b"not an image"), ("photo_before", b"")])
def test_bad_photo_parts_are_rejected(conn, alice, obj, name, data):
    r = send(conn, obj, login(conn, alice), {"mask_confirmed": "1"}, [(name, data)])
    assert r.status == 400
    assert db.one(conn, "SELECT COUNT(*) c FROM card")["c"] == 0


def test_too_many_photos_and_oversize(conn, alice, obj, jpeg):
    ck = login(conn, alice)
    assert send(conn, obj, ck, {"mask_confirmed": "1"}, [("photo_before", jpeg)] * 3).status == 400
    huge = b"0" * (web.MAX_UPLOAD + 1)
    assert web.handle(conn, "POST", f"/o/{obj['obj_id']}/cards",
                      {**HOST, "cookie": ck, "content-type": f"multipart/form-data; boundary={B}"}, huge).status == 413


def test_large_body_is_only_allowed_for_the_upload_route(conn, alice):
    ck = login(conn, alice)
    body = b"name=" + b"a" * (web.MAX_BODY + 1)
    assert web.handle(conn, "POST", "/objects", {**HOST, "cookie": ck, "content-type": f"multipart/form-data; boundary={B}"}, body).status == 413


def test_upload_from_other_origin_is_rejected(conn, alice, obj, jpeg):
    r = send(conn, obj, login(conn, alice), {"mask_confirmed": "1"}, [("photo_before", jpeg)],
             headers={"origin": "http://evil.example"})
    assert r.status == 403 and db.one(conn, "SELECT COUNT(*) c FROM image")["c"] == 0


def test_llm_receives_text_only_never_image_bytes(conn, alice, obj, jpeg):
    seen = []

    def factory(provider):
        def create(**kw):
            seen.append(kw)
            raise RuntimeError("stop")
        from types import SimpleNamespace as N
        return N(messages=N(with_raw_response=N(create=create)))

    web.CLIENT_FACTORY = factory
    send(conn, obj, login(conn, alice), {"before_desc": "エアコン", "mask_confirmed": "1"}, [("photo_before", jpeg)])
    assert seen
    for kw in seen:
        assert all(isinstance(m["content"], str) for m in kw["messages"])  # 画像ブロックは渡さない


def test_upload_then_share_view_shows_masked_image_only_via_signed_url(conn, alice, obj, jpeg):
    ck = login(conn, alice)
    r = send(conn, obj, ck, {"before_desc": "x", "mask_confirmed": "1"}, [("photo_before", jpeg)])
    card_id = dict(r.headers)["Location"].rsplit("/", 1)[1]
    s = cards.issue_share(conn, alice, card_id, confirmed=True)
    page = text(get(conn, f"/s/{s['token']}"))
    assert re.search(r'src="/img/img_[^"]+\?u=share:', page)
    assert "/mimg/" not in page


def test_static_serves_only_mask_js():
    ok = web.handle(None, "GET", "/static/mask.js", HOST)
    assert ok.status == 200 and ok.content_type.startswith("application/javascript") and b"createImageBitmap" in ok.body
    for bad in ["/static/../db.py", "/static/db.py", "/static/%2e%2e/db.py", "/static/"]:
        assert web.handle(None, "GET", bad, HOST).status == 404


def test_card_form_uses_external_script_and_unnamed_file_inputs(conn, alice, obj):
    r = get(conn, f"/o/{obj['obj_id']}/new", cookie=login(conn, alice))
    body = text(r)
    assert '<script src="/static/mask.js" defer></script>' in body
    assert len(re.findall(r"<script", body)) == 1  # インラインの script はない
    inputs = re.findall(r'<input type="file"[^>]*>', body)
    assert inputs and all("name=" not in i for i in inputs)  # 元の写真の入力欄はフォームに入らない
    csp = dict(r.all_headers())["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline'; img" in csp and "script-src 'unsafe" not in csp


def test_mask_js_never_persists_or_uploads_original():
    lines = (web.STATIC_DIR / "mask.js").read_text(encoding="utf-8").splitlines()
    src = "\n".join(l for l in lines if not l.lstrip().startswith("//"))  # コメントは対象外
    for banned in ["localStorage", "sessionStorage", "indexedDB", "caches.", "toDataURL", "sendBeacon", "XMLHttpRequest"]:
        assert banned not in src
    assert src.count("fetch(") == 1 and "toBlob" in src


def test_the_masking_script_tells_the_user_why_a_video_cannot_be_used():
    """動画を選んだ人に「写真として読めない」とだけ返さない。何をすればよいかまで書く。"""
    js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "mask.js").read_text(encoding="utf-8")
    assert 'file.type.startsWith("video/")' in js and "mp4|mov" in js
    assert "動画は取り込めません" in js and "一時停止した画面" in js


# ---- 動画から1コマを選ぶ（案A）。動画そのものは、端末から出さない ----------------

def mask_js() -> str:
    return (web.STATIC_DIR / "mask.js").read_text(encoding="utf-8")


def test_the_form_accepts_video_but_never_names_the_original_file(conn, alice):
    """動画も選べるが、元ファイルの input に name はない＝フォームに入らない。"""
    obj = objects.register_object(conn, alice, "空調")[0]
    body = get(conn, f'/o/{obj["obj_id"]}/new', login(conn, alice)).body.decode()
    assert 'accept="image/*,video/*"' in body
    assert 'class="mask-file"' in body and 'name="mask-file"' not in body
    assert '<video class="mask-video"' in body and 'class="mask-pick"' in body


def test_the_page_allows_local_media_but_no_external_source(conn, alice):
    """<video> を端末の中の参照（blob:）でだけ開けるようにする。外部の URL は許さない。"""
    obj = objects.register_object(conn, alice, "空調")[0]
    csp = dict(get(conn, f'/o/{obj["obj_id"]}/new', login(conn, alice)).all_headers())["Content-Security-Policy"]
    assert "media-src blob:" in csp
    assert "default-src 'none'" in csp and "media-src *" not in csp and "media-src https:" not in csp


def test_the_script_takes_one_frame_and_lets_the_video_go():
    js = mask_js()
    assert "grabFrame" in js and "drawImage" in js
    # コマを取ったら、動画の参照をその場で手放す（条件つきで、実際に呼ばれる形であること）
    assert "if (this.videoUrl) { URL.revokeObjectURL(this.videoUrl); this.videoUrl = null; }" in js
    grab = js[js.index("grabFrame()"):js.index("closeVideo()", js.index("grabFrame()")) + 20]
    assert "this.closeVideo()" in grab


def test_the_script_still_uploads_only_the_masked_frame():
    """動画を足しても、送るのは canvas を JPEG にしたものだけ（元の動画・写真は送らない）。"""
    js = mask_js()
    assert 'toBlob(resolve, "image/jpeg"' in js
    for forbidden in ("formData.append(this.input.files", "fd.append('video'", "input.files[0])"):
        assert forbidden not in js


def test_a_video_the_browser_cannot_open_is_explained():
    js = mask_js()
    assert "この動画は、このブラウザでは開けませんでした" in js and "撮影した端末" in js


# ---- ビデオ通話の画面を、フィルターして取り込む ----------------------------------

def test_the_form_offers_capturing_from_a_call_window(conn, alice):
    obj = objects.register_object(conn, alice, "空調")[0]
    body = get(conn, f'/o/{obj["obj_id"]}/new', login(conn, alice)).body.decode()
    assert 'class="mask-share"' in body and "通話の画面から取り込む" in body


def test_the_page_declares_which_permissions_it_uses(conn, alice):
    """画面共有は自分のページだけ。カメラ・マイク・位置情報は使わないので、明示的に閉じる。"""
    obj = objects.register_object(conn, alice, "空調")[0]
    pp = dict(get(conn, f'/o/{obj["obj_id"]}/new', login(conn, alice)).all_headers())["Permissions-Policy"]
    assert "display-capture=(self)" in pp
    for closed in ("camera=()", "microphone=()", "geolocation=()"):
        assert closed in pp


def test_the_share_takes_no_sound_and_no_whole_screen():
    """通話の音声は取らない。画面全体は、通知や関係のない画面まで写るので断る。"""
    js = mask_js()
    assert "audio: false" in js and 'displaySurface: "window"' in js
    assert 'selfBrowserSurface: "exclude"' in js and 'surfaceSwitching: "exclude"' in js
    assert 'surface === "monitor"' in js and "画面全体は取り込めません" in js
    monitor = js[js.index('surface === "monitor"'):js.index("画面全体は取り込めません")]
    assert "t.stop()" in monitor  # 断ったら、その場で共有を止める


def block(js: str, start: str) -> str:
    """その関数の本体（閉じ括弧まで）。"""
    i = js.index(start)
    end = js.index(chr(10) + '    }', i)
    return js[i:end]


def test_the_share_stops_as_soon_as_one_frame_is_taken():
    js = mask_js()
    assert "this.closeVideo()" in block(js, "grabFrame() {")
    assert "this.stopShare()" in block(js, "closeVideo() {")
    stop = block(js, "stopShare() {")
    assert "t.stop()" in stop and "srcObject = null" in stop


def test_a_captured_call_screen_starts_fully_hidden():
    """通話の画面は、はじめ全面を潰し、作り手がなぞった所だけを開ける（写真とは逆の向き）。"""
    js = mask_js()
    assert "this.reveal = !!this.stream;" in js
    rev = js[js.index("if (this.reveal) {"):js.index("} else {", js.index("if (this.reveal) {"))]
    assert "pixelate(this.ctx, { x: 0, y: 0, w: this.canvas.width, h: this.canvas.height })" in rev
    assert "putImageData" in rev
    assert "はじめは全部隠れています" in js


def test_a_photo_still_starts_visible():
    """写真は従来どおり（全部見える → 隠す所を指定）。取り込み元で向きが変わる。"""
    js = mask_js()
    assert js.count("this.reveal = false;") >= 3  # 写真・動画・後片付けで、必ず元の向きに戻す
