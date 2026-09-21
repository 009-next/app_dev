"""カード作成の遅さの対策: 保存を先に返す（非同期）／分類・共有・説明文を同時に実行する（並列）／サーバーを複数スレッドにする。"""

import http.client
import threading
import time
import urllib.parse
from http import server

import pytest

from app import auth, cards, db, jobs, llm, objects, web
from app.tests.fakes import client_dynamic

C = {"reason": "r", "evidence": ""}
WRITE = ("write_card_text", {"title": "エアコン清掃", "changes": ["清掃した"], "description": "清掃して風量が回復した"})


@pytest.fixture(autouse=True)
def _reset():
    web.reset_limits()
    auth.reset_rate()
    web.CLIENT_FACTORY = None
    web.JOBS = jobs.Inline()
    yield
    web.CLIENT_FACTORY = None
    web.JOBS = jobs.Inline()


class Manual:
    """テスト用: 処理を溜めておき、run_pending() で実行する。"""
    is_async = True

    def __init__(self, parallel=False):
        self.parallel = parallel
        self.queue = []

    def submit(self, conn, fn):
        self.queue.append((conn, fn))

    def run_pending(self):
        q, self.queue = self.queue, []
        for conn, fn in q:
            fn(conn)


def script(delay=0.0, summary=("no_action", C)):
    """段階（ツールの組）を見て、無難な応答を返す。delay 秒だけ待つ（LLM の待ちを模す）。"""
    def fn(kw, i):
        if delay:
            time.sleep(delay)
        names = {t["name"] for t in kw["tools"]}
        if "update_summary" in names:
            return [summary]
        if "propose_extra_mask" in names:
            return [("propose_extra_mask", {**C, "target": "宛名"})]
        if "select_card_type" in names:
            return [("select_card_type", {**C, "type_id": "cleaning"})]
        return [WRITE]
    return client_dynamic(fn)


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def async_create(conn, alice, obj, j, factory, **kw):
    return cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, before_desc="エアコンのフィルターが汚れている",
                                   after_desc="清掃した", client_factory=factory, **kw)


def row(conn, card_id):
    return db.one(conn, "SELECT * FROM card WHERE card_id=?", (card_id,))


# ---- 非同期: 保存を先に返す ------------------------------------------------------------

def test_async_create_returns_before_any_llm_call_then_job_fills_in(conn, alice, obj, jpeg, tmp_path):
    f, j = script(), Manual()
    out = async_create(conn, alice, obj, j, f, images_in=[("before", jpeg, None)], data_dir=tmp_path)
    cid = out["card"]["card_id"]
    assert out["async"] and f.state["calls"] == [] and len(j.queue) == 1  # LLM は、まだ 1 回も呼ばれていない
    assert row(conn, cid)["ai_status"] == "running" and row(conn, cid)["card_type"] == "generic"
    assert db.one(conn, "SELECT COUNT(*) c FROM image WHERE card_id=?", (cid,))["c"] == 1  # 写真は、すぐ保存されている
    j.run_pending()
    r = row(conn, cid)
    assert (r["ai_status"], r["card_type"], r["title"]) == ("done", "cleaning", "エアコン清掃") and len(f.state["calls"]) == 4


def test_no_material_means_no_job_and_no_status(conn, alice, obj):
    j = Manual()
    out = cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, client_factory=script())
    assert j.queue == [] and row(conn, out["card"]["card_id"])["ai_status"] is None


