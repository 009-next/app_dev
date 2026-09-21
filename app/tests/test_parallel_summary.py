"""要約（D4）を、並列の 4 つ目にする。順番に実行する経路（create_card）は変えない。"""

import re
import threading
import time

import pytest

from app import cards, db, jobs, llm, objects
from app.tests.fakes import client_dynamic
from app.tests.test_async_cards import C, WRITE, Manual, async_create, script  # noqa: F401

IDS = re.compile(r"card_[0-9a-f]+")


def prompt_of(kw):
    return " ".join(m["content"] for m in kw["messages"])


def cite_all(kw):
    return [("update_summary", {**C, "summary": "点検の経緯です。", "evidence_card_ids": list(dict.fromkeys(IDS.findall(prompt_of(kw))))})]


def summary_calls(f):
    return [c for c in f.state["calls"] if "update_summary" in {t["name"] for t in c["tools"]}]


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def test_summary_runs_as_a_fourth_parallel_branch(conn, alice, obj):
    """4 つが同時に走れば、遅い応答（各 0.4 秒）でも、全体は約 0.4 秒。直列の要約なら約 0.8 秒になる。"""
    def fn(kw, i):
        time.sleep(0.4)
        names = {t["name"] for t in kw["tools"]}
        if "update_summary" in names:
            return cite_all(kw)
        if "select_card_type" in names:
            return [("select_card_type", {**C, "type_id": "cleaning"})]
        return [("no_action", C)] if "propose_extra_mask" in names else [WRITE]
    f = client_dynamic(fn)
    j = jobs.Threaded(lambda: conn, close=False)
    t0 = time.time()
    r = async_create(conn, alice, obj, j, f)
    assert time.time() - t0 < 0.3  # 保存は、AI を待たずに返る
    assert j.join(5)
    assert time.time() - t0 < 0.65
    assert len(summary_calls(f)) == 1
    assert db.get_card(conn, alice.org_id, r["card"]["card_id"])["ai_status"] == "done"


def test_summary_input_excludes_the_title_of_the_card_being_created(conn, alice, obj):
    f = client_dynamic(lambda kw, i: cite_all(kw) if "update_summary" in {t["name"] for t in kw["tools"]}
                       else [("select_card_type", {**C, "type_id": "cleaning"})] if "select_card_type" in {t["name"] for t in kw["tools"]}
                       else [WRITE])
    j = Manual(parallel=True)
    r = async_create(conn, alice, obj, j, f)
    j.run_pending()
    p = prompt_of(summary_calls(f)[0])
    assert "エアコン清掃" not in p            # 説明文の生成物（タイトル）は、要約の入力に入れない
    assert "清掃した" in p                     # 作業後の文章は入れる
    assert r["card"]["card_id"] in p           # 根拠にできる
    assert db.get_object(conn, alice.org_id, obj["obj_id"])["summary"] == "点検の経緯です。"


def test_summary_still_includes_titles_of_other_cards(conn, alice, obj):
    f = script(summary=("no_action", C))
    old = cards.create_card(conn, alice, obj["obj_id"], before_desc="前", after_desc="後", client_factory=f)["card"]
    db.run(conn, "UPDATE card SET title='前回の点検' WHERE card_id=?", (old["card_id"],))
    conn.commit()
    f2 = script(summary=("no_action", C))
    j = Manual(parallel=True)
    async_create(conn, alice, obj, j, f2)
    j.run_pending()
    assert "前回の点検" in prompt_of(summary_calls(f2)[0])


def test_invited_only_card_makes_no_summary_call_in_parallel(conn, alice, obj):
    f = script()
    j = Manual(parallel=True)
    async_create(conn, alice, obj, j, f, scope="invited_only")
    j.run_pending()
    assert summary_calls(f) == []


def test_parallel_summary_result_equals_sequential(conn, alice, obj):
    """同じ応答なら、並列でも順番でも、物の要約と根拠は同じ。"""
    def run(parallel):
        o = objects.register_object(conn, alice, f"空調{parallel}")[0]
        f = client_dynamic(lambda kw, i: cite_all(kw) if "update_summary" in {t["name"] for t in kw["tools"]}
                           else [("select_card_type", {**C, "type_id": "cleaning"})] if "select_card_type" in {t["name"] for t in kw["tools"]}
                           else [WRITE])
        if parallel:
            j = Manual(parallel=True)
            async_create(conn, alice, o, j, f)
            j.run_pending()
        else:
            cards.create_card(conn, alice, o["obj_id"], before_desc="エアコンのフィルターが汚れている", after_desc="清掃した", client_factory=f)
        row = db.get_object(conn, alice.org_id, o["obj_id"])
        return row["summary"], row["summary_status"], len(__import__("json").loads(row["summary_sources"]))
    assert run(True) == run(False)


def test_deadline_message_names_summary(conn, alice, obj):
    """要約が期限までに終わらなければ、その名前を挙げて失敗になる（他の結果は保存済みのまま）。"""
    gate = threading.Event()

    def fn(kw, i):
        names = {t["name"] for t in kw["tools"]}
        if "update_summary" in names:
            gate.wait(3)
            return [("no_action", C)]
        if "select_card_type" in names:
            return [("select_card_type", {**C, "type_id": "cleaning"})]
        return [WRITE]
    f = client_dynamic(fn)
    j = Manual(parallel=True)
    r = async_create(conn, alice, obj, j, f, config={**llm.load_config(), "job_deadline": 0.5})
    j.run_pending()
    gate.set()
    c = db.get_card(conn, alice.org_id, r["card"]["card_id"])
    assert c["ai_status"] == "timeout" and c["card_type"] == "cleaning" and c["title"]
    assert "summary" in db.one(conn, "SELECT detail FROM audit_log WHERE action='card.ai_timeout'")["detail"]
