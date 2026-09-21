import pytest

from app import agent, auth, cards, db, notifications, objects, web
from app.tests.fakes import client_returning
from app.tests.test_web import get, login, post, text

COMMON = {"reason": "r", "evidence": "点検"}
NEW_TYPES = ["inspection", "near_miss", "correction", "meeting"]


@pytest.fixture(autouse=True)
def _reset():
    web.reset_limits()
    auth.reset_rate()
    yield


@pytest.fixture
def obj(conn, alice, bob):
    return objects.register_object(conn, alice, "空調", assignee_id=bob.member_id)[0]


def _draft(conn, org, obj_id, recipient, message, with_check=True):
    f = client_returning([("draft_notification", {**COMMON, "recipient": recipient, "message": message})])
    return agent.decide(conn, org, stage="periodic", inputs={"状況": "点検の期限が過ぎている"}, obj_id=obj_id, card_id=None,
                        client_factory=f,
                        recipient_ok=(notifications.recipient_ok_for(conn, org, obj_id) if with_check else None))


# ---- カードの種類（your_folderの書式） ----------------------------------------

def test_new_formats_are_card_types_from_the_single_source():
    assert set(NEW_TYPES) <= set(agent.CARD_TYPES) and "generic" in agent.CARD_TYPES
    enum = agent.SPEC["tools"]["select_card_type"]["input_schema"]["properties"]["type_id"]["enum"]
    assert enum == agent.CARD_TYPES


@pytest.mark.parametrize("t", NEW_TYPES)
def test_agent_can_select_new_formats_and_card_saves_them(conn, alice, obj, t):
    f = client_returning([("select_card_type", {**COMMON, "type_id": t})])
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="点検した", client_factory=f)["card"]
    assert card["card_type"] == t


def test_unknown_format_falls_back_to_generic(conn, alice, obj):
    f = client_returning([("select_card_type", {**COMMON, "type_id": "safety_analysis"})])
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="点検した", client_factory=f)["card"]
    assert card["card_type"] == "generic"


# ---- 下書きの保存 ------------------------------------------------------------

def test_draft_to_owner_and_assignee_are_saved(conn, alice, bob, obj):
    d1 = _draft(conn, alice.org_id, obj["obj_id"], "オーナー", "点検の期限が過ぎています。")
    d2 = _draft(conn, alice.org_id, obj["obj_id"], "担当者", "点検の期限が過ぎています。")
    n1, n2 = (notifications.save_draft(conn, alice.org_id, obj["obj_id"], d) for d in (d1, d2))
    assert n1 and n2
    rows = {r["notif_id"]: r for r in db.many(conn, "SELECT * FROM notification")}
    assert rows[n1]["recipient_member_id"] is None and rows[n2]["recipient_member_id"] == bob.member_id
    assert {r["status"] for r in rows.values()} == {"draft"} and rows[n2]["dec_id"] == d2.dec_id


def test_no_draft_without_assignee_or_with_suspended_assignee(conn, alice, bob):
    o = objects.register_object(conn, alice, "no-assignee")[0]
    d = _draft(conn, alice.org_id, o["obj_id"], "担当者", "点検の期限が過ぎています。")
    assert d.boundary_violation and d.args["recipient"] == "オーナー" and d.args["message"] == agent.GENERIC_NOTICE
    n = notifications.save_draft(conn, alice.org_id, o["obj_id"], d)  # 担当者がいない案は、一般的な文面のオーナー宛てに落ちる
    assert db.one(conn, "SELECT recipient, message FROM notification WHERE notif_id=?", (n,))["recipient"] == "オーナー"
    o2 = objects.register_object(conn, alice, "with", assignee_id=bob.member_id)[0]
    db.run(conn, "UPDATE member SET status='suspended' WHERE member_id=?", (bob.member_id,))
    d2 = _draft(conn, alice.org_id, o2["obj_id"], "担当者", "点検の期限が過ぎています。")
    assert notifications.save_draft(conn, alice.org_id, o2["obj_id"], d2) is None


def test_only_applied_draft_notification_decisions_are_saved(conn, alice, obj):
    f = client_returning([("no_action", COMMON)])
    d = agent.decide(conn, alice.org_id, stage="periodic", inputs={"状況": "x"}, obj_id=obj["obj_id"], client_factory=f)
    assert notifications.save_draft(conn, alice.org_id, obj["obj_id"], d) is None
    d2 = _draft(conn, alice.org_id, obj["obj_id"], "オーナー", "本文です。本文です。")
    d2.applied = False  # 判断ログを保存できなかった判断は適用しない
    assert notifications.save_draft(conn, alice.org_id, obj["obj_id"], d2) is None
    assert db.one(conn, "SELECT COUNT(*) c FROM notification")["c"] == 0


