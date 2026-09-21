"""物の作業室: 時系列・確認事項・判断の記録・承認待ち・タグの利用履歴を、1画面に出す。

権限を超えるものは出さない（見られないカードの判断・内容）。
"""

import pytest

from app import agent, auth, cards, db, notifications, objects, web
from app.tests.fakes import client_dynamic
from app.tests.test_web import get, login, post

C = {"reason": "r", "evidence": ""}
WRITE = ("write_card_text", {"title": "エアコン清掃", "changes": ["清掃した"], "description": "清掃して風量が回復した"})


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


def make_card(conn, actor, obj, *, before="フィルターが汚れている", after="清掃した", scope="org_only", days_ago=0):
    r = cards.create_card(conn, actor, obj["obj_id"], before_desc=before, after_desc=after, scope=scope, client_factory=script())
    if days_ago:
        db.run(conn, "UPDATE card SET created_at=? WHERE card_id=?", (db.now() - days_ago * 86400.0, r["card"]["card_id"]))
        db.run(conn, "UPDATE decision_log SET created_at=? WHERE card_id=?", (db.now() - days_ago * 86400.0, r["card"]["card_id"]))
        conn.commit()
    return r["card"]


# ---- 時系列（サイコメトリー） ---------------------------------------------------

def test_the_timeline_lists_cards_and_decisions_in_order(conn, alice, obj):
    make_card(conn, alice, obj, days_ago=10)
    make_card(conn, alice, obj)
    rows = objects.timeline(conn, alice, obj["obj_id"])
    assert [r["kind"] for r in rows].count("card") == 2
    assert any(r["kind"] == "decision" for r in rows)
    assert rows == sorted(rows, key=lambda r: r["at"])


def test_the_timeline_marks_periods_with_no_record(conn, alice, obj):
    make_card(conn, alice, obj, days_ago=200)
    make_card(conn, alice, obj)
    gaps = [r for r in objects.timeline(conn, alice, obj["obj_id"]) if r["kind"] == "gap"]
    assert gaps and gaps[0]["days"] >= 90 and "記録なし" in gaps[0]["label"]


def test_short_intervals_are_not_marked_as_a_gap(conn, alice, obj):
    make_card(conn, alice, obj, days_ago=3)
    make_card(conn, alice, obj)
    assert not [r for r in objects.timeline(conn, alice, obj["obj_id"]) if r["kind"] == "gap"]


def test_the_timeline_hides_cards_the_viewer_may_not_see(conn, alice, bob, obj):
    make_card(conn, alice, obj, scope="invited_only", after="招待限定の内容")
    mine = objects.timeline(conn, alice, obj["obj_id"])
    theirs = objects.timeline(conn, bob, obj["obj_id"])
    assert any(r["kind"] == "card" for r in mine)
    assert not any(r["kind"] == "card" for r in theirs)


def test_every_timeline_entry_names_its_source(conn, alice, obj):
    make_card(conn, alice, obj)
    for r in objects.timeline(conn, alice, obj["obj_id"]):
        assert r["at"] and r["label"]
        if r["kind"] != "gap":
            assert r["id"]


# ---- 作業室の画面（忍びの地図） ---------------------------------------------------

def body_of(conn, actor, obj):
    return get(conn, f'/o/{obj["obj_id"]}', login(conn, actor)).body.decode()


def test_the_room_shows_the_notes_with_their_rule(conn, alice, obj):
    make_card(conn, alice, obj, after="点検して正常だった")
    make_card(conn, alice, obj, after="異音があり異常と判断した")
    b = body_of(conn, alice, obj)
    assert "確認事項" in b and "要確認" in b and "contradiction" in b


def test_the_room_shows_the_decision_log_with_its_reason_and_model(conn, alice, obj):
    make_card(conn, alice, obj)
    b = body_of(conn, alice, obj)
    assert "AI の判断" in b and "classify" in b and "claude" in b


def test_the_room_shows_the_cost_spent_on_this_object(conn, alice, obj):
    make_card(conn, alice, obj)
    assert "この物で使った" in body_of(conn, alice, obj)


def test_the_room_shows_pending_approvals(conn, alice, obj):
    make_card(conn, alice, obj)
    notifications.save_draft(conn, "org_1", obj["obj_id"],
                             agent.Decision(stage="periodic", tool="draft_notification", dec_id="dec_x",
                                            args={**C, "recipient": "オーナー", "message": "期限を過ぎています。"}))
    b = body_of(conn, alice, obj)
    assert "承認待ち" in b and "/n" in b


def test_the_room_says_it_is_not_for_watching_people(conn, alice, obj):
    assert "監視" in body_of(conn, alice, obj)


def test_the_room_hides_everything_from_a_member_who_may_not_see_the_decisions(conn, alice, bob, obj):
    card = make_card(conn, alice, obj, scope="invited_only", after="招待限定の作業内容です")
    b = get(conn, f'/o/{obj["obj_id"]}', login(conn, bob)).body.decode()
    assert card["card_id"] not in b and "招待限定の作業内容" not in b


def test_the_room_hides_decisions_about_a_card_that_no_longer_exists(conn, alice, obj):
    """消したカードについての判断は、時系列にも判断の記録にも出さない（根拠をたどれないため）。"""
    gone = make_card(conn, alice, obj, after="消す予定のカード")
    kept = make_card(conn, alice, obj, after="残すカード")
    cards.delete_card(conn, alice, gone["card_id"])
    rows = objects.timeline(conn, alice, obj["obj_id"])
    shown = {r.get("row")["card_id"] for r in rows if r["kind"] == "decision" and r.get("row")["card_id"]}
    assert gone["card_id"] not in shown and kept["card_id"] in shown
    assert gone["card_id"] not in body_of(conn, alice, obj)


def test_a_member_of_another_org_cannot_open_the_room(conn, alice, outsider, obj):
    make_card(conn, alice, obj)
    assert get(conn, f'/o/{obj["obj_id"]}', login(conn, outsider)).status == 404


# ---- タグの利用履歴（ポートキー） ---------------------------------------------------

def test_reading_a_tag_is_recorded(conn, alice, obj):
    tag = objects.issue_tag(conn, alice, obj["obj_id"])
    get(conn, f'/t/{tag["tag_id"]}')
    row = db.one(conn, "SELECT * FROM audit_log WHERE action='tag.read'")
    assert row and row["target"] == tag["tag_id"]


def test_the_tag_record_keeps_no_address_or_identity_of_an_anonymous_reader(conn, alice, obj):
    tag = objects.issue_tag(conn, alice, obj["obj_id"])
    get(conn, f'/t/{tag["tag_id"]}', ip="203.0.113.9")
    row = db.one(conn, "SELECT * FROM audit_log WHERE action='tag.read'")
    assert "203.0.113" not in (row["detail"] or "") and row["actor"] == "anon"


def test_the_room_shows_when_the_tag_was_last_read(conn, alice, obj):
    tag = objects.issue_tag(conn, alice, obj["obj_id"])
    get(conn, f'/t/{tag["tag_id"]}')
    assert "最後に読まれた" in body_of(conn, alice, obj)


def test_a_disabled_tag_is_not_read_and_not_recorded(conn, alice, obj):
    tag = objects.issue_tag(conn, alice, obj["obj_id"])
    objects.disable_tag(conn, alice, tag["tag_id"])
    assert get(conn, f'/t/{tag["tag_id"]}').status == 404
    assert db.one(conn, "SELECT * FROM audit_log WHERE action='tag.read'") is None
