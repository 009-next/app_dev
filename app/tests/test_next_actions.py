"""将来シナリオの比較（D7）: 確信度で足切りし、入力にない数値を書いた案は拒否する。"""

import json

import pytest

from app import agent, cards, db, objects, signals
from app.tests.fakes import client_dynamic, client_returning
from app.tests.test_web import get, login, post
from app import auth, web

C = {"reason": "r", "evidence": "フィルターが汚れている"}
WRITE = ("write_card_text", {"title": "エアコン清掃", "changes": ["清掃した"], "description": "清掃した"})


def opt(title="点検する", condition="担当者が安全に点検できるとき", expect="今の状態を記録できる（推測）",
        risk="停止時間は分からない", duration="未計測", cost="未計測"):
    return {"title": title, "condition": condition, "expect": expect, "risk": risk, "duration": duration, "cost": cost}


def call(options, ids, evidence="フィルターが汚れている"):
    return [("propose_next_actions", {"reason": "r", "evidence": evidence, "options": options, "evidence_card_ids": ids})]


def script(summary=("no_action", C)):
    def fn(kw, i):
        names = {t["name"] for t in kw["tools"]}
        if "update_summary" in names:
            return [summary]
        if "propose_extra_mask" in names:
            return [("no_action", C)]
        if "select_card_type" in names:
            return [("select_card_type", {**C, "type_id": "cleaning"})]
        return [WRITE]
    return client_dynamic(fn)


@pytest.fixture(autouse=True)
def _limits():
    web.reset_limits()
    auth.reset_rate()
    yield


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def two_cards(conn, alice, obj):
    a = cards.create_card(conn, alice, obj["obj_id"], before_desc="フィルターが汚れている", after_desc="清掃した",
                          client_factory=script())["card"]
    b = cards.create_card(conn, alice, obj["obj_id"], before_desc="風量が弱い", after_desc="点検した",
                          client_factory=script())["card"]
    return [a["card_id"], b["card_id"]]


# ---- 確信度による足切り（LLM を呼ばない） -------------------------------------------

def test_too_few_records_returns_questions_without_calling_the_model(conn, alice, obj):
    f = client_dynamic(lambda kw, i: call([opt(), opt("交換する")], []))
    r = objects.next_actions(conn, alice, obj["obj_id"], client_factory=f)
    assert r["confidence"]["label"] == "insufficient" and r["options"] == []
    assert r["questions"] and f.state["i"] == 0  # 呼んでいない＝費用なし