def test_draft_without_recipient_check_is_replaced_by_generic_notice(conn, alice, obj):
    d = _draft(conn, alice.org_id, obj["obj_id"], "担当者", "秘密の内容", with_check=False)  # 検査の関数を渡し忘れた
    assert d.boundary_violation and d.args["message"] == agent.GENERIC_NOTICE and d.args["recipient"] == "オーナー"


def test_message_leaking_invited_only_card_to_assignee_is_replaced(conn, alice, obj):
    cards.create_card(conn, alice, obj["obj_id"], before_desc="配管の漏れを確認した", scope="invited_only")
    leak = _draft(conn, alice.org_id, obj["obj_id"], "担当者", "配管の漏れを確認したので対応してください")
    assert leak.boundary_violation and leak.args["message"] == agent.GENERIC_NOTICE
    fine = _draft(conn, alice.org_id, obj["obj_id"], "担当者", "期限が過ぎています。点検をお願いします。")
    assert not fine.boundary_violation
    to_owner = _draft(conn, alice.org_id, obj["obj_id"], "オーナー", "配管の漏れを確認したので対応してください")
    assert not to_owner.boundary_violation  # オーナーは組織のすべてのカードを見られる


# ---- 承認・却下 --------------------------------------------------------------

def _saved(conn, alice, obj):
    d = _draft(conn, alice.org_id, obj["obj_id"], "オーナー", "点検の期限が過ぎています。")
    return notifications.save_draft(conn, alice.org_id, obj["obj_id"], d)


def test_list_visibility_follows_send_notification_permission(conn, alice, bob, owner, outsider, obj):
    n = _saved(conn, alice, obj)
    ids = lambda a: [r["notif_id"] for r in notifications.list_drafts(conn, a)]  # noqa: E731
    assert ids(owner) == [n] and ids(alice) == [n]        # オーナーと、その物の登録者
    assert ids(bob) == [] and ids(outsider) == []          # 担当者でも登録者でないメンバー・他組織は見えない


def test_approve_and_reject_rules(conn, alice, bob, owner, outsider, obj):
    n = _saved(conn, alice, obj)
    with pytest.raises(objects.Denied):
        notifications.decide_draft(conn, bob, n, True)
    with pytest.raises(objects.NotFound):
        notifications.decide_draft(conn, outsider, n, True)
    notifications.decide_draft(conn, alice, n, True)
    assert db.one(conn, "SELECT status FROM notification")["status"] == "approved"
    with pytest.raises(objects.NotFound):  # 決定済みは再決定できない
        notifications.decide_draft(conn, owner, n, False)
    assert db.one(conn, "SELECT COUNT(*) c FROM audit_log WHERE action='notification.approve'")["c"] == 1


def test_approval_never_sends_anything(conn, alice, obj):
    n = _saved(conn, alice, obj)
    notifications.decide_draft(conn, alice, n, True)
    assert {r["status"] for r in db.many(conn, "SELECT status FROM notification")} <= {"draft", "approved", "rejected"}


# ---- Web ---------------------------------------------------------------------

def test_web_requires_login_and_escapes_message(conn, alice, obj):
    assert get(conn, "/n").status == 303
    d = _draft(conn, alice.org_id, obj["obj_id"], "オーナー", "点検です。点検です。")
    d.args["message"] = "<script>alert(1)</script>点検です。"
    notifications.save_draft(conn, alice.org_id, obj["obj_id"], d)
    page = text(get(conn, "/n", cookie=login(conn, alice)))
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "送信されません" in page


def test_web_approve_needs_recent_login_reject_does_not(conn, alice, obj):
    n = _saved(conn, alice, obj)
    ck = login(conn, alice)
    db.run(conn, "UPDATE session SET last_auth_at=last_auth_at-?", (auth.STEPUP_WINDOW + 5,))
    r = post(conn, f"/n/{n}/approve", {}, cookie=ck)
    assert r.status == 403 and "再認証" in text(r)
    assert db.one(conn, "SELECT status FROM notification")["status"] == "draft"
    assert post(conn, f"/n/{n}/reject", {}, cookie=ck).status == 303
    assert db.one(conn, "SELECT status FROM notification")["status"] == "rejected"


def test_web_approve_by_other_member_and_other_org(conn, alice, bob, outsider, obj):
    n = _saved(conn, alice, obj)
    assert post(conn, f"/n/{n}/approve", {}, cookie=login(conn, bob)).status == 403
    assert post(conn, f"/n/{n}/approve", {}, cookie=login(conn, outsider)).status == 404
    assert post(conn, f"/n/{n}/approve", {}, cookie=login(conn, alice)).status == 303
    assert get(conn, "/n", cookie=login(conn, alice)).status == 200
