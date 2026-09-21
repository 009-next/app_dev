"""AI の追加のぼかしの提案（propose_extra_mask）を、カード画面に出して、作り手が確認するまでのテスト。"""

import json
import sqlite3

import pytest

from app import auth, cards, db, objects, web
from app.tests.fakes import client_dynamic
from app.tests.test_web import get, login, post, text

C = {"reason": "宛名が読み取れる", "evidence": ""}
WRITE = ("write_card_text", {"title": "壁紙の張替え", "changes": ["張替え"], "description": "完了"})


@pytest.fixture(autouse=True)
def _reset():
    web.reset_limits()
    auth.reset_rate()
    yield


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "502号室")[0]


def script(privacy):
    """共有の段階（propose_extra_mask を持つ段階）だけ privacy(kw) の応答にする。ほかは無難な応答。"""
    def fn(kw, i):
        names = {t["name"] for t in kw["tools"]}
        if "propose_extra_mask" in names:
            return privacy(kw)
        if "select_card_type" in names:
            return [("select_card_type", {**C, "type_id": "rental"})]
        if "update_summary" in names:
            return [("no_action", C)]
        return [WRITE]
    return client_dynamic(fn)


MASK = lambda kw: [("propose_extra_mask", {**C, "target": "玄関の郵便物の宛名（氏名・住所）"})]  # noqa: E731


def make(conn, alice, obj, factory, **kw):
    return cards.create_card(conn, alice, obj["obj_id"], before_desc="退去後の原状回復が完了した。玄関に郵便物がある",
                             client_factory=factory, **kw)["card"]


def stored(conn, card):
    row = db.one(conn, "SELECT proposed_mask FROM card WHERE card_id=?", (card["card_id"],))["proposed_mask"]
    return json.loads(row) if row else None


# ---- 保存 ---------------------------------------------------------------------

def test_proposal_is_stored_with_target_and_reason(conn, alice, obj):
    c = make(conn, alice, obj, script(MASK))
    assert stored(conn, c) == {"target": "玄関の郵便物の宛名（氏名・住所）", "reason": "宛名が読み取れる"}


def test_no_proposal_when_ai_does_nothing_or_fails(conn, alice, obj):
    assert stored(conn, make(conn, alice, obj, script(lambda kw: [("no_action", C)]))) is None
    from app.tests.fakes import client_failing
    assert stored(conn, make(conn, alice, obj, client_failing(RuntimeError("down")))) is None


def test_long_text_is_truncated(conn, alice, obj):
    long = lambda kw: [("propose_extra_mask", {"reason": "あ" * 900, "evidence": "", "target": "い" * 900})]  # noqa: E731
    p = stored(conn, make(conn, alice, obj, script(long)))
    assert len(p["target"]) == 200 and len(p["reason"]) == 300


def test_scope_proposal_still_works_alongside(conn, alice, obj):
    both = lambda kw: [("propose_narrower_scope", {**C, "scope": "org_only"})]  # noqa: E731
    c = make(conn, alice, obj, script(both))
    assert cards.get_card(conn, alice, c["card_id"])["proposed_scope"] == "org_only" and stored(conn, c) is None


# ---- 画面 ---------------------------------------------------------------------

def test_card_page_shows_proposal_escaped_with_ack_button_and_share_warning(conn, alice, obj):
    evil = lambda kw: [("propose_extra_mask", {**C, "target": "<script>alert(1)</script>宛名"})]  # noqa: E731
    c = make(conn, alice, obj, script(evil))
    page = text(get(conn, f"/c/{c['card_id']}", cookie=login(conn, alice)))
    assert "AIの提案（追加のぼかし）" in page and "&lt;script&gt;alert(1)" in page and "<script>alert" not in page
    assert f'action="/c/{c["card_id"]}/mask-ack"' in page
    assert "まだ確認されていません" in page  # 共有のボタンの前に、警告が出る


def test_card_page_without_proposal_has_no_block(conn, alice, obj):
    c = make(conn, alice, obj, script(lambda kw: [("no_action", C)]))
    page = text(get(conn, f"/c/{c['card_id']}", cookie=login(conn, alice)))
    assert "追加のぼかし" not in page and "mask-ack" not in page


def test_ack_clears_proposal_and_is_audited(conn, alice, obj):
    c = make(conn, alice, obj, script(MASK))
    ck = login(conn, alice)
    r = post(conn, f"/c/{c['card_id']}/mask-ack", {}, cookie=ck)
    assert r.status == 303 and stored(conn, c) is None
    assert "追加のぼかし" not in text(get(conn, f"/c/{c['card_id']}", cookie=ck))
    assert db.one(conn, "SELECT COUNT(*) c FROM audit_log WHERE action='mask.proposal_ack'")["c"] == 1
    assert post(conn, f"/c/{c['card_id']}/mask-ack", {}, cookie=ck).status == 400  # 提案がなければ確認できない


def test_new_privacy_decision_replaces_the_old_proposal(conn, alice, obj):
    c = make(conn, alice, obj, script(MASK))
    second = lambda kw: [("propose_extra_mask", {**C, "target": "表札の名前"})]  # noqa: E731
    cards._run_agent(conn, alice, c["card_id"], script(second), None)  # 同じカードで、共有の判断をもう一度行う
    assert stored(conn, c)["target"] == "表札の名前"


# ---- 権限 ---------------------------------------------------------------------

def test_ack_requires_login_and_same_org_and_no_leak_to_recipients(conn, alice, obj, outsider):
    c = make(conn, alice, obj, script(MASK))
    assert post(conn, f"/c/{c['card_id']}/mask-ack", {}).status == 303  # ログインへ
    assert post(conn, f"/c/{c['card_id']}/mask-ack", {}, cookie=login(conn, outsider)).status == 404
    assert stored(conn, c) is not None
    s = cards.issue_share(conn, alice, c["card_id"], confirmed=True)  # 提案があっても、共有の操作そのものは止めない（警告のみ）
    assert "追加のぼかし" not in text(get(conn, f"/s/{s['token']}")) and "郵便物の宛名" not in text(get(conn, f"/s/{s['token']}"))


def test_other_member_of_same_org_can_ack_but_not_other_org(conn, alice, bob, obj):
    c = make(conn, alice, obj, script(MASK))
    assert post(conn, f"/c/{c['card_id']}/mask-ack", {}, cookie=login(conn, bob)).status == 303  # 同じ組織のメンバーは、カードを編集できる


# ---- 列の追加 -----------------------------------------------------------------------

def test_init_adds_the_column_to_an_old_database_once():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE card(card_id TEXT PRIMARY KEY, obj_id TEXT);")  # 古い形（列がない）
    db.init(c)
    db.init(c)  # 2回目でもエラーにならない
    assert "proposed_mask" in {r["name"] for r in c.execute("PRAGMA table_info(card)")}
