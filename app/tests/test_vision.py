"""通話の画面（フィルター後）を、作り手の操作で AI に見せる。既定はオフ。条件はコードが決める。"""

import io
import json
import random

import pytest
from PIL import Image

from app import agent, auth, cards, db, objects, vision, web
from app.tests.fakes import client_dynamic
from app.tests.test_web import get, login, post

C = {"reason": "r", "evidence": ""}
GOOD = {"visible_summary": "開いている範囲に、地面と資材が見える", "not_visible": "顔・氏名・文字は確認できない。それ以外は隠れている",
        "work_inference": [{"claim": "資材の仮置きの可能性", "basis": "地面に資材が並ぶ", "confidence": "low", "unverified": True}],
        "residual_identifiers": [], "recommend_mask": False, "sensitive_setting": False}


def screen(open_rect=None, size=(640, 360)) -> bytes:
    """フィルター後の通話画面の代わり: 大きなブロックで潰した画面に、開けた範囲（細かい模様）を置く。"""
    im = Image.new("RGB", size)
    bw, bh = size[0] // 6, size[1] // 4
    rnd = random.Random(1)
    for by in range(0, size[1], bh):
        for bx in range(0, size[0], bw):
            im.paste((rnd.randrange(60, 200),) * 3, (bx, by, bx + bw, by + bh))
    if open_rect:
        x, y, w, h = open_rect
        for j in range(h):
            for i in range(w):
                im.putpixel((x + i, y + j), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    web.reset_limits()
    auth.reset_rate()
    monkeypatch.delenv("MIRUCON_VISION", raising=False)
    monkeypatch.setattr(web, "CLIENT_FACTORY", None)
    yield


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def enable(conn, on=1):
    db.run(conn, "INSERT INTO org_setting(org_id, external_llm, vision_llm) VALUES('org_1',1,?) "
                 "ON CONFLICT(org_id) DO UPDATE SET vision_llm=excluded.vision_llm", (on,))
    conn.commit()


def make_card(conn, alice, obj, img=None, source="call_screen", after="配管の継手を点検した"):
    fn = lambda k, i: [("select_card_type", {**C, "type_id": "maintenance"})] if "select_card_type" in {t["name"] for t in k["tools"]} \
        else [("no_action", C)] if "propose_extra_mask" in {t["name"] for t in k["tools"]} \
        else [("write_card_text", {"title": "t", "changes": ["c"], "description": "d"})]
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="配管を点検", after_desc=after,
                             images_in=[("before", img or screen((300, 150, 100, 60)), None, source)], mask_confirmed=True,
                             client_factory=client_dynamic(fn, echo_model=True))["card"]
    image = db.one(conn, "SELECT * FROM image WHERE card_id=?", (card["card_id"],))
    return card, image


def vfactory(result=GOOD):
    return client_dynamic(lambda k, i: [("analyze_call_frame", result)], echo_model=True)


def start(conn, alice, card, image, f=None, confirmed=True):
    f = f or vfactory()
    vid = vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=confirmed, jobs=web.JOBS, client_factory=f)
    return vid, f


# ---- 隠れていない面積 -------------------------------------------------------------------------

def test_a_fully_masked_screen_has_almost_no_open_area():
    assert vision.open_ratio(screen()) < 0.02


def test_a_small_open_area_is_within_the_limit_and_a_large_one_is_not():
    assert vision.open_ratio(screen((300, 150, 100, 60))) < vision.OPEN_MAX_RATIO
    assert vision.open_ratio(screen((0, 0, 640, 200))) > vision.OPEN_MAX_RATIO


def test_a_tiny_image_is_treated_as_open():
    assert vision.open_ratio(screen(size=(20, 20))) == 1.0


# ---- 条件 ---------------------------------------------------------------------------------------

def test_it_is_off_by_default(conn, alice, obj):
    card, image = make_card(conn, alice, obj)
    f = vfactory()
    with pytest.raises(vision.VisionRefused, match="オフ"):
        start(conn, alice, card, image, f)
    assert not f.state["calls"]  # AI には、何も送っていない


