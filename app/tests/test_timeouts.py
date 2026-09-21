"""呼び出しのタイムアウトと、別の経路・上のモデルへの切り替え。"""

import threading
import time
from http import server
from types import SimpleNamespace as N

import pytest

from app import agent, cards, db, jobs, llm, objects
from app.tests.fakes import client_dynamic
from app.tests.test_async_cards import C, Manual, row, script

ORG = "org_1"
NO_ACTION = agent.SPEC["tools"]["no_action"]
MSG = [{"role": "user", "content": "x"}]


class APITimeoutError(Exception):
    """SDK のタイムアウトと、クラス名が同じ（llm.is_timeout はクラス名で見分ける）。"""


class Boom(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def route_factory(behaviors):
    """provider -> 応答の作り方。behaviors[p] は、呼ばれるたびに、(例外 | 'ok') を順に返すリスト（使い切ったら最後を繰り返す）。
    呼び出しの引数（timeout など）は .calls に残す。"""
    calls, counters = [], {}

    def factory(provider):
        def create(**kw):
            calls.append((provider, kw))
            seq = behaviors.get(provider, ["ok"])
            i = counters.get(provider, 0)
            counters[provider] = i + 1
            b = seq[min(i, len(seq) - 1)]
            if b != "ok":
                raise b
            usage = N(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            msg = N(content=[N(type="tool_use", name="no_action", input={"reason": "r", "evidence": ""})], usage=usage)
            return N(headers={"x-orca-resolved-model": "claude-haiku-4-5"}, parse=lambda: msg)
        return N(messages=N(with_raw_response=N(create=create)))
    factory.calls = calls
    return factory


def ask(conn, f, kind="triage", cfg=None):
    return llm.call(conn, ORG, kind, "s", [NO_ACTION], MSG, f, cfg)


# ---- タイムアウトの値と、リトライ --------------------------------------------------------------------

def test_timeout_is_passed_per_kind_from_config(conn):
    f = route_factory({})
    for kind in ("triage", "decide_light", "decide_heavy"):
        ask(conn, f, kind)
    assert [kw["timeout"] for _, kw in f.calls] == [12.0, 25.0, 45.0]
    cfg = {**llm.load_config(), "timeouts": {"triage": 3.0}, "timeout": 7.0}
    f2 = route_factory({})
    ask(conn, f2, "triage", cfg)
    ask(conn, f2, "decide_light", cfg)  # 種類の指定がなければ、全体の値
    assert [kw["timeout"] for _, kw in f2.calls] == [3.0, 7.0]


def test_default_client_has_no_sdk_retries(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ORCA_API_KEY", "k")
    assert llm.default_factory("anthropic").max_retries == 0 and llm.default_factory("orca").max_retries == 0


def test_timeout_moves_to_next_route_without_waiting_again_and_is_recorded(conn):
    f = route_factory({"orca": [APITimeoutError("slow")]})
    res = ask(conn, f)
    assert res.provider == "anthropic" and [p for p, _ in f.calls] == ["orca", "anthropic"]  # 同じ経路で、待ち直さない
    assert [r["status"] for r in db.many(conn, "SELECT status FROM llm_call ORDER BY created_at")] == ["timeout", "ok"]
    rep = llm.cost_report(conn)
    assert sum(r["timeouts"] or 0 for r in rep) == 1


def test_transient_failure_is_retried_once_on_the_same_route_but_4xx_and_timeouts_are_not(conn):
    f = route_factory({"orca": [Boom(503), "ok"]})
    assert ask(conn, f).provider == "orca" and [p for p, _ in f.calls] == ["orca", "orca"]
    llm.reset_cooldowns()
    f2 = route_factory({"orca": [Boom(400), "ok"], "anthropic": ["ok"]})
    assert ask(conn, f2).provider == "anthropic" and [p for p, _ in f2.calls] == ["orca", "anthropic"]  # 400 は、やり直さない
    cfg = {**llm.load_config(), "retry_transient": 0}
    llm.reset_cooldowns()
    f3 = route_factory({"orca": [Boom(503), "ok"]})
    assert ask(conn, f3, cfg=cfg).provider == "anthropic" and [p for p, _ in f3.calls] == ["orca", "anthropic"]  # 設定で切れる


# ---- LLMTimeout ----------------------------------------------------------------------------------------

def test_all_routes_timing_out_raises_llmtimeout_which_is_an_llmerror(conn):
    f = route_factory({"orca": [APITimeoutError()], "anthropic": [APITimeoutError()]})
    with pytest.raises(llm.LLMTimeout) as e:
        ask(conn, f)
    assert isinstance(e.value, llm.LLMError)  # 既存の except llm.LLMError は、そのまま動く


def test_mixed_failures_are_plain_llmerror_and_missing_key_does_not_count_as_failure(conn):
    f = route_factory({"orca": [APITimeoutError()], "anthropic": [Boom(401)]})
    with pytest.raises(llm.LLMError) as e:
        ask(conn, f)
    assert not isinstance(e.value, llm.LLMTimeout)  # 401（キー）は、遅いのではなく、故障
    llm.reset_cooldowns()

    def factory(provider):
        if provider == "orca":
            raise llm.LLMError("ORCA_API_KEY が未設定です")
        return route_factory({"anthropic": [APITimeoutError()]})(provider)
    with pytest.raises(llm.LLMTimeout):  # キー未設定の経路があっても、試せた経路がタイムアウトなら、タイムアウト
        ask(conn, factory)


# ---- 連続タイムアウトの後回し ------------------------------------------------------------------------------

def test_two_consecutive_timeouts_deprioritise_a_route_but_it_is_still_tried_last(conn):
    f = route_factory({"orca": [APITimeoutError(), APITimeoutError(), "ok"]})
    ask(conn, f)  # 1回目のタイムアウト → anthropic
    ask(conn, f)  # 2回目 → 連続2回 → orca は後回しに
    assert llm._soft_cooldown.get("orca", 0) > time.time()
    f.calls.clear()
    ask(conn, f)
    assert [p for p, _ in f.calls] == ["anthropic"]  # 先に、後回しでない経路を試し、そこで成功
    only = route_factory({"anthropic": [APITimeoutError(), APITimeoutError(), "ok"]})
    cfg = {**llm.load_config(), "providers": ["anthropic"]}
    llm.reset_cooldowns()
    for _ in range(2):
        with pytest.raises(llm.LLMTimeout):
            ask(conn, only, cfg=cfg)
    assert ask(conn, only, cfg=cfg).provider == "anthropic"  # 1つしかない経路は、後回しでも、試す（止めてしまわない）


def test_success_resets_the_streak(conn):
    f = route_factory({"orca": [APITimeoutError(), "ok", APITimeoutError(), "ok"]})
    for _ in range(4):
        ask(conn, f)
    assert "orca" not in llm._soft_cooldown  # 成功が挟まれば、連続ではない


# ---- 実際の SDK のタイムアウト（ローカルの遅い HTTP サーバーに対して） -----------------------------------------

def test_real_sdk_client_gives_up_at_the_per_call_timeout(conn):
    anthropic = pytest.importorskip("anthropic")

    class Slow(server.BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(2.5)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = anthropic.Anthropic(base_url=f"http://127.0.0.1:{srv.server_address[1]}", api_key="x", max_retries=0, timeout=60.0)
        cfg = {**llm.load_config(), "providers": ["anthropic"], "timeouts": {"triage": 0.4}}
        t0 = time.time()
        with pytest.raises(llm.LLMTimeout):
            ask(conn, lambda p: client, cfg=cfg)
        assert time.time() - t0 < 1.5  # 呼び出しごとの 0.4 秒で打ち切る（クライアントの 60 秒ではなく）
    finally:
        srv.shutdown()
        srv.server_close()


# ---- agent.decide: タイムアウトしたら、上のモデルの段へ ----------------------------------------------------------

def by_model(table):
    def fn(kw, i):
        for key, resp in table.items():
            if key in kw["model"]:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError("想定外のモデル: " + kw["model"])
    return client_dynamic(fn, echo_model=True)


GOOD = ("select_card_type", {"reason": "r", "evidence": "", "type_id": "cleaning"})
INPUTS = {"状況": "ソファのクリーニングを実施した"}


def test_haiku_timeout_moves_to_sonnet_instead_of_the_default(conn):
    f = by_model({"haiku": APITimeoutError("slow"), "sonnet": [GOOD]})
    d = agent.decide(conn, ORG, stage="classify", inputs=INPUTS, card_id="c", client_factory=f)
    assert d.tool == "select_card_type" and not d.default_used and d.model == "claude-sonnet-5"
    assert d.tiers[0]["escalated_because"] == "タイムアウト"
    assert "モデルの切り替え" in db.one(conn, "SELECT validation FROM decision_log")["validation"]
    assert d.cost_usd == pytest.approx(llm.cost_usd("claude-sonnet-5", 100, 20))  # 費用は、応答が返った分だけ


def test_all_tiers_timing_out_falls_back_to_the_default(conn):
    f = by_model({"haiku": APITimeoutError(), "sonnet": APITimeoutError(), "opus": APITimeoutError()})
    d = agent.decide(conn, ORG, stage="classify", inputs=INPUTS, card_id="c", client_factory=f)
    assert d.default_used and (d.tool, d.args) == agent.DEFAULTS["classify"] and "LLMの失敗" in d.default_reason
    assert len(f.state["calls"]) == 6  # 3つの段 × 2つの経路（Orca → Claude API 直）を、それぞれ1回ずつ試した


def test_non_timeout_error_at_the_first_tier_still_defaults_immediately(conn):
    f = by_model({"haiku": Boom(401), "sonnet": [GOOD]})
    d = agent.decide(conn, ORG, stage="classify", inputs=INPUTS, card_id="c", client_factory=f)
    assert d.default_used and "sonnet" not in " ".join(c["model"] for c in f.state["calls"])  # 従来どおり（残高・キーの問題は、上のモデルでも直らない）


def test_upper_tier_timeout_keeps_the_lower_valid_decision(conn):
    weak = ("select_card_type", {"reason": "r", "evidence": "入力にない引用", "type_id": "cleaning"})
    f = by_model({"haiku": [weak], "sonnet": APITimeoutError()})
    d = agent.decide(conn, ORG, stage="classify", inputs=INPUTS, card_id="c", client_factory=f)
    assert d.tool == "select_card_type" and not d.default_used and d.model.startswith("claude-haiku")


# ---- ジョブ全体の期限 --------------------------------------------------------------------------------------------

@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def test_job_deadline_keeps_finished_results_marks_timeout_and_does_not_wait(conn, alice, obj):
    cfg = {**llm.load_config(), "job_deadline": 0.4}
    j = Manual(parallel=True)
    cid = cards.create_card_async(conn, alice, obj["obj_id"], jobs=j, before_desc="汚れ", config=cfg,
                                  client_factory=script_delays({"privacy": 3.0}))["card"]["card_id"]
    t0 = time.time()
    j.run_pending()
    assert time.time() - t0 < 1.5  # 3 秒の判断を、待たない
    r = row(conn, cid)
    assert r["ai_status"] == "timeout" and r["title"] == "エアコン清掃"  # 出せた分は保存されている
    assert "privacy" in db.one(conn, "SELECT detail FROM audit_log WHERE action='card.ai_timeout'")["detail"]


def script_delays(delays):
    from app.tests.test_title_first import slow
    return slow(delays)


def test_page_shows_a_message_for_timed_out_ai_work(conn, alice, obj):
    from app.tests.test_web import get, login, text
    cid = cards.create_card(conn, alice, obj["obj_id"], before_desc="x")["card"]["card_id"]
    db.run(conn, "UPDATE card SET ai_status='timeout' WHERE card_id=?", (cid,))
    page = text(get(conn, f"/c/{cid}", cookie=login(conn, alice)))
    assert "時間がかかっています" in page and "http-equiv" not in page  # 自動更新は続けない


# ---- 応答の深さ（effort）・思考の切り替え（既定は送らない） -------------------------------------------------

def sent_kw(cfg, kind, provider="anthropic"):
    f = route_factory({})
    llm.call  # noqa: B018
    cfg = {**llm.load_config(), "providers": [provider], **cfg}
    ask_cfg = ask
    ask_cfg(None if False else _conn_holder[0], f, kind, cfg)
    return f.calls[0][1]


_conn_holder = [None]


@pytest.fixture(autouse=True)
def _hold_conn(conn):
    _conn_holder[0] = conn
    yield


def test_default_sends_effort_low_for_decisions_only_and_never_thinking_off():
    assert sent_kw({}, "decide_light")["output_config"] == {"effort": "low"}
    assert sent_kw({}, "decide_heavy")["output_config"] == {"effort": "low"}
    for k in ("triage", "describe", "describe_refine"):  # Haiku は非対応、説明文は未検証なので、送らない
        assert "output_config" not in sent_kw({}, k)
    assert "thinking" not in sent_kw({}, "decide_light")
    assert "output_config" not in sent_kw({}, "decide_light", provider="orca")


def test_effort_can_be_cleared_by_config():
    assert "output_config" not in sent_kw({"effort_by_kind": {}}, "decide_light")


def test_effort_is_sent_only_for_configured_kinds_and_supported_models():
    cfg = {"effort_by_kind": {"decide_light": "low", "triage": "low"}}
    assert sent_kw(cfg, "decide_light")["output_config"] == {"effort": "low"}   # Sonnet 5
    assert "output_config" not in sent_kw(cfg, "triage")                        # Haiku 4.5 は effort に対応しない（400 になる）
    assert "output_config" not in sent_kw(cfg, "decide_heavy")                  # 設定していない種類
    assert "output_config" not in sent_kw(cfg, "decide_light", provider="orca")  # Orca 側の対応は未確認


def test_thinking_can_be_switched_off_only_for_sonnet_5():
    cfg = {"thinking_off": ["decide_light", "decide_heavy", "triage"]}
    assert sent_kw(cfg, "decide_light")["thinking"] == {"type": "disabled"}
    assert "thinking" not in sent_kw(cfg, "decide_heavy")  # Opus 5 は、切ると、ツール呼び出しが本文に書かれる恐れがあるので、送らない
    assert "thinking" not in sent_kw(cfg, "triage")