def test_unexpected_job_failure_marks_failed_and_keeps_the_card(conn, alice, obj, monkeypatch):
    j = Manual()
    cid = async_create(conn, alice, obj, j, script())["card"]["card_id"]
    monkeypatch.setattr(cards, "_run_agent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    j.run_pending()
    assert row(conn, cid)["ai_status"] == "failed" and db.get_card(conn, alice.org_id, cid)
    assert "RuntimeError" in db.one(conn, "SELECT detail FROM audit_log WHERE action='card.ai_failed'")["detail"]


def test_llm_failure_is_not_a_job_failure(conn, alice, obj):
    from app.tests.fakes import client_failing
    j = Manual()
    cid = async_create(conn, alice, obj, j, client_failing(RuntimeError("down")))["card"]["card_id"]
    j.run_pending()
    assert row(conn, cid)["ai_status"] == "done" and row(conn, cid)["card_type"] == "generic"  # 既定動作に落ちて、処理は完了


def test_sync_create_card_still_leaves_status_empty(conn, alice, obj):
    c = cards.create_card(conn, alice, obj["obj_id"], before_desc="x", client_factory=script())["card"]
    assert c["ai_status"] is None and c["card_type"] == "cleaning"


# ---- 並列 --------------------------------------------------------------------------------

def outcome(conn, cid):
    r = row(conn, cid)
    obj_row = db.one(conn, "SELECT summary, summary_status FROM object")
    return (r["card_type"], r["title"], r["proposed_mask"], r["proposed_scope"], obj_row["summary_status"])


def test_parallel_run_gives_the_same_result_as_sequential(conn, alice):
    a = objects.register_object(conn, alice, "A")[0]
    b = objects.register_object(conn, alice, "B")[0]
    seq = cards.create_card(conn, alice, a["obj_id"], before_desc="エアコンが汚れている", client_factory=script())["card"]
    cid = cards.create_card_async(conn, alice, b["obj_id"], jobs=(j := Manual(parallel=True)), before_desc="エアコンが汚れている",
                                  client_factory=script())["card"]["card_id"]
    j.run_pending()
    s, p = row(conn, seq["card_id"]), row(conn, cid)
    assert (s["card_type"], s["title"], s["proposed_mask"], s["proposed_scope"]) == (p["card_type"], p["title"], p["proposed_mask"], p["proposed_scope"])
    assert db.one(conn, "SELECT COUNT(*) c FROM decision_log WHERE obj_id=?", (a["obj_id"],))["c"] == \
        db.one(conn, "SELECT COUNT(*) c FROM decision_log WHERE obj_id=?", (b["obj_id"],))["c"] == 3  # 分類・共有・要約


def test_parallel_merges_costs_and_keeps_decision_order(conn, alice, obj):
    card = cards.create_card_async(conn, alice, obj["obj_id"], jobs=(j := Manual(parallel=True)), before_desc="汚れ",
                                   client_factory=script())["card"]
    captured = {}
    real = cards._run_agent

    def spy(*a, **k):
        captured.update(real(*a, **k))
        return captured
    cards._run_agent = spy
    try:
        j.run_pending()
    finally:
        cards._run_agent = real
    assert [d.stage for d in captured["decisions"]] == ["classify", "privacy", "summary"]
    total = sum(r["cost_usd"] for r in db.many(conn, "SELECT cost_usd FROM llm_call"))
    assert captured["cost_usd"] == pytest.approx(total) and total > 0
    assert row(conn, card["card_id"])["ai_status"] == "done"


def test_parallel_is_faster_and_async_returns_immediately(conn, alice, obj):
    delay = 0.3
    t0 = time.time()
    cards.create_card_async(conn, alice, obj["obj_id"], jobs=Manual(), before_desc="汚れ", client_factory=script(delay))
    assert time.time() - t0 < 0.15  # 保存だけ。LLM を待たない

    def run(parallel):
        j = Manual(parallel=parallel)
        cid = cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, before_desc="汚れ", client_factory=script(delay))["card"]["card_id"]
        t = time.time()
        j.run_pending()
        return time.time() - t, cid
    seq, _ = run(False)
    par, cid = run(True)
    assert seq >= 4 * delay * 0.95  # 順番だと、4 回ぶん待つ
    assert par < 3 * delay  # 同時なら、分類・共有・説明文が 1 回ぶん＋要約 1 回ぶん（約 2 回ぶん）
    assert row(conn, cid)["ai_status"] == "done"


def test_parallel_branches_do_not_share_the_question_limit_or_call_counts(conn, alice, obj):
    ask = lambda: [("request_more_input", {**C, "question": "何の作業ですか"})]  # noqa: E731

    def fn(kw, i):
        names = {t["name"] for t in kw["tools"]}
        if "select_card_type" in names:
            return ask()
        if "propose_extra_mask" in names:
            return [("no_action", C)]
        if "update_summary" in names:
            return [("no_action", C)]
        return [WRITE]
    j = Manual(parallel=True)
    cid = cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, before_desc="直した", client_factory=client_dynamic(fn))["card"]["card_id"]
    j.run_pending()
    assert row(conn, cid)["pending_question"] == "何の作業ですか"


# ---- 画面 ------------------------------------------------------------------------------------

def post_create(conn, ck, obj):
    from app.tests.test_web import post
    return post(conn, f"/o/{obj['obj_id']}/cards", {"before_desc": "エアコンが汚れている", "scope": "org_only"}, cookie=ck)


def test_web_returns_at_once_and_page_refreshes_until_done(conn, alice, obj):
    from app.tests.test_web import get, login, text
    web.JOBS = j = Manual()
    web.CLIENT_FACTORY = script()
    ck = login(conn, alice)
    r = post_create(conn, ck, obj)
    loc = dict(r.headers)["Location"]
    assert r.status == 303 and web.CLIENT_FACTORY.state["calls"] == []
    running = text(get(conn, loc, cookie=ck))
    assert "作成しています" in running and '<meta http-equiv="refresh" content="3">' in running
    j.run_pending()
    done = text(get(conn, loc, cookie=ck))
    assert "エアコン清掃" in done and "作成しています" not in done and "http-equiv" not in done


def test_web_shows_stale_and_failed_states(conn, alice, obj):
    from app.tests.test_web import get, login, text
    web.JOBS = Manual()
    ck = login(conn, alice)
    loc = dict(post_create(conn, ck, obj).headers)["Location"]
    db.shift_clock(200)  # 200 秒たっても running のまま
    stale = text(get(conn, loc, cookie=ck))
    assert "中断された可能性" in stale and "http-equiv" not in stale
    db.run(conn, "UPDATE card SET ai_status='failed'")
    assert "AIの処理に失敗しました" in text(get(conn, loc, cookie=ck))