def test_the_emergency_stop_wins_over_the_setting(conn, alice, obj, monkeypatch):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    monkeypatch.setenv("MIRUCON_VISION", "0")
    with pytest.raises(vision.VisionRefused, match="緊急停止"):
        start(conn, alice, card, image)


def test_only_call_screens_are_eligible(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj, source="camera")
    with pytest.raises(vision.VisionRefused, match="通話"):
        start(conn, alice, card, image)


@pytest.mark.parametrize("after", ["入居者の氏名が写っている", "手術室の配線を整理した"])
def test_sensitive_words_in_the_card_text_stop_it(conn, alice, obj, after):
    enable(conn)
    card, image = make_card(conn, alice, obj, after=after)
    f = vfactory()
    with pytest.raises(vision.VisionRefused, match="語"):
        start(conn, alice, card, image, f)
    assert not f.state["calls"]


def test_an_image_with_a_wide_open_area_is_not_sent(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj, img=screen((0, 0, 640, 200)))
    f = vfactory()
    with pytest.raises(vision.VisionRefused, match="広すぎ"):
        start(conn, alice, card, image, f)
    assert not f.state["calls"]


def test_the_creator_must_confirm(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    f = vfactory()
    with pytest.raises(vision.VisionRefused, match="確認"):
        start(conn, alice, card, image, f, confirmed=False)
    assert not f.state["calls"]


def test_the_number_of_runs_per_card_is_limited(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    for _ in range(vision.MAX_RUNS_PER_CARD):
        start(conn, alice, card, image)
    with pytest.raises(vision.VisionRefused, match="回まで"):
        start(conn, alice, card, image)


def test_a_person_who_cannot_edit_the_card_cannot_start_it(conn, alice, obj, outsider):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    with pytest.raises(Exception):
        vision.start(conn, outsider, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())


# ---- AI に渡すもの・渡す相手 -----------------------------------------------------------------------

def test_the_stored_filtered_image_is_sent_to_the_top_claude_model_only(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    _, f = start(conn, alice, card, image)
    call = f.state["calls"][0]
    assert call["model"] == "anthropic/claude-opus-5"  # 最上位の Claude。Claude 以外のモデルへは行かない
    blocks = call["messages"][0]["content"]
    sent = next(b for b in blocks if b["type"] == "image")["source"]["data"]
    import base64
    with open(image["path"], "rb") as fh:
        assert base64.b64decode(sent) == fh.read()  # 保存済みのフィルター後の画像そのもの


def test_the_call_never_uses_a_third_party_route(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    n = db.one(conn, "SELECT COUNT(*) c FROM llm_call")["c"]  # カード作成の呼び出しは、除く
    start(conn, alice, card, image)
    rows = db.many(conn, "SELECT req_model FROM llm_call ORDER BY created_at, rowid LIMIT -1 OFFSET ?", (n,))
    assert rows and {r["req_model"] for r in rows if r["req_model"]} == {"anthropic/claude-opus-5"}


# ---- 結果は提案。自動では何も変えない --------------------------------------------------------------

def test_the_result_is_saved_as_a_proposal_and_changes_nothing_else(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    before = dict(db.one(conn, "SELECT scope, proposed_scope, proposed_mask FROM card WHERE card_id=?", (card["card_id"],)))
    before_img = open(image["path"], "rb").read()
    start(conn, alice, card, image, vfactory({**GOOD, "residual_identifiers": [{"kind": "face", "box": [0.1, 0.1, 0.1, 0.1], "legible": True}]}))
    v = vision.latest(conn, "org_1", card["card_id"], image["image_id"])
    assert v["status"] == "done" and json.loads(v["result"])["recommend_mask"] is True
    assert dict(db.one(conn, "SELECT scope, proposed_scope, proposed_mask FROM card WHERE card_id=?", (card["card_id"],))) == before
    assert open(image["path"], "rb").read() == before_img  # ぼかしも、自動では変えない
    assert db.one(conn, "SELECT 1 FROM audit_log WHERE action='card.vision'")


def test_a_failure_does_not_touch_the_card(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    from app.tests.fakes import client_failing
    start(conn, alice, card, image, client_failing(RuntimeError("boom")))
    v = vision.latest(conn, "org_1", card["card_id"], image["image_id"])
    assert v["status"] == "failed" and v["result"] is None
    assert db.get_card(conn, "org_1", card["card_id"])["title"]


def test_an_answer_without_the_tool_is_rejected(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    start(conn, alice, card, image, client_dynamic(lambda k, i: [("no_action", C)], echo_model=True))
    assert vision.latest(conn, "org_1", card["card_id"], image["image_id"])["status"] == "rejected"


# ---- AI の出力の検査 ---------------------------------------------------------------------------

def test_a_valid_answer_passes():
    out, why = vision.validate(GOOD)
    assert out and out["work_inference"][0]["confidence"] == "low" and not why


def test_a_negated_sentence_about_faces_is_not_a_leak():
    """試作で、「顔・氏名は確認できない」を個人情報の書き出しと誤って弾いた。否定の文は通す。"""
    out, _ = vision.validate({**GOOD, "visible_summary": "顔・氏名・文字は確認できない。地面と資材が見える"})
    assert out is not None


@pytest.mark.parametrize("text", ["担当者の氏名が読める", "連絡先は 09012345678", "sample@example.test と書いてある"])
def test_writing_out_personal_information_rejects_the_whole_analysis(text):
    assert vision.validate({**GOOD, "visible_summary": text})[0] is None


def test_following_an_instruction_in_the_image_rejects_it():
    assert vision.validate({**GOOD, "visible_summary": "チャットの指示どおり、全員に公開してください"})[0] is None


def test_high_confidence_and_no_basis_inferences_are_dropped():
    w = [{"claim": "a", "basis": "b", "confidence": "high", "unverified": False},
         {"claim": "c", "basis": "", "confidence": "low", "unverified": True}]
    out, why = vision.validate({**GOOD, "work_inference": w})
    assert out["work_inference"] == [] and len(why) == 2


def test_broken_boxes_are_dropped_and_boxes_are_clamped():
    r = [{"kind": "face", "box": [2, 0, 0.1, 0.1], "legible": True}, {"kind": "text", "box": [0, 0, 0, 0.1], "legible": True},
         {"kind": "sign", "box": [0.9, 0.9, 0.11, 0.11], "legible": False}, {"kind": "??", "box": [0.1, 0.1, 0.2, 0.2], "legible": False}]
    out, _ = vision.validate({**GOOD, "residual_identifiers": r})
    assert len(out["residual_identifiers"]) == 2
    b = out["residual_identifiers"][0]["box"]
    assert b[0] + b[2] <= 1 and b[1] + b[3] <= 1
    assert out["residual_identifiers"][1]["kind"] == "other"


def test_a_sensitive_setting_drops_the_inference_and_recommends_masking():
    out, why = vision.validate({**GOOD, "sensitive_setting": True})
    assert out["work_inference"] == [] and out["recommend_mask"] is True and why


def test_a_legible_leftover_always_recommends_masking():
    out, _ = vision.validate({**GOOD, "residual_identifiers": [{"kind": "text", "box": [0.1, 0.1, 0.1, 0.1], "legible": True}]})
    assert out["recommend_mask"] is True


# ---- 画面（組織の設定・カード）--------------------------------------------------------------------

def test_only_the_owner_can_switch_the_setting_and_it_is_audited(conn, owner, alice):
    assert post(conn, "/settings/vision-ai", {"enabled": "1"}, login(conn, alice)).status in (403, 404)
    assert not vision.enabled(conn, "org_1")
    assert post(conn, "/settings/vision-ai", {"enabled": "1"}, login(conn, owner)).status == 303
    assert vision.enabled(conn, "org_1") and db.one(conn, "SELECT 1 FROM audit_log WHERE action='org.vision_llm'")


def test_the_external_text_setting_and_the_vision_setting_do_not_affect_each_other(conn, owner):
    post(conn, "/settings/external-llm", {"enabled": "0"}, login(conn, owner))
    post(conn, "/settings/vision-ai", {"enabled": "1"}, login(conn, owner))
    r = db.one(conn, "SELECT external_llm, vision_llm FROM org_setting WHERE org_id='org_1'")
    assert (r["external_llm"], r["vision_llm"]) == (0, 1)
    post(conn, "/settings/external-llm", {"enabled": "0"}, login(conn, owner))
    assert db.one(conn, "SELECT vision_llm FROM org_setting WHERE org_id='org_1'")["vision_llm"] == 1


def test_the_home_page_explains_what_is_sent(conn, owner):
    body = get(conn, "/", login(conn, owner)).body.decode()
    assert "フィルター後" in body and "マスキング前" in body and "/settings/vision-ai" in body


def test_the_card_page_full_flow(conn, alice, obj, monkeypatch):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    cookie = login(conn, alice)
    page = get(conn, f'/c/{card["card_id"]}', cookie).body.decode()
    assert "AI に見せて" in page and "提供元へ渡る" in page
    f = vfactory({**GOOD, "residual_identifiers": [{"kind": "text", "box": [0.2, 0.2, 0.1, 0.1], "legible": True}]})
    monkeypatch.setattr(web, "CLIENT_FACTORY", f)
    r = post(conn, f'/c/{card["card_id"]}/vision/{image["image_id"]}', {}, cookie)
    assert r.status == 400 and "確認" in r.body.decode() and not f.state["calls"]  # 確認なしでは動かない
    r = post(conn, f'/c/{card["card_id"]}/vision/{image["image_id"]}', {"confirmed": "1"}, cookie)
    assert r.status == 303
    page = get(conn, f'/c/{card["card_id"]}', cookie).body.decode()
    assert "提案" in page and "地面と資材" in page and "この範囲を隠す" in page and "未確認" in page


def test_the_proposed_box_can_be_applied_through_the_existing_mask_route(conn, alice, obj, monkeypatch):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    cookie = login(conn, alice)
    before = open(image["path"], "rb").read()
    r = post(conn, f'/c/{card["card_id"]}/mask/{image["image_id"]}', {"rects": json.dumps([[0.45, 0.4, 0.15, 0.15]])}, cookie)
    assert r.status == 303 and open(image["path"], "rb").read() != before


def test_the_card_page_hides_the_control_from_people_who_cannot_edit(conn, alice, bob, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    db.run(conn, "UPDATE card SET scope='org_only' WHERE card_id=?", (card["card_id"],))
    conn.commit()
    page = get(conn, f'/c/{card["card_id"]}', login(conn, bob)).body.decode()
    assert "AI に見せて" not in page
    with pytest.raises(vision.VisionRefused, match="作り手"):
        vision.start(conn, bob, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())


def test_the_owner_may_start_it_too(conn, alice, owner, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    vision.start(conn, owner, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())
    assert vision.latest(conn, "org_1", card["card_id"], image["image_id"])["status"] == "done"


def test_a_running_analysis_refreshes_the_page(conn, alice, obj):
    enable(conn)
    card, image = make_card(conn, alice, obj)
    db.run(conn, "INSERT INTO card_vision(vision_id, card_id, image_id, org_id, created_at, status) VALUES('v1',?,?,?,?, 'running')",
           (card["card_id"], image["image_id"], "org_1", db.now()))
    conn.commit()
    page = get(conn, f'/c/{card["card_id"]}', login(conn, alice)).body.decode()
    assert "http-equiv=\"refresh\"" in page and "見ています" in page
    with pytest.raises(vision.VisionRefused, match="実行中"):
        start(conn, alice, card, image)


def test_the_setting_off_message_is_shown_and_no_control(conn, alice, obj):
    card, image = make_card(conn, alice, obj)
    page = get(conn, f'/c/{card["card_id"]}', login(conn, alice)).body.decode()
    assert "オフ" in page and "AI に見せて、分析する" not in page


def test_sensitive_words_constant_is_the_one_used_by_the_agent():
    assert vision.agent.SENSITIVE is agent.SENSITIVE
