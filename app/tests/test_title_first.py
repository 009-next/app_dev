"""タイトル（説明文）を先に出す: 並列の判断は、揃うのを待たず、出たものから順に保存する。"""

import json
import time

import pytest

from app import agent, auth, authz, cards, db, jobs, objects, web
from app.tests.fakes import client_dynamic
from app.tests.test_async_cards import C, WRITE, Manual, row

DEFAULT_DELAYS = {"classify": 0.05, "privacy": 0.05, "text": 0.05, "summary": 0.05}


def slow(delays=None, classify=None, privacy=None):
    """段階ごとに、待ち時間を変えられる偽の応答。classify / privacy には、応答（ツールの呼び出し）を上書きできる。"""
    d = {**DEFAULT_DELAYS, **(delays or {})}

    def fn(kw, i):
        names = {t["name"] for t in kw["tools"]}
        if "update_summary" in names:
            time.sleep(d["summary"])
            return [("no_action", C)]
        if "propose_extra_mask" in names:
            time.sleep(d["privacy"])
            return privacy or [("no_action", C)]
        if "select_card_type" in names:
            time.sleep(d["classify"])
            return classify or [("select_card_type", {**C, "type_id": "cleaning"})]
        time.sleep(d["text"])
        return [WRITE]
    return client_dynamic(fn)


@pytest.fixture(autouse=True)
def _reset():
    web.reset_limits()
    auth.reset_rate()
    web.JOBS = jobs.Inline()
    yield
    web.JOBS = jobs.Inline()


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def test_title_appears_while_a_slow_privacy_decision_is_still_running(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init(conn)
    db.run(conn, "INSERT INTO org VALUES('o','組織','c',?)", (db.now(),))
    mid = auth.add_member(conn, "o", "a@example.test", "member")
    conn.commit()
    actor = authz.Actor(kind="member", org_id="o", member_id=mid, role="member")
    obj = objects.register_object(conn, actor, "空調")[0]
    runner = jobs.Threaded(lambda: db.connect(path))  # AI の処理は、専用の接続（本番と同じ）
    t0 = time.time()
    cid = cards.create_card_async(conn, actor, obj["obj_id"], jobs=runner, before_desc="汚れ", client_factory=slow({"privacy": 1.2, "classify": 0.02, "text": 0.25}))["card"]["card_id"]
    t_title = t_done = None
    while time.time() - t0 < 10:
        r = db.one(conn, "SELECT ai_text, ai_status, card_type FROM card WHERE card_id=?", (cid,))
        if t_title is None and r["ai_text"]:
            t_title, status_then, type_then = time.time() - t0, r["ai_status"], r["card_type"]
        if r["ai_status"] == "done":
            t_done = time.time() - t0
            break
        time.sleep(0.03)
    assert runner.join(10)
    assert t_title is not None and t_title < 0.9          # 説明文（0.25秒）が出た時点で、タイトルが付く
    assert status_then == "running" and type_then == "cleaning"  # まだ処理中。分類の結果も、共有の判断を待たずに付いている
    assert t_done >= 1.2 and t_done - t_title > 0.5       # 共有の判断（1.2秒）と要約が終わるのは、そのあと


def test_privacy_scope_proposal_wins_over_classify_in_either_finishing_order(conn, alice, obj):
    classify_scope = [("propose_narrower_scope", {**C, "scope": "org_only"})]
    privacy_scope = [("propose_narrower_scope", {**C, "scope": "invited_only"})]
    for delays in ({"classify": 0.4, "privacy": 0.05}, {"classify": 0.05, "privacy": 0.4}):  # 分類が後／共有が後
        j = Manual(parallel=True)
        cid = cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, before_desc="汚れ",
                                      client_factory=slow(delays, classify=classify_scope, privacy=privacy_scope))["card"]["card_id"]
        j.run_pending()
        assert row(conn, cid)["proposed_scope"] == "invited_only", delays  # 順番に実行するときと同じ（共有の判断が優先）


def test_saved_results_survive_a_failure_in_another_branch(conn, alice, obj, monkeypatch):
    real = agent.decide

    def flaky(c, org, *, stage, **kw):
        if stage == "privacy":
            time.sleep(0.3)
            raise RuntimeError("boom")
        return real(c, org, stage=stage, **kw)
    monkeypatch.setattr(agent, "decide", flaky)
    j = Manual(parallel=True)
    cid = cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, before_desc="汚れ", client_factory=slow())["card"]["card_id"]
    j.run_pending()
    r = row(conn, cid)
    assert r["ai_status"] == "failed" and r["title"] == "エアコン清掃" and json.loads(r["ai_text"])["title"]  # 出せた説明文は残る


def test_sequential_and_parallel_still_agree(conn, alice):
    a = objects.register_object(conn, alice, "A")[0]
    b = objects.register_object(conn, alice, "B")[0]
    priv = [("propose_extra_mask", {**C, "target": "宛名"})]
    seq = cards.create_card(conn, alice, a["obj_id"], before_desc="汚れ", client_factory=slow(privacy=priv))["card"]
    j = Manual(parallel=True)
    cid = cards.create_card_async(conn, alice, b["obj_id"], jobs=j, before_desc="汚れ", client_factory=slow(privacy=priv))["card"]["card_id"]
    j.run_pending()
    s, p = row(conn, seq["card_id"]), row(conn, cid)
    assert (s["card_type"], s["title"], s["proposed_mask"]) == (p["card_type"], p["title"], p["proposed_mask"]) and p["proposed_mask"]


def test_page_shows_title_and_a_progress_note_while_still_running(conn, alice, obj):
    from app.tests.test_web import get, login, post, text
    web.JOBS = Manual()
    ck = login(conn, alice)
    loc = dict(post(conn, f"/o/{obj['obj_id']}/cards", {"before_desc": "汚れ", "scope": "org_only"}, cookie=ck).headers)["Location"]
    assert "作成しています" in text(get(conn, loc, cookie=ck))  # 何もできていない間
    cid = loc.rsplit("/", 1)[1]
    db.run(conn, "UPDATE card SET ai_text=?, title=? WHERE card_id=?", (json.dumps({"title": "エアコン清掃", "changes": ["清掃"], "description": "d"}, ensure_ascii=False), "エアコン清掃", cid))
    page = text(get(conn, loc, cookie=ck))
    assert "エアコン清掃" in page and "説明文ができました" in page and '<meta http-equiv="refresh" content="3">' in page  # 出ている分は見せて、更新は続ける