def test_web_without_async_jobs_keeps_the_old_synchronous_behaviour(conn, alice, obj):
    from app.tests.test_web import get, login, text
    web.CLIENT_FACTORY = script()
    ck = login(conn, alice)
    loc = dict(post_create(conn, ck, obj).headers)["Location"]
    page = text(get(conn, loc, cookie=ck))
    assert "エアコン清掃" in page and "作成しています" not in page and row(conn, loc.rsplit("/", 1)[1])["ai_status"] is None


# ---- 本物のサーバー（複数スレッド）で、待たずに返り、ほかの人も待たされない ---------------------------------------

def test_threaded_server_returns_quickly_and_serves_others_during_the_ai_job(conn, alice, obj):
    web.CLIENT_FACTORY = script(delay=0.4)
    tag = db.one(conn, "SELECT tag_id FROM tag")["tag_id"]
    sid = auth.create_session(conn, "member", member_id=alice.member_id)
    conn.commit()
    shared = db.serialized(conn)  # テストでは、メモリ上の DB を、リクエストと AI の処理で共有する（本番は、AI の処理が専用の接続）
    web.JOBS = j = jobs.Threaded(lambda: shared, close=False)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(shared))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def request(method, path, body=None, headers=None):
            h = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            h.request(method, path, body=body, headers=headers or {})
            r = h.getresponse()
            data = r.read()
            loc = r.getheader("Location")
            h.close()
            return r.status, loc, data
        t0 = time.time()
        status, loc, _ = request("POST", f"/o/{obj['obj_id']}/cards",
                                 urllib.parse.urlencode({"before_desc": "エアコンが汚れている", "scope": "org_only"}).encode(),
                                 {"Cookie": f"sid={sid}", "Content-Type": "application/x-www-form-urlencoded"})
        first = time.time() - t0
        assert status == 303 and first < 0.35  # AI の待ち（分類・共有・説明文で 0.4 秒、要約で 0.4 秒）を待たずに返る
        t1 = time.time()
        pub_status, _, pub = request("GET", f"/t/{tag}")  # AI の処理中に、ほかの人（匿名）が、待たされずに開ける
        assert pub_status == 200 and time.time() - t1 < 0.3 and "空調" in pub.decode()
        assert j.join(10)
        _, _, page = request("GET", loc, headers={"Cookie": f"sid={sid}"})
        assert "エアコン清掃" in page.decode() and "作成しています" not in page.decode()
    finally:
        srv.shutdown()
        srv.server_close()


# ---- 設定 -----------------------------------------------------------------------------------------

def test_main_installs_threaded_jobs_and_threading_server():
    import inspect
    src = inspect.getsource(web.main)
    assert "jobs.Threaded" in src and "ThreadingHTTPServer" in src and "HTTPServer(" not in src.replace("ThreadingHTTPServer(", "")
    assert isinstance(web.JOBS, jobs.Inline)  # 既定（テスト・import 時）は同期のまま


# ---- 接続の共有（並列の土台） ---------------------------------------------------------------------------------

def test_locked_conn_survives_heavy_concurrent_use_of_the_same_statements(conn):
    shared = db.serialized(conn)
    errors = []

    def work(k):
        try:
            for i in range(150):
                db.run(shared, "INSERT INTO audit_log VALUES(?,?,?,?,?,?,?)", (f"a{k}_{i}", "o", "x", "t", "", "", db.now()))
                assert db.one(shared, "SELECT COUNT(*) c FROM audit_log")["c"] >= 1
                shared.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
    threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [x.start() for x in threads]
    [x.join(30) for x in threads]
    assert errors == [] and db.one(conn, "SELECT COUNT(*) c FROM audit_log")["c"] == 8 * 150


def test_locked_conn_behaves_like_a_connection_for_our_code(conn):
    s = db.serialized(conn)
    assert db.serialized(s) is s
    r = s.execute("INSERT INTO org VALUES('x','X','c',0)")
    assert r.rowcount == 1 and db.one(s, "SELECT name FROM org WHERE org_id='x'")["name"] == "X"
    assert [x["org_id"] for x in s.execute("SELECT org_id FROM org WHERE org_id='x'")] == ["x"]
    assert db.one(s, "SELECT 1 FROM org WHERE org_id='none'") is None and db.many(s, "SELECT 1 WHERE 0") == []


def test_parallel_branches_receive_a_locked_connection(conn, alice, obj, monkeypatch):
    from app import agent
    seen = []
    real = agent.decide

    def spy(c, *a, **k):
        seen.append(type(c).__name__)
        return real(c, *a, **k)
    monkeypatch.setattr(agent, "decide", spy)
    j = Manual(parallel=True)
    async_create(conn, alice, obj, j, script())
    j.run_pending()
    assert seen.count("LockedConn") == 3  # 分類・共有・要約（要約も、並列の 4 つ目として、同じ入れ物を通る。説明文は agent.describe）
