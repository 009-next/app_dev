"""通知の下書き: 同じ内容を二重に登録しない／承認は、画面に出した本文そのものに紐づける。"""

import dataclasses

import pytest

from app import agent, db, notifications, objects

C = {"reason": "r", "evidence": ""}


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def dec(message="点検の期限を過ぎています。", recipient="オーナー", dec_id="dec_1"):
    return agent.Decision(stage="periodic", tool="draft_notification",
                          args={**C, "recipient": recipient, "message": message}, default_used=False, dec_id=dec_id)


def drafts(conn):
    return db.many(conn, "SELECT * FROM notification ORDER BY created_at")


# ---- 二重登録の防止（フォース・プッシュ） -----------------------------------------

def test_the_same_draft_is_not_saved_twice(conn, obj):
    a = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    b = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    assert a == b and len(drafts(conn)) == 1


def test_a_different_message_is_a_different_draft(conn, obj):
    notifications.save_draft(conn, "org_1", obj["obj_id"], dec("点検の期限を過ぎています。"))
    notifications.save_draft(conn, "org_1", obj["obj_id"], dec("記録が矛盾しています。"))
    assert len(drafts(conn)) == 2


def test_every_draft_records_the_operation_it_came_from(conn, obj):
    notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    assert drafts(conn)[0]["operation_id"]


def test_a_decided_draft_does_not_block_a_new_one(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    notifications.decide_draft(conn, alice, nid, False)
    again = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    assert again and again != nid and len(drafts(conn)) == 2


def test_extra_drafts_from_one_response_are_also_deduplicated(conn, obj):
    d = dataclasses.replace(dec(), tool="hold_summary", args=C,
                            extras=[{"tool": "draft_notification", "executed": True,
                                     "args": {**C, "recipient": "オーナー", "message": "矛盾があります。"}}] * 2)
    notifications.save_extra_drafts(conn, "org_1", obj["obj_id"], d)
    assert len(drafts(conn)) == 1


# ---- 承認を、本文に紐づける -------------------------------------------------------

def test_the_digest_covers_the_message_and_the_recipient(conn, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    n = db.one(conn, "SELECT * FROM notification WHERE notif_id=?", (nid,))
    first = notifications.draft_digest(conn, n)
    assert first == notifications.draft_digest(conn, n)
    changed = dict(n)
    changed["message"] = "別の本文"
    assert notifications.draft_digest(conn, changed) != first


def test_approval_with_the_matching_digest_works(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    n = db.one(conn, "SELECT * FROM notification WHERE notif_id=?", (nid,))
    notifications.decide_draft(conn, alice, nid, True, digest=notifications.draft_digest(conn, n))
    assert db.one(conn, "SELECT status FROM notification WHERE notif_id=?", (nid,))["status"] == "approved"


def test_approval_with_a_stale_digest_is_refused(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    with pytest.raises(notifications.StaleDraft):
        notifications.decide_draft(conn, alice, nid, True, digest="0" * 64)
    assert db.one(conn, "SELECT status FROM notification WHERE notif_id=?", (nid,))["status"] == "draft"


def test_omitting_the_digest_keeps_the_previous_behaviour(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    notifications.decide_draft(conn, alice, nid, True)
    assert db.one(conn, "SELECT status FROM notification WHERE notif_id=?", (nid,))["status"] == "approved"


def test_the_audit_log_keeps_what_was_approved(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    n = db.one(conn, "SELECT * FROM notification WHERE notif_id=?", (nid,))
    d = notifications.draft_digest(conn, n)
    notifications.decide_draft(conn, alice, nid, True, digest=d)
    row = db.one(conn, "SELECT detail FROM audit_log WHERE action='notification.approve'")
    assert d[:16] in row["detail"]


# ---- 画面 ------------------------------------------------------------------------

from app.tests.test_web import get, login, post  # noqa: E402


def test_the_approval_screen_says_what_approving_does(conn, alice, obj):
    notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    body = get(conn, "/n", login(conn, alice)).body.decode()
    assert "送信" in body and "digest" in body


def test_approving_a_stale_screen_is_refused_with_a_clear_message(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    c = login(conn, alice)
    r = post(conn, f"/n/{nid}/approve", {"digest": "0" * 64}, c)
    assert r.status == 409 and "読み直し" in r.body.decode()
    assert db.one(conn, "SELECT status FROM notification WHERE notif_id=?", (nid,))["status"] == "draft"


def test_approving_from_a_fresh_screen_succeeds(conn, alice, obj):
    nid = notifications.save_draft(conn, "org_1", obj["obj_id"], dec())
    c = login(conn, alice)
    n = db.one(conn, "SELECT * FROM notification WHERE notif_id=?", (nid,))
    r = post(conn, f"/n/{nid}/approve", {"digest": notifications.draft_digest(conn, n)}, c)
    assert r.status == 303
    assert db.one(conn, "SELECT status FROM notification WHERE notif_id=?", (nid,))["status"] == "approved"
