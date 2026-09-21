"""取り込み元（写真・動画の1コマ・通話の画面）を記録し、エージェントの判断の材料にする。

通話の画面は第三者が写るので、コードが**既定の共有範囲を最も狭く**し、AI には出どころを伝える。
"""

import pytest

from app import cards, db, objects
from app.tests.fakes import client_dynamic
from app.tests.test_web import HOST, login

C = {"reason": "r", "evidence": ""}
WRITE = ("write_card_text", {"title": "t", "changes": ["c"], "description": "d"})


def script(seen=None):
    def fn(kw, i):
        if seen is not None:
            seen.append(" ".join(m["content"] for m in kw["messages"]))
        names = {t["name"] for t in kw["tools"]}
        if "update_summary" in names:
            return [("hold_summary", C)]
        if "propose_extra_mask" in names:
            return [("no_action", C)]
        if "select_card_type" in names:
            return [("select_card_type", {**C, "type_id": "maintenance"})]
        return [WRITE]
    return client_dynamic(fn)


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def make(conn, alice, obj, jpeg, source, **kw):
    return cards.create_card(conn, alice, obj["obj_id"], before_desc="配管の点検",
                             images_in=[("before", jpeg, None, source)], mask_confirmed=True,
                             client_factory=kw.pop("client_factory", script()), **kw)["card"]


# ---- 出どころを残す ----------------------------------------------------------

def test_the_source_of_each_photo_is_recorded(conn, alice, obj, jpeg):
    card = make(conn, alice, obj, jpeg, "call_screen")
    row = db.one(conn, "SELECT source FROM image WHERE card_id=?", (card["card_id"],))
    assert row["source"] == "call_screen"


def test_an_unknown_source_is_stored_as_camera(conn, alice, obj, jpeg):
    card = make(conn, alice, obj, jpeg, "でたらめな値")
    assert db.one(conn, "SELECT source FROM image WHERE card_id=?", (card["card_id"],))["source"] == "camera"


def test_omitting_the_source_keeps_the_previous_behaviour(conn, alice, obj, jpeg):
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="点検",
                             images_in=[("before", jpeg, None)], mask_confirmed=True,
                             client_factory=script())["card"]
    assert db.one(conn, "SELECT source FROM image WHERE card_id=?", (card["card_id"],))["source"] == "camera"


# ---- 通話の画面は、コードが共有範囲を狭める ------------------------------------

def test_a_call_screen_forces_the_narrowest_scope(conn, alice, obj, jpeg):
    """通話の画面には第三者が写る。作り手が広い範囲を選んでも、コードが最も狭い範囲にする。"""
    card = make(conn, alice, obj, jpeg, "call_screen", scope="link_30d")
    assert card["scope"] == "invited_only"


def test_a_photo_keeps_the_chosen_scope(conn, alice, obj, jpeg):
    card = make(conn, alice, obj, jpeg, "camera", scope="link_30d")
    assert card["scope"] == "link_30d"


def test_a_video_frame_keeps_the_chosen_scope(conn, alice, obj, jpeg):
    """自分で撮った動画の1コマは、写真と同じ扱い。"""
    card = make(conn, alice, obj, jpeg, "video_frame", scope="link_30d")
    assert card["scope"] == "link_30d"


def test_narrowing_a_call_screen_is_audited(conn, alice, obj, jpeg):
    card = make(conn, alice, obj, jpeg, "call_screen", scope="link_30d")
    row = db.one(conn, "SELECT * FROM audit_log WHERE action='scope.forced' AND target=?", (card["card_id"],))
    assert row and "通話" in row["detail"]


# ---- AI に出どころを伝える ----------------------------------------------------

def test_the_agent_is_told_the_photo_came_from_a_call(conn, alice, obj, jpeg):
    seen = []
    make(conn, alice, obj, jpeg, "call_screen", client_factory=script(seen))
    assert seen and any("通話の画面" in s for s in seen)


def test_the_agent_is_not_told_that_for_an_ordinary_photo(conn, alice, obj, jpeg):
    seen = []
    make(conn, alice, obj, jpeg, "camera", client_factory=script(seen))
    assert seen and not any("通話の画面" in s for s in seen)


# ---- 画面 --------------------------------------------------------------------

def test_the_card_page_shows_where_the_photo_came_from(conn, alice, obj, jpeg):
    from app import web
    card = make(conn, alice, obj, jpeg, "call_screen")
    body = web.handle(conn, "GET", f'/c/{card["card_id"]}', {**HOST, "cookie": login(conn, alice)}).body.decode()
    assert "通話の画面" in body and "最も狭い" in body


def test_the_form_sends_the_source_and_offers_the_narrowest_scope(conn, alice, obj):
    from app import web
    body = web.handle(conn, "GET", f'/o/{obj["obj_id"]}/new', {**HOST, "cookie": login(conn, alice)}).body.decode()
    assert 'name="photo_source_before"' in body and 'class="mask-source"' in body
    js = (web.STATIC_DIR / "mask.js").read_text(encoding="utf-8")
    assert '.mask-source' in js and '"call_screen"' in js and '"video_frame"' in js
    # 通話の画面を取り込んだら、共有範囲の初期値を最も狭くする（作り手は選び直せる）
    assert 'sel.value = "invited_only"' in js