def test_old_records_also_stop_before_the_model(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    db.run(conn, "UPDATE card SET created_at=? WHERE card_id IN (?,?)", (db.now() - 200 * 86400.0, *ids))
    conn.commit()
    f = client_dynamic(lambda kw, i: call([opt(), opt("交換する")], ids))
    r = objects.next_actions(conn, alice, obj["obj_id"], client_factory=f)
    assert r["confidence"]["label"] == "insufficient" and f.state["i"] == 0


def test_enough_recent_records_produce_options(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    f = client_dynamic(lambda kw, i: call([opt(), opt("交換する")], ids))
    r = objects.next_actions(conn, alice, obj["obj_id"], client_factory=f)
    assert len(r["options"]) == 2 and r["confidence"]["label"] == "usable"
    stored = json.loads(db.get_object(conn, "org_1", obj["obj_id"])["next_options"])
    assert stored["options"][0]["title"] == "点検する" and stored["evidence_card_ids"] == ids


# ---- 案の検査（フォース・ビジョンの実装ルール） ----------------------------------------

def refuse(conn, alice, obj, response):
    f = client_returning(response)
    return objects.next_actions(conn, alice, obj["obj_id"], client_factory=f)


def test_numbers_that_are_not_in_the_material_are_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    r = refuse(conn, alice, obj, call([opt(expect="3年は持つ（推測）"), opt("交換する")], ids))
    assert r["options"] == [] and "作り" in r["refused"]


def test_a_model_number_that_is_not_in_the_material_is_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    r = refuse(conn, alice, obj, call([opt(title="RA-25 を交換する"), opt()], ids))
    assert r["options"] == []


def test_a_made_up_duration_or_cost_is_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    assert refuse(conn, alice, obj, call([opt(duration="2時間"), opt()], ids))["options"] == []
    assert refuse(conn, alice, obj, call([opt(cost="5000円"), opt()], ids))["options"] == []


def test_a_vague_duration_without_a_number_must_say_unmeasured(conn, alice, obj):
    """「長め」のようなごまかしを認めない。入力に数値がなければ「未計測」と書く。"""
    ids = two_cards(conn, alice, obj)
    assert refuse(conn, alice, obj, call([opt(duration="長め"), opt()], ids))["options"] == []
    assert refuse(conn, alice, obj, call([opt(cost="それなり"), opt()], ids))["options"] == []


def test_unmeasured_duration_and_cost_are_accepted(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    r = refuse(conn, alice, obj, call([opt(), opt("交換する")], ids))
    assert len(r["options"]) == 2


def test_fewer_than_two_options_is_refused(conn, alice, obj):
    """1つだけ出すのは「断定」に近い。必ず比べさせる。"""
    ids = two_cards(conn, alice, obj)
    assert refuse(conn, alice, obj, call([opt()], ids))["options"] == []


def test_more_than_four_options_is_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    assert refuse(conn, alice, obj, call([opt(f"案{i}") for i in range(5)], ids))["options"] == []


def test_a_missing_field_is_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    bad = {k: v for k, v in opt().items() if k != "risk"}
    assert refuse(conn, alice, obj, call([bad, opt()], ids))["options"] == []


def test_an_empty_field_is_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    assert refuse(conn, alice, obj, call([opt(risk="  "), opt()], ids))["options"] == []


def test_evidence_ids_that_do_not_exist_are_refused(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    assert refuse(conn, alice, obj, call([opt(), opt("交換する")], ["card_zzz"]))["options"] == []
    assert refuse(conn, alice, obj, call([opt(), opt("交換する")], []))["options"] == []


def test_a_refused_proposal_is_recorded_as_the_default(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    refuse(conn, alice, obj, call([opt(expect="3年は持つ"), opt()], ids))
    d = db.one(conn, "SELECT * FROM decision_log WHERE stage='options' ORDER BY created_at DESC")
    assert d["chosen_tool"] == "no_action" and "作り" in d["validation"]


# ---- 画面 ------------------------------------------------------------------------

def test_the_room_offers_the_button_and_shows_the_result(conn, alice, obj):
    two_cards(conn, alice, obj)
    c = login(conn, alice)
    assert "次の行動" in get(conn, f'/o/{obj["obj_id"]}', c).body.decode()


def test_the_screen_marks_the_options_as_conditional_not_an_instruction(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    web.CLIENT_FACTORY = client_returning(call([opt(), opt("交換する")], ids))
    try:
        c = login(conn, alice)
        post(conn, f'/o/{obj["obj_id"]}/options', {}, c)
        b = get(conn, f'/o/{obj["obj_id"]}', c).body.decode()
    finally:
        web.CLIENT_FACTORY = None
    assert "断定" in b and "指示ではありません" in b and "点検する" in b


def test_a_new_card_marks_the_options_as_out_of_date(conn, alice, obj):
    ids = two_cards(conn, alice, obj)
    f = client_returning(call([opt(), opt("交換する")], ids))
    objects.next_actions(conn, alice, obj["obj_id"], client_factory=f)
    cards.create_card(conn, alice, obj["obj_id"], before_desc="新しい記録", after_desc="対応した", client_factory=script())
    b = get(conn, f'/o/{obj["obj_id"]}', login(conn, alice)).body.decode()
    assert "新しい記録があります" in b


def test_a_member_of_another_org_cannot_ask_for_options(conn, alice, outsider, obj):
    two_cards(conn, alice, obj)
    r = post(conn, f'/o/{obj["obj_id"]}/options', {}, login(conn, outsider))
    assert r.status == 404


def test_the_screen_explains_why_no_options_were_made(conn, alice, obj):
    """押しても案が出ないことがある。その理由を画面に出す。"""
    ids = two_cards(conn, alice, obj)
    web.CLIENT_FACTORY = client_returning(call([opt(cost="1〜2週間後に再点検"), opt()], ids))
    try:
        c = login(conn, alice)
        post(conn, f'/o/{obj["obj_id"]}/options', {}, c)
        b = get(conn, f'/o/{obj["obj_id"]}', c).body.decode()
    finally:
        web.CLIENT_FACTORY = None
    assert "案を作りませんでした" in b and "もう一度押す" in b
